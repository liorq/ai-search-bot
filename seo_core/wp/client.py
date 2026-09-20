"""
WordPress REST client, and the capability probe that runs before any edit.
=========================================================================

Nothing here assumes a site is editable. A great many WordPress installs
expose the REST API for reading and quietly refuse writes: a security plugin
blocks the route, the application password belongs to an Editor who cannot
touch that post type, or `meta` is simply not registered in REST for the post
type we care about — in which case a PATCH returns 200 and changes nothing.

`probe()` establishes what is actually possible. A skill that cannot write
falls back to producing text for a human to paste; it never pushes harder.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlparse

from ..schema import Result
from . import targets

REQUEST_TIMEOUT = 20          # שניות
USER_AGENT      = "seo-toolkit/1.0 (+WordPress REST)"

#: Post types we are willing to edit. Anything else is left alone — editing an
#: unknown custom post type without understanding its template is reckless.
EDITABLE_POST_TYPES = ("post", "page")

#: The one meta key whose change nobody sees until Elementor re-renders.
ELEMENTOR_DATA_KEY = "_elementor_data"


class Transport(Protocol):
    """The slice of `requests.Session` this module uses.

    Declared as a Protocol so tests can pass a fake and run offline.
    """

    def request(self, method: str, url: str, **kwargs: Any) -> Any: ...


@dataclass
class Capabilities:
    """What this site and this credential can actually do."""

    reachable: bool = False
    authenticated: bool = False
    user_id: int | None = None
    user_roles: list[str] = field(default_factory=list)
    can_edit: dict[str, bool] = field(default_factory=dict)      # post type → editable
    meta_exposed: dict[str, bool] = field(default_factory=dict)  # post type → meta in REST
    blockers: list[str] = field(default_factory=list)

    def can_write(self, post_type: str = "post") -> bool:
        """Whether a content edit to this post type would actually land."""
        return (
            self.reachable
            and self.authenticated
            and self.can_edit.get(post_type, False)
        )

    def can_write_meta(self, post_type: str = "post") -> bool:
        """Whether an SEO-meta edit would land.

        Separate from `can_write` on purpose: `meta` not being registered in
        REST is the failure that looks like success, returning 200 while
        silently discarding the field.
        """
        return self.can_write(post_type) and self.meta_exposed.get(post_type, False)

    def summary(self) -> str:
        if not self.reachable:
            return "האתר לא נגיש דרך REST"
        if not self.authenticated:
            return "האתר נגיש אבל האימות נכשל"
        writable = [t for t, ok in self.can_edit.items() if ok]
        if not writable:
            return "מאומת, אבל למשתמש אין הרשאת עריכה"
        meta_ok = [t for t, ok in self.meta_exposed.items() if ok]
        return (
            f"עריכה אפשרית ב: {', '.join(writable)}"
            + (f" · meta חשוף ב: {', '.join(meta_ok)}" if meta_ok else " · meta לא חשוף")
        )


class WordPressClient:
    """Read and write posts over the REST API.

    Every method returns a `Result` rather than raising, because a skill needs
    to distinguish "the write did not happen" from "the write happened and
    then something else went wrong" before deciding whether to roll back.
    """

    def __init__(
        self,
        base_url: str,
        username: str,
        app_password: str,
        transport: Transport | None = None,
        write_guard: Any | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.username = username
        self._password = app_password
        self._transport = transport
        self.capabilities = Capabilities()
        #: When set, every write is checked against it first. The rehearsal
        #: always sets one; a skill's publish mode may. `_call` is the only
        #: place a request is sent, so one check here covers all of them.
        self.write_guard = write_guard

    # ---------- plumbing ----------

    @property
    def api(self) -> str:
        return f"{self.base_url}/wp-json/wp/v2"

    def _session(self) -> Transport:
        if self._transport is None:
            import requests

            session = requests.Session()
            session.auth = (self.username, self._password)
            session.headers.update({"User-Agent": USER_AGENT})
            self._transport = session
        return self._transport

    def _call(self, method: str, url: str, expect_json: bool = True, **kwargs: Any) -> Result:
        kwargs.setdefault("timeout", REQUEST_TIMEOUT)

        guarded = self.write_guard is not None and method.upper() in targets.WRITE_METHODS
        if guarded:
            permitted = self.write_guard.check(method, url)
            if not permitted:
                return permitted
            # Never follow a redirect on a write: a 301 from staging to
            # production is how a rehearsal becomes a live edit.
            kwargs.setdefault("allow_redirects", False)

        try:
            response = self._session().request(method, url, **kwargs)
        except Exception as exc:                      # network, TLS, DNS
            return Result.failure(
                "unreachable", f"לא הצלחתי להגיע ל-{url}: {exc}", recoverable=True
            )

        status = getattr(response, "status_code", 0)
        if guarded:
            if 300 <= status < 400:
                return Result.failure(
                    "write_redirected",
                    f"בקשת הכתיבה ל-{url} קיבלה הפניה ({status}) אל "
                    f"{getattr(response, 'headers', {}).get('Location', 'יעד לא ידוע')} — "
                    "לא נשלחה שוב. פנה ישירות לכתובת הנכונה", recoverable=False)
            landed = self.write_guard.check_landing(
                getattr(response, "url", url), getattr(response, "history", None))
            if not landed:
                return landed
        if status == 401:
            return Result.failure("unauthorized", "האימות נדחה — בדוק את סיסמת האפליקציה")
        if status == 403:
            return Result.failure("forbidden", "למשתמש אין הרשאה לפעולה הזו")
        if status == 404:
            return Result.failure("not_found", f"לא נמצא: {url}")
        if status >= 500:
            return Result.failure(
                "server_error", f"השרת החזיר {status}", recoverable=True
            )
        if status >= 400:
            return Result.failure("http_error", f"השרת החזיר {status}")

        try:
            payload = response.json()
        except Exception:
            if not expect_json:
                # Some endpoints answer 200 with an empty body. Elementor's
                # cache route is one; calling that a failure would report a
                # cache that was cleared as a cache that was not.
                return Result.success("ok", "בוצע", payload=None, status=status)
            return Result.failure("bad_payload", "התשובה מהשרת אינה JSON תקין")

        return Result.success("ok", "בוצע", payload=payload, status=status)

    # ---------- probe ----------

    def probe(self, post_types: tuple[str, ...] = EDITABLE_POST_TYPES) -> Capabilities:
        """Establish what is possible before any edit is attempted."""
        caps = Capabilities()

        root = self._call("GET", f"{self.base_url}/wp-json/")
        if not root:
            caps.blockers.append(root.detail)
            self.capabilities = caps
            return caps
        caps.reachable = True

        me = self._call("GET", f"{self.api}/users/me", params={"context": "edit"})
        if not me:
            caps.blockers.append(f"אימות: {me.detail}")
            self.capabilities = caps
            return caps

        user = me.data.get("payload", {})
        caps.authenticated = True
        caps.user_id = user.get("id")
        caps.user_roles = list(user.get("roles", []))

        # `capabilities` only appears in the edit context, and only for a user
        # allowed to see it. Absent, we fall back to the role name.
        wp_caps = user.get("capabilities", {}) or {}
        for post_type in post_types:
            if wp_caps:
                editable = bool(wp_caps.get(f"edit_{post_type}s") or wp_caps.get("edit_posts"))
            else:
                editable = bool({"administrator", "editor"} & set(caps.user_roles))
            caps.can_edit[post_type] = editable
            if not editable:
                caps.blockers.append(f"אין הרשאת עריכה ל-{post_type}")

        # A PATCH to an unregistered `meta` returns 200 and discards the field,
        # so the route description is the only honest way to know. The root
        # index fetched above already carries it.
        for post_type in post_types:
            caps.meta_exposed[post_type] = self._meta_is_exposed(root, post_type)
            if not caps.meta_exposed[post_type]:
                caps.blockers.append(
                    f"meta לא חשוף ב-REST עבור {post_type} — שינויי SEO לא יישמרו"
                )

        self.capabilities = caps
        return caps

    @staticmethod
    def _meta_is_exposed(root: Result, post_type: str) -> bool:
        if not root:
            return False
        routes = root.data.get("payload", {}).get("routes", {})
        base = f"/wp/v2/{post_type}s"
        for path, spec in routes.items():
            if not path.startswith(base):
                continue
            for endpoint in spec.get("endpoints", []):
                if "meta" in (endpoint.get("args") or {}):
                    return True
        return False

    # ---------- reading ----------

    def get_post(self, post_id: int, post_type: str = "posts") -> Result:
        return self._call(
            "GET", f"{self.api}/{post_type}/{post_id}", params={"context": "edit"}
        )

    def find_by_url(self, url: str) -> Result:
        """Resolve a public URL to its post, trying pages before posts.

        Most of what an SEO skill touches is a page, so checking pages first
        saves a round trip on the common case.
        """
        slug = _slug_from_url(url)
        if not slug:
            return Result.failure("bad_url", f"לא הצלחתי לחלץ slug מ-{url}")

        for post_type in ("pages", "posts"):
            result = self._call(
                "GET",
                f"{self.api}/{post_type}",
                params={"slug": slug, "context": "edit", "per_page": 1},
            )
            if not result:
                continue
            items = result.data.get("payload") or []
            if items:
                return Result.success(
                    "found", f"נמצא {post_type[:-1]}", payload=items[0], post_type=post_type
                )

        return Result.failure("not_found", f"לא נמצא תוכן עבור {url} (slug: {slug})")

    # ---------- writing ----------

    def update_post(
        self,
        post_id: int,
        *,
        post_type: str = "posts",
        content: str | None = None,
        meta: dict[str, Any] | None = None,
        title: str | None = None,
    ) -> Result:
        """Write to a post. Refuses rather than pretending when it cannot.

        A meta-only write against a site where `meta` is not exposed in REST
        would return 200 and change nothing, so that case is rejected up front
        instead of being reported as a success.
        """
        singular = post_type.rstrip("s")
        if not self.capabilities.reachable:
            return Result.failure(
                "not_probed",
                "probe() לא רץ — אסור לכתוב לפני שבודקים מה אפשרי",
                recoverable=True,
            )
        if not self.capabilities.can_write(singular):
            return Result.failure(
                "no_permission",
                f"אין הרשאת כתיבה ל-{singular}: {self.capabilities.summary()}",
                recoverable=True,
            )
        if meta and not self.capabilities.can_write_meta(singular):
            return Result.failure(
                "meta_not_exposed",
                f"meta לא חשוף ב-REST עבור {singular} — הכתיבה הייתה מתקבלת ונזרקת בשקט",
                recoverable=True,
            )

        payload: dict[str, Any] = {}
        if content is not None:
            payload["content"] = content
        if title is not None:
            payload["title"] = title
        if meta:
            payload["meta"] = meta
        if not payload:
            return Result.failure("empty_write", "לא נשלח שום שינוי")

        written = self._call("POST", f"{self.api}/{post_type}/{post_id}", json=payload)
        if written and meta and ELEMENTOR_DATA_KEY in meta:
            # The widget tree is saved, and the visitor still gets the old HTML:
            # Elementor caches a page's rendered markup per document, and only
            # its own save path clears it. Measured on Elementor 4.2 — a new
            # widget, and even an edit to an existing one, stayed invisible
            # until this ran. A write nobody can see is not a write.
            cleared = self.clear_elementor_cache()
            written.data["cache_cleared"] = bool(cleared)
            written.data["cache_detail"] = cleared.detail
        return written

    def clear_elementor_cache(self) -> Result:
        """Make Elementor re-render. Site-wide, because that is what it offers.

        This is the endpoint behind "Regenerate Files & Data" in Elementor's
        own Tools screen. It throws away generated markup and CSS; nothing
        authored is touched, and the next visitor pays one re-render.
        """
        cleared = self._call("DELETE", f"{self.base_url}/wp-json/elementor/v1/cache",
                             expect_json=False)
        if cleared:
            return Result.success("cache_cleared", "המטמון של Elementor נוקה")
        if cleared.code == "not_found":
            return Result.failure(
                "cache_endpoint_missing",
                "לא נמצאה נקודת הקצה לניקוי המטמון של Elementor — "
                "גרסה ישנה מדי. שינוי בדף עלול לא להופיע למבקרים",
                recoverable=True)
        return Result.failure("cache_not_cleared",
                              f"ניקוי המטמון של Elementor נכשל: {cleared.detail}",
                              recoverable=True)

    def list_posts(self, post_type: str = "pages", per_page: int = 20) -> Result:
        """Recent posts of a type, newest first.

        Used where a caller needs *a* page rather than a particular one — the
        rehearsal, which only needs somewhere safe to write and undo.
        """
        result = self._call(
            "GET",
            f"{self.api}/{post_type}",
            params={"per_page": per_page, "context": "edit", "status": "publish"},
        )
        if not result:
            return result
        items = result.data.get("payload") or []
        if not items:
            return Result.failure("no_posts", f"לא נמצאו {post_type} מפורסמים באתר")
        return Result.success("listed", f"{len(items)} {post_type}", items=items)

    def list_revisions(self, post_id: int, post_type: str = "posts") -> Result:
        return self._call("GET", f"{self.api}/{post_type}/{post_id}/revisions")


def _slug_from_url(url: str) -> str:
    path = urlparse(url).path.strip("/")
    if not path:
        return ""
    slug = path.split("/")[-1]
    # Strip a trailing file extension, e.g. an old .html permalink structure.
    return re.sub(r"\.(html?|php)$", "", slug)
