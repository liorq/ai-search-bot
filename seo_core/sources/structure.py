"""
Site structure: what the crawl can prove, and what it can only suggest.
======================================================================

Four things come out of one crawl, and they are not equally solid. The module
keeps them apart on purpose, because a report that presents them at the same
confidence is a report that gets the easy wins ignored along with the guesses.

    broken       an internal link to a page that returns 404 or 5xx.
                 A fact. Nothing to argue with, and always worth fixing.

    redirected   an internal link to a URL that redirects. Also a fact, and
                 the chain is counted — one hop is untidy, four hops through
                 two domains is a page that may never be reached at all.

    blocked      a page earning impressions that the site itself tells
                 crawlers to ignore, or marks noindex. A fact with a cause
                 worth asking about before changing anything.

    deep         a page more than three clicks from the front door. A signal,
                 not a fact, and the weakest thing here.

That last distinction matters. Depth does not stop a page being found — a
sitemap does that job — and a page at depth five that ranks perfectly well is
not a problem anybody needs to solve. What depth actually measures is how
much of the site's internal link equity reaches a page, and how far a visitor
has to travel. So a deep page is only raised when it *also* has demand it is
not converting into clicks, and even then it is ranked below the two facts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from ..schema import Finding
from .crawl import Crawl, Link, Trace, normalise
from .queries import CTRCurve, QueryRow

#: The rule of thumb everybody quotes. Kept, but as a threshold for looking
#: rather than as a finding on its own.
MAX_HEALTHY_DEPTH = 3

#: A deep page is only worth raising when somebody is searching for it.
MIN_DEMAND = 200

#: How much of the gap to par is claimed for a structural fix. Low on
#: purpose: shortening the path to a page helps it, but far less reliably
#: than answering the query better does.
STRUCTURAL_RECOVERY = 0.35
REALISTIC_GAIN = 1.5
BEST_CLAIMABLE_POSITION = 3.0

#: Beyond one hop, a redirect is worth its own line rather than a footnote.
CHAIN_THRESHOLD = 2


# ═══════════════════════════════════════════════════════
#  Facts
# ═══════════════════════════════════════════════════════

@dataclass
class BrokenTarget:
    url: str
    status: int
    linked_from: list[str] = field(default_factory=list)
    anchors: list[str] = field(default_factory=list)

    def describe(self) -> str:
        label = "לא נמצא" if self.status == 404 else f"סטטוס {self.status}"
        if self.status == 0:
            label = "לא נענה בכלל"
        return f"{label}, מקושר מ-{len(self.linked_from)} דפים"


def broken_links(crawl: Crawl) -> list[BrokenTarget]:
    """Internal links pointing at a page that does not answer."""
    inbound: dict[str, list[Link]] = {}
    for link in crawl.internal_links:
        inbound.setdefault(link.target, []).append(link)

    found: list[BrokenTarget] = []
    for url, page in crawl.pages.items():
        if page.is_ok or 300 <= page.status < 400:
            continue
        links = inbound.get(url, [])
        if not links:
            continue                      # the front door itself, or a stray
        found.append(BrokenTarget(
            url=url, status=page.status,
            linked_from=sorted({l.source for l in links}),
            anchors=sorted({l.anchor for l in links if l.anchor})[:3],
        ))
    return sorted(found, key=lambda b: len(b.linked_from), reverse=True)


@dataclass
class RedirectedTarget:
    url: str
    trace: Trace
    linked_from: list[str] = field(default_factory=list)

    @property
    def is_chain(self) -> bool:
        return self.trace.hops >= CHAIN_THRESHOLD or self.trace.looped

    def describe(self) -> str:
        return f"{self.trace.describe()}, מקושר מ-{len(self.linked_from)} דפים"


def redirected_links(crawl: Crawl, traces: dict[str, Trace] | None = None
                     ) -> list[RedirectedTarget]:
    """Internal links pointing at a URL that redirects.

    Each one costs a round trip and hands the destination's identity to a URL
    nobody meant to publish. Where a trace is supplied the chain is counted;
    without one the single hop the crawl saw is all that is known.
    """
    inbound: dict[str, list[Link]] = {}
    for link in crawl.internal_links:
        inbound.setdefault(link.target, []).append(link)

    found: list[RedirectedTarget] = []
    for url, page in crawl.pages.items():
        if not page.redirected:
            continue
        links = inbound.get(url, [])
        if not links:
            continue
        known = (traces or {}).get(url) or Trace(chain=list(page.redirect_chain))
        found.append(RedirectedTarget(
            url=url, trace=known,
            linked_from=sorted({l.source for l in links}),
        ))
    return sorted(found, key=lambda r: (r.is_chain, len(r.linked_from)), reverse=True)


@dataclass
class BlockedPage:
    url: str
    reason: str                  # noindex · robots
    impressions: int
    clicks: int

    def describe(self) -> str:
        label = ("הדף מסומן noindex" if self.reason == "noindex"
                 else "הדף חסום ב-robots.txt")
        return (f"{label} ומקבל {self.impressions:,} הופעות "
                f"ו-{self.clicks} קליקים")


def blocked_but_earning(crawl: Crawl, rows: Iterable[QueryRow]) -> list[BlockedPage]:
    """Pages the site tells crawlers to ignore, that people are still finding.

    Usually one of two stories: a noindex left on after a migration, or a page
    somebody deliberately withheld. Both are worth raising, neither is worth
    changing without asking — which is why this reports and never proposes.
    """
    demand: dict[str, dict[str, int]] = {}
    for row in rows:
        entry = demand.setdefault(normalise(row.url), {"impressions": 0, "clicks": 0})
        entry["impressions"] += row.impressions
        entry["clicks"] += row.clicks

    found: list[BlockedPage] = []
    for url, page in crawl.pages.items():
        if page.noindex and url in demand:
            found.append(BlockedPage(url, "noindex", demand[url]["impressions"],
                                     demand[url]["clicks"]))

    for url in crawl.blocked:
        normalised = normalise(url)
        if normalised in demand:
            found.append(BlockedPage(
                normalised, "robots", demand[normalised]["impressions"],
                demand[normalised]["clicks"]))

    return sorted(found, key=lambda b: b.impressions, reverse=True)


# ═══════════════════════════════════════════════════════
#  The signal
# ═══════════════════════════════════════════════════════

@dataclass
class DeepPage:
    url: str
    depth: int
    impressions: int
    clicks: int
    position: float
    potential_clicks: float
    path_hint: str

    def describe(self) -> str:
        return (
            f"{self.depth} קליקים מדף הבית, {self.impressions:,} הופעות "
            f"במיקום {self.position:.1f}"
        )


def _shortest_path(crawl: Crawl, url: str) -> str:
    """A page one click closer than the target, to say where a link could go."""
    page = crawl.pages.get(url)
    if page is None:
        return ""
    for link in crawl.internal_links:
        if link.target != url:
            continue
        source = crawl.pages.get(link.source)
        if source and source.depth < page.depth:
            return source.url
    return ""


def deep_pages(crawl: Crawl, rows: Iterable[QueryRow], curve: CTRCurve
               ) -> list[DeepPage]:
    """Pages far from the front door that are also being searched for.

    Depth on its own is not a finding. A page at depth six that nobody
    searches for is a page nobody searches for, and moving it up the tree
    changes nothing about that.
    """
    demand: dict[str, dict[str, float]] = {}
    for row in rows:
        entry = demand.setdefault(
            normalise(row.url), {"impressions": 0.0, "clicks": 0.0, "weighted": 0.0})
        entry["impressions"] += row.impressions
        entry["clicks"] += row.clicks
        entry["weighted"] += row.position * row.impressions

    found: list[DeepPage] = []
    for url, page in crawl.pages.items():
        if not page.is_ok or page.noindex or page.depth <= MAX_HEALTHY_DEPTH:
            continue
        entry = demand.get(url)
        if not entry or entry["impressions"] < MIN_DEMAND:
            continue

        position = entry["weighted"] / entry["impressions"]
        if position <= BEST_CLAIMABLE_POSITION or position > 20:
            continue

        target = max(BEST_CLAIMABLE_POSITION, position - REALISTIC_GAIN)
        gap = max(0.0, entry["impressions"] * curve.expected(target) - entry["clicks"])
        gain = gap * STRUCTURAL_RECOVERY
        if gain <= 0:
            continue

        found.append(DeepPage(
            url=url, depth=page.depth, impressions=int(entry["impressions"]),
            clicks=int(entry["clicks"]), position=position,
            potential_clicks=gain, path_hint=_shortest_path(crawl, url),
        ))

    return sorted(found, key=lambda d: d.potential_clicks, reverse=True)


# ═══════════════════════════════════════════════════════
#  The report
# ═══════════════════════════════════════════════════════

@dataclass
class Report:
    broken: list[BrokenTarget] = field(default_factory=list)
    redirected: list[RedirectedTarget] = field(default_factory=list)
    blocked: list[BlockedPage] = field(default_factory=list)
    deep: list[DeepPage] = field(default_factory=list)
    max_depth: int = 0
    crawled: int = 0

    @property
    def wasted_links(self) -> int:
        """Internal links that do not arrive where they claim to."""
        return (sum(len(b.linked_from) for b in self.broken)
                + sum(len(r.linked_from) for r in self.redirected))

    def summary(self) -> str:
        return (
            f"{self.crawled} דפים · עומק מרבי {self.max_depth} · "
            f"{len(self.broken)} יעדים שבורים · "
            f"{len(self.redirected)} יעדים שמפנים"
        )


def analyse(crawl: Crawl, rows: list[QueryRow], curve: CTRCurve,
            traces: dict[str, Trace] | None = None) -> Report:
    return Report(
        broken=broken_links(crawl),
        redirected=redirected_links(crawl, traces),
        blocked=blocked_but_earning(crawl, rows),
        deep=deep_pages(crawl, rows, curve),
        max_depth=max((p.depth for p in crawl.pages.values()), default=0),
        crawled=len(crawl.pages),
    )


def to_findings(report: Report, client: str, curve: CTRCurve) -> list[Finding]:
    """The report as ranked findings — facts first, the signal after."""
    findings: list[Finding] = []

    for broken in report.broken:
        findings.append(Finding(
            skill="click-depth", client=client, type="broken_internal_link",
            severity="high" if len(broken.linked_from) > 3 else "medium",
            source="crawl", url=broken.url,
            impact_clicks=0.0,
            impact_basis=(
                f"{len(broken.linked_from)} קישורים פנימיים מובילים לדף "
                f"שמחזיר {broken.status}. אין אומדן קליקים — זו תקלה, "
                "לא הזדמנות"
            ),
            confidence="high",
            confidence_reason="הדף נבדק בסריקה והחזיר את הסטטוס הזה בפועל",
            effort="s",
            evidence={"status": broken.status, "linked_from": broken.linked_from,
                      "anchors": broken.anchors},
            action={"kind": "fix_broken_link", "url": broken.url,
                    "instruction": "תקן את הקישור או הפנה את היעד ב-301",
                    "linked_from": broken.linked_from},
            baseline={"status": broken.status,
                      "inbound_links": len(broken.linked_from)},
        ))

    for redirect in report.redirected:
        if not redirect.is_chain:
            continue                      # a single hop is tidying, not a finding
        findings.append(Finding(
            skill="click-depth", client=client, type="redirect_chain",
            severity="medium", source="crawl", url=redirect.url,
            impact_clicks=0.0,
            impact_basis=(
                f"{redirect.trace.describe()}. כל קפיצה עולה סיבוב נוסף "
                "ומחלישה את הקישור"
            ),
            confidence="high",
            confidence_reason="השרשרת נמדדה קפיצה-קפיצה מול השרת",
            effort="s",
            evidence={"chain": redirect.trace.chain,
                      "hops": redirect.trace.hops,
                      "looped": redirect.trace.looped,
                      "linked_from": redirect.linked_from},
            action={"kind": "shorten_redirect", "url": redirect.url,
                    "instruction": "עדכן את הקישורים ליעד הסופי, "
                                   "והשאר הפניה אחת בלבד",
                    "destination": redirect.trace.destination},
            baseline={"hops": redirect.trace.hops},
        ))

    for blocked in report.blocked:
        findings.append(Finding(
            skill="click-depth", client=client, type="blocked_but_earning",
            severity="high", source="derived", url=blocked.url,
            impact_clicks=0.0,
            impact_basis=blocked.describe() + " — הדף מופיע בתוצאות למרות החסימה",
            confidence="high",
            confidence_reason="הסימון נקרא מהדף עצמו וההופעות מ-Search Console",
            effort="s",
            evidence={"reason": blocked.reason, "impressions": blocked.impressions,
                      "clicks": blocked.clicks},
            action={"kind": "review_indexability", "url": blocked.url,
                    "instruction": "בדוק אם החסימה מכוונת. אם היא שריד "
                                   "מסביבת פיתוח — הסר אותה",
                    "reason": blocked.reason},
            baseline={"reason": blocked.reason, "impressions": blocked.impressions},
        ))

    for deep in report.deep:
        findings.append(Finding(
            skill="click-depth", client=client, type="deep_page_with_demand",
            severity="medium" if deep.potential_clicks >= 30 else "low",
            source="derived", url=deep.url,
            impact_clicks=round(deep.potential_clicks, 1),
            impact_basis=(
                f"{deep.describe()}. קיצור הדרך אליו שווה כ-"
                f"{deep.potential_clicks:.0f} קליקים לפי "
                f"{STRUCTURAL_RECOVERY:.0%} מהפער"
            ),
            confidence="low",
            confidence_reason=(
                "עומק הוא סימן ולא סיבה — sitemap דואג לגילוי, והקשר בין "
                "עומק לדירוג עקיף. שווה קיצור, לא הבטחה"
            ),
            effort="s",
            evidence={"depth": deep.depth, "impressions": deep.impressions,
                      "clicks": deep.clicks, "position": round(deep.position, 1),
                      "curve_source": curve.source,
                      "nearest_shallower_page": deep.path_hint},
            action={"kind": "shorten_path", "url": deep.url,
                    "instruction": "הוסף קישור מדף רדוד יותר — "
                                   "`internal-anchors` יודע למצוא משפט קיים",
                    "from_page": deep.path_hint},
            baseline={"depth": deep.depth, "clicks": deep.clicks,
                      "impressions": deep.impressions},
        ))

    return findings
