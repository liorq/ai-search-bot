"""
A polite crawl of one site, and the link graph it produces.
===========================================================

Two skills need the same thing and neither needs a full crawler: where every
internal link goes, what text it was given, and how far each page sits from
the front door.

Three decisions shape this module.

**Boilerplate is separated from editorial links, by counting.** A menu link
appears on every page; an in-body link appears on one. Trying to tell them
apart by looking for `<nav>` or a theme's class names fails on the first site
that builds its menu out of divs. Counting how many pages carry the identical
link answers it without knowing anything about the theme, and the distinction
matters: a page with forty inbound links from the footer has one editorial
vote, not forty.

**Redirects are recorded, not followed silently.** An internal link pointing
at a URL that redirects is a link that costs a round trip and loses its
target's identity in every report built on it. The chain is kept.

**The crawl refuses to be impolite.** robots.txt is parsed before the first
request, a delay separates requests, and there is a hard page cap. This runs
against a client's production site — the failure mode of getting it wrong is
not a bad report, it is a site under load.

The fetcher is injected, so the whole module is testable without a network.
"""

from __future__ import annotations

import re
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from html import unescape
from typing import Callable, Iterable
from urllib.parse import urljoin, urlparse, urlunparse, parse_qsl, urlencode

from ..schema import Result

#: (status, html, final_url) — final_url differs from the request when the
#: server redirected, which is the whole reason it is returned.
Fetcher = Callable[[str], tuple[int, str, str]]

USER_AGENT = "seo-toolkit"

#: Defaults chosen to be forgettable rather than fast. A crawl that hurts a
#: client's site is worse than no crawl.
DEFAULT_DELAY = 0.5
DEFAULT_MAX_PAGES = 400
DEFAULT_TIMEOUT = 20

#: A link carried by this share of crawled pages is navigation, not an
#: editorial recommendation.
BOILERPLATE_SHARE = 0.5

#: Below this many pages the share test means nothing — on a six-page site
#: half the pages linking to the services page is ordinary.
BOILERPLATE_MIN_PAGES = 12

#: Query parameters that never identify a different page.
_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "gclid", "fbclid", "msclkid", "mc_cid", "mc_eid", "_ga", "ref",
}

_SKIP_SCHEMES = ("mailto:", "tel:", "javascript:", "sms:", "whatsapp:", "#")
_SKIP_SUFFIXES = (
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico", ".pdf", ".zip",
    ".mp4", ".mp3", ".css", ".js", ".xml", ".doc", ".docx", ".xls", ".xlsx",
)

_ANCHOR = re.compile(r"<a\b([^>]*)>(.*?)</a>", re.I | re.S)
_ATTR = re.compile(r"""(\w[\w:-]*)\s*=\s*["']([^"']*)["']""")
_TAG = re.compile(r"<[^>]+>")
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_CANONICAL = re.compile(
    r"""<link[^>]+rel\s*=\s*["']canonical["'][^>]*>""", re.I)
_META_ROBOTS = re.compile(
    r"""<meta[^>]+name\s*=\s*["']robots["'][^>]*>""", re.I)
_HREF_ATTR = re.compile(r"""href\s*=\s*["']([^"']*)["']""", re.I)
_CONTENT_ATTR = re.compile(r"""content\s*=\s*["']([^"']*)["']""", re.I)

#: Anchors that describe the act of clicking rather than the destination.
GENERIC_ANCHORS = {
    "click here", "here", "read more", "more", "learn more", "this page",
    "link", "this link", "website", "see more", "details", "info",
    "לחץ כאן", "כאן", "קרא עוד", "למידע נוסף", "לפרטים", "עוד", "לחצו כאן",
    "המשך קריאה", "לקריאה", "אתר", "קישור", "לחץ",
}


# ═══════════════════════════════════════════════════════
#  URLs
# ═══════════════════════════════════════════════════════

def normalise(url: str, base: str = "") -> str:
    """One spelling per page, so the graph does not split a page in two.

    Fragments go, tracking parameters go, and the default port goes. The
    trailing slash is left exactly as the site wrote it, because on some
    servers `/about` and `/about/` really are different pages and deciding
    otherwise here would merge two URLs the site treats as separate.
    """
    if base:
        url = urljoin(base, url)
    parts = urlparse(url.strip())

    netloc = parts.netloc.lower()
    for scheme, port in (("http", ":80"), ("https", ":443")):
        if parts.scheme == scheme and netloc.endswith(port):
            netloc = netloc[: -len(port)]

    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if k.lower() not in _TRACKING_PARAMS]

    return urlunparse((
        parts.scheme.lower(), netloc, parts.path, parts.params,
        urlencode(kept), "",
    ))


def same_site(url: str, root: str) -> bool:
    """Whether a URL belongs to the site being crawled.

    `www` counts as the same site. A subdomain does not: a blog on its own
    subdomain has its own link graph, and pretending otherwise produces a
    report about a site nobody manages as one.
    """
    host = urlparse(url).netloc.lower().removeprefix("www.")
    return bool(host) and host == urlparse(root).netloc.lower().removeprefix("www.")


def is_crawlable(url: str) -> bool:
    if any(url.lower().startswith(scheme) for scheme in _SKIP_SCHEMES):
        return False
    path = urlparse(url).path.lower()
    return not path.endswith(_SKIP_SUFFIXES)


# ═══════════════════════════════════════════════════════
#  robots.txt
# ═══════════════════════════════════════════════════════

@dataclass
class Robots:
    """The subset of robots.txt that decides whether we may fetch a URL."""

    disallow: list[str] = field(default_factory=list)
    allow: list[str] = field(default_factory=list)
    crawl_delay: float | None = None
    fetched: bool = False

    def allows(self, url: str) -> bool:
        path = urlparse(url).path or "/"
        # The longest matching rule wins, and Allow beats Disallow at equal
        # length — the behaviour every major crawler implements.
        best_allow = max((len(r) for r in self.allow if path.startswith(r)), default=-1)
        best_deny = max((len(r) for r in self.disallow if path.startswith(r)), default=-1)
        return best_allow >= best_deny


def parse_robots(text: str, agent: str = USER_AGENT) -> Robots:
    """Read the groups that apply to us, falling back to the wildcard group."""
    groups: dict[str, Robots] = {}
    current: list[str] = []

    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        field_name, _, value = line.partition(":")
        field_name, value = field_name.strip().lower(), value.strip()

        if field_name == "user-agent":
            current = [value.lower()]
            groups.setdefault(value.lower(), Robots(fetched=True))
            continue
        for name in current:
            group = groups.setdefault(name, Robots(fetched=True))
            if field_name == "disallow" and value:
                group.disallow.append(value)
            elif field_name == "allow" and value:
                group.allow.append(value)
            elif field_name == "crawl-delay":
                try:
                    group.crawl_delay = float(value)
                except ValueError:
                    pass

    for name in (agent.lower(), "*"):
        if name in groups:
            return groups[name]
    return Robots(fetched=True)


# ═══════════════════════════════════════════════════════
#  Pages and links
# ═══════════════════════════════════════════════════════

@dataclass(frozen=True)
class Link:
    source: str
    target: str
    anchor: str
    nofollow: bool = False

    @property
    def is_generic(self) -> bool:
        """Anchor text that describes clicking rather than the destination."""
        return self.anchor.strip().lower().strip(" .!:-—") in GENERIC_ANCHORS

    @property
    def is_empty(self) -> bool:
        """An image link or a bare icon — no text for Google to read."""
        return not self.anchor.strip()

    @property
    def key(self) -> tuple[str, str]:
        """Identity for counting how many pages carry the same link."""
        return (self.target, self.anchor.strip().lower())


@dataclass
class Page:
    url: str
    status: int
    depth: int
    title: str = ""
    canonical: str = ""
    noindex: bool = False
    links: list[Link] = field(default_factory=list)
    redirect_chain: list[str] = field(default_factory=list)
    html_length: int = 0
    text: str = ""                       # visible text, for finding mentions

    @property
    def redirected(self) -> bool:
        return len(self.redirect_chain) > 1

    @property
    def is_ok(self) -> bool:
        return 200 <= self.status < 300


def _attributes(raw: str) -> dict[str, str]:
    return {k.lower(): v for k, v in _ATTR.findall(raw)}


def _text(html_fragment: str) -> str:
    return re.sub(r"\s+", " ", unescape(_TAG.sub(" ", html_fragment))).strip()


def extract_links(url: str, html: str) -> list[Link]:
    """Every internal-or-external link on the page, with the text it carries."""
    found: list[Link] = []
    for raw_attrs, inner in _ANCHOR.findall(html):
        attrs = _attributes(raw_attrs)
        href = attrs.get("href", "").strip()
        if not href or not is_crawlable(href):
            continue
        anchor = _text(inner)
        if not anchor:
            # An image link carries its alt text to Google, so it is not blank.
            alt = re.search(r"""alt\s*=\s*["']([^"']*)["']""", inner, re.I)
            anchor = _text(alt.group(1)) if alt else ""
        found.append(Link(
            source=url,
            target=normalise(href, url),
            anchor=anchor,
            nofollow="nofollow" in attrs.get("rel", "").lower(),
        ))
    return found


_SCRIPT_OR_STYLE = re.compile(r"<(script|style)\b.*?</\1>", re.I | re.S)


def visible_text(html: str) -> str:
    """The words on the page, with scripts and markup taken out."""
    return _text(_SCRIPT_OR_STYLE.sub(" ", html))


def parse_page(url: str, status: int, html: str, depth: int,
               redirect_chain: list[str] | None = None) -> Page:
    title = _TITLE.search(html)
    canonical_tag = _CANONICAL.search(html)
    canonical = ""
    if canonical_tag:
        href = _HREF_ATTR.search(canonical_tag.group(0))
        canonical = normalise(href.group(1), url) if href else ""

    robots_tag = _META_ROBOTS.search(html)
    noindex = False
    if robots_tag:
        content = _CONTENT_ATTR.search(robots_tag.group(0))
        noindex = bool(content) and "noindex" in content.group(1).lower()

    return Page(
        url=url, status=status, depth=depth,
        title=_text(title.group(1)) if title else "",
        canonical=canonical, noindex=noindex,
        links=extract_links(url, html),
        text=visible_text(html),
        redirect_chain=list(redirect_chain or []),
        html_length=len(html),
    )


# ═══════════════════════════════════════════════════════
#  The crawl
# ═══════════════════════════════════════════════════════

@dataclass
class Crawl:
    """Everything one pass over a site produced."""

    root: str
    pages: dict[str, Page] = field(default_factory=dict)
    robots: Robots = field(default_factory=Robots)
    blocked: list[str] = field(default_factory=list)
    stopped_at_cap: bool = False

    @property
    def internal_links(self) -> list[Link]:
        return [
            link for page in self.pages.values() for link in page.links
            if same_site(link.target, self.root)
        ]

    def boilerplate_keys(self) -> set[tuple[str, str]]:
        """Links carried by so many pages that they are navigation.

        Below `BOILERPLATE_MIN_PAGES` nothing is called boilerplate: on a
        small site, every page linking to `/contact` from the body is normal
        and calling it a menu would erase the site's real link graph.
        """
        crawled = len([p for p in self.pages.values() if p.is_ok])
        if crawled < BOILERPLATE_MIN_PAGES:
            return set()

        seen: Counter[tuple[str, str]] = Counter()
        for page in self.pages.values():
            for key in {link.key for link in page.links}:
                seen[key] += 1

        threshold = crawled * BOILERPLATE_SHARE
        return {key for key, count in seen.items() if count >= threshold}

    def editorial_links(self) -> list[Link]:
        """Internal links that are a recommendation rather than a menu."""
        boilerplate = self.boilerplate_keys()
        return [link for link in self.internal_links if link.key not in boilerplate]

    def summary(self) -> str:
        ok = sum(1 for p in self.pages.values() if p.is_ok)
        broken = sum(1 for p in self.pages.values() if p.status >= 400)
        return (
            f"{len(self.pages)} דפים נסרקו · {ok} תקינים · {broken} שבורים · "
            f"{len(self.editorial_links())} קישורים פנימיים עריכתיים"
        )


def crawl(
    root: str,
    fetch: Fetcher,
    *,
    robots_text: str | None = None,
    max_pages: int = DEFAULT_MAX_PAGES,
    delay: float = DEFAULT_DELAY,
    sleep: Callable[[float], None] = time.sleep,
) -> Crawl:
    """Walk the site breadth-first from the front door.

    Breadth-first is not an implementation detail: the order *is* the depth
    measurement. A page first reached on the third wave is three clicks from
    home, and any other traversal order would report a different number for
    the same site.
    """
    start = normalise(root)
    robots = parse_robots(robots_text) if robots_text is not None else Robots()
    if robots.crawl_delay:
        delay = max(delay, robots.crawl_delay)

    result = Crawl(root=start, robots=robots)
    queue: deque[tuple[str, int]] = deque([(start, 0)])
    seen = {start}

    while queue:
        if len(result.pages) >= max_pages:
            result.stopped_at_cap = True
            break

        url, depth = queue.popleft()
        if robots.fetched and not robots.allows(url):
            result.blocked.append(url)
            continue

        try:
            status, html, final_url = fetch(url)
        except Exception:
            result.pages[url] = Page(url=url, status=0, depth=depth)
            continue

        chain = [url] if normalise(final_url) == url else [url, normalise(final_url)]
        page = parse_page(url, status, html, depth, chain)
        result.pages[url] = page

        if not page.is_ok:
            continue

        if delay:
            sleep(delay)

        for link in page.links:
            target = link.target
            if target in seen or not same_site(target, start):
                continue
            seen.add(target)
            queue.append((target, depth + 1))

    return result


def requests_fetcher(timeout: int = DEFAULT_TIMEOUT, *, follow: bool = True) -> Fetcher:
    """The real fetcher, kept out of the crawl so tests never touch a network.

    `follow=False` returns each redirect as itself rather than its
    destination, which is what `trace` needs to count hops.
    """
    import requests

    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT

    def fetch(url: str) -> tuple[int, str, str]:
        response = session.get(url, timeout=timeout, allow_redirects=follow)
        if not follow and response.is_redirect:
            target = response.headers.get("Location", "")
            return response.status_code, "", normalise(target, url)
        return response.status_code, response.text, response.url

    return fetch


MAX_HOPS = 10


@dataclass
class Trace:
    """One URL's redirect chain, followed a hop at a time."""

    chain: list[str]
    status: int = 0
    looped: bool = False
    truncated: bool = False

    @property
    def hops(self) -> int:
        return max(0, len(self.chain) - 1)

    @property
    def destination(self) -> str:
        return self.chain[-1]

    def describe(self) -> str:
        if self.looped:
            return f"לולאת הפניות: {' → '.join(self.chain[-3:])}"
        if self.truncated:
            return f"יותר מ-{MAX_HOPS} הפניות ברצף — לא הגענו ליעד"
        if self.hops == 0:
            return "אין הפניה"
        if self.hops == 1:
            return f"הפניה אחת אל {self.destination}"
        return f"{self.hops} הפניות ברצף אל {self.destination}"


def trace(url: str, fetch: Fetcher, max_hops: int = MAX_HOPS) -> Trace:
    """Follow a redirect one hop at a time, so the chain can be counted.

    A fetcher that follows redirects itself hands back the destination and
    nothing else, and "this URL redirects" is a very different finding from
    "this URL redirects four times through two domains". Only run against
    URLs already known to redirect — it costs one request per hop.
    """
    chain = [normalise(url)]
    status = 0

    for _ in range(max_hops):
        try:
            status, _html, target = fetch(chain[-1])
        except Exception:
            return Trace(chain, status=0)

        if not (300 <= status < 400):
            return Trace(chain, status=status)

        target = normalise(target)
        if not target:
            return Trace(chain, status=status)
        if target in chain:
            return Trace(chain + [target], status=status, looped=True)
        chain.append(target)

    return Trace(chain, status=status, truncated=True)


def load_robots(root: str, fetch: Fetcher) -> str:
    """Fetch robots.txt, treating an unreadable one as no restrictions.

    A 404 means there are no rules. A 500 means the server is unwell, and
    refusing to crawl over that would be its own kind of wrong — but it is
    said out loud rather than assumed.
    """
    try:
        status, text, _ = fetch(urljoin(root, "/robots.txt"))
    except Exception:
        return ""
    return text if status == 200 else ""


def check_start(root: str, fetch: Fetcher) -> Result:
    """Confirm the front door answers before crawling four hundred pages."""
    try:
        status, html, final_url = fetch(normalise(root))
    except Exception as exc:
        return Result.failure("unreachable", f"האתר לא נענה: {exc}")

    if status >= 400:
        return Result.failure("bad_status", f"דף הבית מחזיר {status}")
    if normalise(final_url) != normalise(root):
        return Result.success(
            "redirected",
            f"דף הבית מפנה ל-{final_url} — הסריקה תתחיל משם",
            root=normalise(final_url), html=html,
        )
    return Result.success("reachable", f"דף הבית מחזיר {status}",
                          root=normalise(root), html=html)
