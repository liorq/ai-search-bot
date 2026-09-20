"""
Where a write is allowed to land — an explicit list, not a guess about the name.
================================================================================

The rehearsal writes to a site, verifies, and restores. Recognising the target
by its name is not good enough: `.local` is a convention, and
`http://my-site.local.example.com` ends in neither `.local` nor anything a
suffix test would catch, while `https://my-site.local` on another port is a
different server altogether.

So three things are checked, and all three have to pass:

    * the origin is on an explicit list, matched exactly after normalising;
    * the site says it is that site — WordPress reports its own `home`/`url`
      at `/wp-json/`, and a tunnel, a proxy or a stale hosts entry shows up
      here as a mismatch;
    * a write never follows a redirect. A 301 to production is exactly how a
      staging URL becomes a live edit, so the redirect is an error, not a hop.

Fail closed: no list, an unreadable list, or an empty one allows nothing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from .. import paths
from ..schema import Result

#: Methods that change something. HEAD/GET/OPTIONS are reads.
WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: Where the list lives — beside .env and clients.json, not in the repo.
TARGETS_FILE = "write_targets.json"

DEFAULT_PORTS = {"http": 80, "https": 443}


def origin_of(url: str) -> str:
    """scheme://host[:port], lowercased, with the default port folded away.

    Comparing whole URLs would make `/wp-json/wp/v2/pages/7` a different
    target from `/wp-json/`; comparing hosts alone would let `https://` pass a
    list that only allows `http://`, which on a development box is a different
    server with a different database.
    """
    parts = urlsplit(url.strip())
    if not parts.scheme or not parts.hostname:
        return ""
    scheme = parts.scheme.lower()
    host = parts.hostname.lower()
    port = parts.port
    if port is None or port == DEFAULT_PORTS.get(scheme):
        return f"{scheme}://{host}"
    return f"{scheme}://{host}:{port}"


def targets_path() -> Path:
    return paths.home() / TARGETS_FILE


def load_allowed(purpose: str = "rehearsal", path: Path | None = None) -> frozenset[str]:
    """The origins this purpose may write to. Anything unreadable means none."""
    source = path or targets_path()
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
        listed = raw.get(purpose) or []
    except (OSError, json.JSONDecodeError, AttributeError):
        return frozenset()
    return frozenset(o for o in (origin_of(str(u)) for u in listed) if o)


@dataclass(frozen=True)
class WriteGuard:
    """Consulted before every write. Refusing is the default, not the exception."""

    allowed: frozenset[str]
    purpose: str = "rehearsal"

    @classmethod
    def for_purpose(cls, purpose: str = "rehearsal", path: Path | None = None) -> "WriteGuard":
        return cls(load_allowed(purpose, path), purpose)

    def describe(self) -> str:
        return ", ".join(sorted(self.allowed)) or "אין אף יעד מורשה"

    def check(self, method: str, url: str) -> Result:
        """Whether this request may be sent at all."""
        if method.upper() not in WRITE_METHODS:
            return Result.success("read", "קריאה — לא נדרש אישור יעד")
        target = origin_of(url)
        if not target:
            return Result.failure("write_target_unparsable",
                                  f"לא הצלחתי לקרוא יעד מהכתובת {url}", recoverable=False)
        if not self.allowed:
            return Result.failure(
                "write_targets_empty",
                f"אין רשימת יעדים מורשים ל-{self.purpose} ב-{targets_path()} — "
                "כתיבה חסומה עד שתוגדר במפורש", recoverable=False)
        if target not in self.allowed:
            return Result.failure(
                "write_target_refused",
                f"כתיבה ל-{target} נחסמה — היעד לא ברשימת המורשים ({self.describe()})",
                recoverable=False)
        return Result.success("allowed", f"{target} מורשה לכתיבה")

    def check_landing(self, url: str, history: Any = None) -> Result:
        """Whether a write that was sent actually landed where it was allowed to.

        A redirect is refused even when it points at an allowed origin: the
        request body is not guaranteed to survive the hop, and the caller
        should address the correct URL rather than let the server choose.
        """
        hops = list(history or [])
        if not hops:
            return Result.success("direct", "הבקשה הגיעה ישירות ליעד")
        landed = origin_of(url)
        return Result.failure(
            "write_redirected",
            f"בקשת כתיבה הופנתה אל {landed or url} ({len(hops)} הפניות) — "
            "הבקשה לא תישלח שוב לשם. פנה ישירות לכתובת הנכונה",
            recoverable=False)


def verify_identity(base_url: str, call: Callable[[str], Result]) -> Result:
    """Ask the site who it is, and refuse if the answer is somebody else.

    WordPress returns `url` and `home` from its REST root. A machine answering
    for an allow-listed host — through a proxy, a tunnel, or a hosts file that
    someone changed — gives itself away here.
    """
    expected = origin_of(base_url)
    reply = call(f"{base_url.rstrip('/')}/wp-json/")
    if not reply:
        return Result.failure("identity_unreadable",
                              f"לא הצלחתי לקרוא את זהות האתר ב-{base_url}: {reply.detail}",
                              recoverable=False)
    payload = reply.data.get("payload") or {}
    claimed = [origin_of(str(payload.get(field, ""))) for field in ("home", "url")]
    claimed = [c for c in claimed if c]
    if not claimed:
        return Result.failure("identity_missing",
                              f"{base_url} לא דיווח על הכתובת שלו ב-/wp-json/ — "
                              "אי אפשר לאמת שזה האתר הנכון", recoverable=False)
    if expected not in claimed:
        return Result.failure(
            "identity_mismatch",
            f"פניתי ל-{expected} והאתר מדווח על עצמו כ-{', '.join(claimed)} — "
            "לא כותבים ליעד שלא מזדהה כמצופה", recoverable=False)
    return Result.success("identity", f"{expected} אישר את זהותו")
