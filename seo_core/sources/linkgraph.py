"""
The internal link graph: which pages the site supports, and which it forgets.
============================================================================

Every site has pages that earn impressions and get nothing from the rest of
the site — no link from a relevant page, or a link whose anchor text says
"click here". And every site has the opposite: a page linked from the footer
of four hundred pages that has nothing to rank for.

Two numbers are computed here, and they mean different things:

    pagerank            over every followed internal link, because that is
                        what a search engine sees — menus included
    editorial inbound   over links that are not carried by half the site,
                        because that is what somebody actually chose

A page with forty inbound links from the footer has one editorial vote, not
forty, and a report that cannot tell the difference recommends nothing useful.

One thing this module is careful about: **a crawl cannot find an orphan.** A
page with no inbound link at all is a page the crawl never reaches, so it is
absent from the results rather than flagged in them. Orphans are found by
comparing the crawl against a list of URLs from somewhere else — Search
Console, or the sitemap — and never from the crawl alone.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

from ..schema import Finding
from .crawl import Crawl, Link, normalise
from .queries import CTRCurve, QueryRow, _STOPWORDS, _WORD

#: Standard damping. The exact value barely matters for ranking pages against
#: each other, which is all it is used for here.
DAMPING = 0.85
ITERATIONS = 40

#: What an internal link is worth in places. Deliberately smaller than the
#: three places an on-page rewrite claims: internal linking moves a page that
#: is already close, and it does not make a page answer a question better.
REALISTIC_GAIN = 1.5

#: Where a page can land off the back of internal support alone.
BEST_CLAIMABLE_POSITION = 3.0

#: A page is under-supported when it is in the bottom slice of the site's
#: editorial inbound links while being in the top slice of its demand.
#:
#: Support is counted in editorial links rather than in PageRank on purpose.
#: PageRank across a site whose pages all link onward in the same pattern is
#: nearly flat, so a quantile cut through it separates pages by rounding
#: error. The count is also the thing that can be changed: the recommendation
#: is to add a link, not to raise a number. PageRank stays in the evidence,
#: where it says something about the whole site rather than about one page.
LOW_SUPPORT_QUANTILE = 0.35
HIGH_DEMAND_QUANTILE = 0.65

#: However the site's own distribution falls, a page with this many pages
#: pointing at it on purpose is supported.
WELL_SUPPORTED = 10

#: "Top third of the site's demand" says nothing when the site has three
#: pages with demand, so the bar is absolute as well as relative. At 1.5
#: places of modelled movement, a page below this earns a handful of clicks
#: either way and the estimate is smaller than the month-to-month noise.
MIN_DEMAND = 300

#: And the same in the other unit: a modelled gain this small is not worth a
#: line in a work queue, whatever the impressions behind it.
MIN_CLAIMABLE_CLICKS = 5.0

#: Under this many crawled pages the quantile comparison is meaningless.
MIN_PAGES_FOR_QUANTILES = 15

#: An anchor this long is a sentence that happens to be linked, not an anchor.
MAX_ANCHOR_WORDS = 12


# ═══════════════════════════════════════════════════════
#  The graph
# ═══════════════════════════════════════════════════════

@dataclass
class Node:
    url: str
    depth: int = 0
    inbound: list[Link] = field(default_factory=list)
    editorial_inbound: list[Link] = field(default_factory=list)
    outbound: list[Link] = field(default_factory=list)
    pagerank: float = 0.0
    noindex: bool = False

    @property
    def anchors(self) -> list[str]:
        return [link.anchor for link in self.editorial_inbound if link.anchor]

    @property
    def generic_share(self) -> float:
        """Share of the editorial inbound links whose anchor says nothing."""
        useful = [l for l in self.editorial_inbound if not l.is_empty]
        if not useful:
            return 0.0
        return sum(1 for l in useful if l.is_generic) / len(useful)


@dataclass
class Graph:
    nodes: dict[str, Node] = field(default_factory=dict)
    root: str = ""

    def get(self, url: str) -> Node | None:
        return self.nodes.get(normalise(url))

    def ranked(self) -> list[Node]:
        return sorted(self.nodes.values(), key=lambda n: n.pagerank, reverse=True)

    def orphans(self, known_urls: Iterable[str]) -> list[str]:
        """URLs the site has but the crawl never reached from the front door.

        This is the only way to find them. A page with no inbound link cannot
        turn up in a crawl that follows links, so its absence from the crawl
        *is* the finding — which is why a separate list of URLs is required
        rather than optional.
        """
        crawled = {normalise(url) for url, node in self.nodes.items()
                   if node.inbound or url == self.root}
        return sorted(
            normalise(url) for url in known_urls
            if normalise(url) not in crawled
        )

    def unsupported(self) -> list[Node]:
        """Crawled pages reached only by navigation — no page chose to link."""
        return sorted(
            (n for n in self.nodes.values()
             if n.inbound and not n.editorial_inbound and n.url != self.root),
            key=lambda n: n.depth,
        )

    def summary(self) -> str:
        editorial = sum(len(n.editorial_inbound) for n in self.nodes.values())
        return (
            f"{len(self.nodes)} דפים בגרף · {editorial} קישורים עריכתיים · "
            f"{len(self.unsupported())} דפים בלי אף קישור עריכתי"
        )


def build(crawl: Crawl) -> Graph:
    """Turn a crawl into a graph with link equity distributed over it."""
    graph = Graph(root=crawl.root)
    for url, page in crawl.pages.items():
        graph.nodes[url] = Node(url=url, depth=page.depth, noindex=page.noindex)

    boilerplate = crawl.boilerplate_keys()
    for link in crawl.internal_links:
        source, target = graph.nodes.get(link.source), graph.nodes.get(link.target)
        if source is None or target is None or link.source == link.target:
            continue
        source.outbound.append(link)
        target.inbound.append(link)
        if link.key not in boilerplate and not link.nofollow:
            target.editorial_inbound.append(link)

    _pagerank(graph)
    return graph


def _pagerank(graph: Graph) -> None:
    """Distribute link equity over the followed internal links.

    Menu links are included on purpose. This number answers "what does a
    crawler see", and a crawler sees the menu. The editorial count answers
    the other question.
    """
    nodes = list(graph.nodes.values())
    if not nodes:
        return

    count = len(nodes)
    rank = {node.url: 1.0 / count for node in nodes}
    outgoing = {
        node.url: {l.target for l in node.outbound
                   if not l.nofollow and l.target in graph.nodes}
        for node in nodes
    }

    for _ in range(ITERATIONS):
        # A page with no outgoing links would otherwise swallow equity, so its
        # share is redistributed evenly — the standard treatment of a sink.
        sink = sum(rank[url] for url, targets in outgoing.items() if not targets)
        nxt = {url: (1 - DAMPING) / count + DAMPING * sink / count for url in rank}
        for url, targets in outgoing.items():
            if not targets:
                continue
            share = DAMPING * rank[url] / len(targets)
            for target in targets:
                nxt[target] += share
        rank = nxt

    for node in nodes:
        node.pagerank = rank[node.url]


# ═══════════════════════════════════════════════════════
#  Anchors
# ═══════════════════════════════════════════════════════

@dataclass
class AnchorProblem:
    kind: str                    # generic · empty · off_topic · over_long
    url: str
    anchors: list[str]
    detail: str


def anchor_problems(graph: Graph, page_terms: dict[str, set[str]] | None = None
                    ) -> list[AnchorProblem]:
    """Inbound anchor text that tells Google nothing about the destination."""
    problems: list[AnchorProblem] = []

    for node in graph.nodes.values():
        links = node.editorial_inbound
        if not links:
            continue

        generic = [l.anchor for l in links if l.is_generic]
        if generic and node.generic_share >= 0.5:
            problems.append(AnchorProblem(
                "generic", node.url, generic,
                f"{len(generic)} מתוך {len(links)} הקישורים העריכתיים לדף נושאים "
                f"אנקור שלא אומר לאן הוא מוביל: {', '.join(sorted(set(generic))[:3])}",
            ))

        empty = [l for l in links if l.is_empty]
        if empty:
            problems.append(AnchorProblem(
                "empty", node.url, [],
                f"{len(empty)} קישורים לדף בלי טקסט ובלי alt — "
                "קישור תמונה שגוגל לא יודע לקרוא",
            ))

        long_ones = [l.anchor for l in links
                     if len(l.anchor.split()) > MAX_ANCHOR_WORDS]
        if long_ones:
            problems.append(AnchorProblem(
                "over_long", node.url, long_ones[:2],
                f"{len(long_ones)} אנקורים באורך משפט שלם — כנראה כל הפסקה עטופה בקישור",
            ))

        terms = (page_terms or {}).get(node.url)
        if terms and links:
            words = {w for l in links for w in _WORD.findall(l.anchor.lower())}
            if not (terms & words):
                problems.append(AnchorProblem(
                    "off_topic", node.url, [l.anchor for l in links[:3]],
                    "אף אנקור נכנס לדף לא מכיל מילה מהשאילתות שהדף מדורג עליהן",
                ))

    return problems


# ═══════════════════════════════════════════════════════
#  Where the support does not match the demand
# ═══════════════════════════════════════════════════════

@dataclass
class SupportGap:
    """A page the site's own links do not back up, and what that costs."""

    url: str
    impressions: int
    clicks: int
    position: float
    pagerank: float
    editorial_inbound: int
    depth: int
    potential_clicks: float
    basis: str

    @property
    def rank(self) -> float:
        return self.potential_clicks


def _quantile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(len(ordered) * fraction)))
    return ordered[index]


def find_support_gaps(
    graph: Graph, rows: Iterable[QueryRow], curve: CTRCurve
) -> list[SupportGap]:
    """Pages with real demand and almost none of the site's internal equity.

    Both halves are required. A page with no internal links and no impressions
    is not a missed opportunity, it is a page nobody searches for, and adding
    links to it moves nothing.
    """
    if len(graph.nodes) < MIN_PAGES_FOR_QUANTILES:
        return []

    demand: dict[str, dict[str, float]] = {}
    for row in rows:
        url = normalise(row.url)
        entry = demand.setdefault(
            url, {"impressions": 0.0, "clicks": 0.0, "weighted": 0.0})
        entry["impressions"] += row.impressions
        entry["clicks"] += row.clicks
        entry["weighted"] += row.position * row.impressions

    support = [float(len(n.editorial_inbound)) for n in graph.nodes.values()]
    low_support = min(_quantile(support, LOW_SUPPORT_QUANTILE), WELL_SUPPORTED - 1)
    volumes = [d["impressions"] for d in demand.values()]
    high_demand = _quantile(volumes, HIGH_DEMAND_QUANTILE)

    gaps: list[SupportGap] = []
    for url, entry in demand.items():
        node = graph.nodes.get(url)
        if node is None or node.noindex:
            continue
        if entry["impressions"] < max(MIN_DEMAND, high_demand):
            continue
        if len(node.editorial_inbound) > low_support:
            continue

        position = entry["weighted"] / entry["impressions"]
        if position <= BEST_CLAIMABLE_POSITION or position > 20:
            continue

        target = max(BEST_CLAIMABLE_POSITION, position - REALISTIC_GAIN)
        gain = max(0.0, entry["impressions"] * curve.expected(target) - entry["clicks"])
        if gain < MIN_CLAIMABLE_CLICKS:
            continue

        gaps.append(SupportGap(
            url=url, impressions=int(entry["impressions"]),
            clicks=int(entry["clicks"]), position=position,
            pagerank=node.pagerank, editorial_inbound=len(node.editorial_inbound),
            depth=node.depth, potential_clicks=gain,
            basis=(
                f"{int(entry['impressions']):,} הופעות במיקום {position:.1f}, "
                f"אבל {len(node.editorial_inbound)} קישורים עריכתיים בלבד — "
                f"השליש התחתון של האתר. "
                f"עלייה של {REALISTIC_GAIN:.1f} מקומות שווה כ-{gain:.0f} קליקים"
            ),
        ))

    return sorted(gaps, key=lambda g: g.rank, reverse=True)


# ═══════════════════════════════════════════════════════
#  Proposals — a link that fits a sentence already written
# ═══════════════════════════════════════════════════════

@dataclass
class Proposal:
    source: str
    target: str
    phrase: str
    source_depth: int
    source_pagerank: float

    def command(self, script: str, client: str) -> str:
        return (
            f"python {script} --mode plan --client {client} "
            f"--from {self.source} --to {self.target} --phrase \"{self.phrase}\""
        )


def propose(
    crawl: Crawl, graph: Graph, target: str, phrases: Iterable[str],
    *, limit: int = 5,
) -> list[Proposal]:
    """Pages that already say the phrase and do not yet link to the target.

    The best internal link is one that was almost written already: a sentence
    a human wrote, containing the words the destination ranks for. Nothing is
    added to the page and nothing is rewritten — the phrase is wrapped.

    Sources are ordered by their own link equity, because a link from a page
    the site actually supports is worth more than a link from one it does not.
    """
    target = normalise(target)
    wanted = [p.strip() for p in phrases if p.strip()]
    found: list[Proposal] = []

    for url, page in crawl.pages.items():
        if url == target or not page.is_ok or page.noindex:
            continue
        if any(link.target == target for link in page.links):
            continue

        body = page.text.lower()
        match = next((p for p in wanted
                      if re.search(rf"\b{re.escape(p.lower())}\b", body)), None)
        if not match:
            continue

        node = graph.nodes.get(url)
        found.append(Proposal(
            source=url, target=target, phrase=match,
            source_depth=page.depth,
            source_pagerank=node.pagerank if node else 0.0,
        ))

    return sorted(found, key=lambda p: p.source_pagerank, reverse=True)[:limit]


def phrases_for(rows: Iterable[QueryRow], url: str, limit: int = 6) -> list[str]:
    """The queries a page ranks for, as candidate anchor text.

    An anchor that matches what the destination is trying to rank for is the
    entire point; an anchor of "click here" is the failure this replaces.
    """
    target = normalise(url)
    ordered = sorted(
        (r for r in rows if normalise(r.url) == target),
        key=lambda r: r.impressions, reverse=True,
    )
    seen: list[str] = []
    for row in ordered:
        phrase = row.query.strip()
        meaningful = [w for w in _WORD.findall(phrase.lower())
                      if w not in _STOPWORDS]
        if len(meaningful) >= 2 and phrase not in seen:
            seen.append(phrase)
        if len(seen) >= limit:
            break
    return seen


# ═══════════════════════════════════════════════════════
#  Findings
# ═══════════════════════════════════════════════════════

_SEVERITY_FLOOR = ((100, "high"), (30, "medium"))


def _severity(potential: float) -> str:
    for floor, level in _SEVERITY_FLOOR:
        if potential >= floor:
            return level
    return "low"


def gap_to_finding(
    gap: SupportGap, client: str, curve: CTRCurve, proposals: list[Proposal],
    conversion_rate: float | None = None,
) -> Finding:
    """One under-supported page as a ranked, evidence-carrying finding."""
    potential = round(gap.potential_clicks, 1)
    confidence = "low" if curve.source != "site" or not proposals else "medium"
    reason = (
        "אין מספיק נתונים לעקומת CTR של האתר, אז האומדן נשען על ממוצע תעשייה"
        if curve.source != "site" else
        "לא נמצא דף מקור שכבר מזכיר את הביטוי — בלי משפט קיים לקשר, "
        "הקישור יצטרך להיכתב ידנית"
        if not proposals else
        f"{len(proposals)} דפים באתר כבר מזכירים את הביטוי בטקסט. "
        f"קישור פנימי מזיז דף שכבר קרוב, ולא הופך דף לתשובה טובה יותר"
    )

    return Finding(
        skill="internal-anchors",
        client=client,
        type="under_supported_page",
        severity=_severity(potential),
        source="derived",
        url=gap.url,
        impact_clicks=potential,
        impact_conversions=(
            round(potential * conversion_rate, 2) if conversion_rate else None),
        impact_basis=gap.basis,
        confidence=confidence,
        confidence_reason=reason,
        effort="s",
        evidence={
            "impressions": gap.impressions,
            "clicks": gap.clicks,
            "position": round(gap.position, 1),
            "editorial_inbound": gap.editorial_inbound,
            "pagerank": round(gap.pagerank, 6),
            "depth": gap.depth,
            "curve_source": curve.source,
        },
        action={
            "kind": "add_internal_links",
            "url": gap.url,
            "instruction": (
                "עטוף ביטוי קיים בדף מקור רלוונטי בקישור לדף הזה — "
                "בלי להוסיף משפט ובלי לשכתב"
            ),
            "proposals": [
                {"from": p.source, "phrase": p.phrase} for p in proposals
            ],
        },
        baseline={
            "position": round(gap.position, 1),
            "clicks": gap.clicks,
            "impressions": gap.impressions,
            "editorial_inbound": gap.editorial_inbound,
        },
    )
