"""
Grouping the queries a site already has into topics, and keeping the map.
========================================================================

`keyword-research` finds words a client does not have yet. This does the other
job: it takes the queries Search Console already reports and works out what
structure they imply — which page owns a topic, which subtopic has outgrown
the page it is buried in, and which topic is split across two pages that both
half-answer it.

**Topics are grouped by the URL Google chose, not by the words.**

Lexical clustering — "these two queries share the word springs, so they are
the same topic" — merges `spring repair cost` with `spring water delivery`
and separates `garage door won't close` from `broken torsion spring`, which
are the same intent to any searcher. Google has already done the hard part:
when it ranks the same page for two queries, it has decided they are answered
by the same content. That decision is in the export.

Where a query ranks on a page that also ranks for a query in another group,
the two groups are one topic. Words are used only to name a cluster once it
exists, never to build one.

**The map persists.** Running this again next month against the same client
should not produce a fresh map with no memory: it should say what is new,
what moved, and what disappeared. So the map is stored, merged, and the
difference is the output that matters on every run after the first.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from ..log import count
from ..schema import Finding, Result
from .crawl import normalise
from .queries import CTRCurve, MIN_IMPRESSIONS, QueryRow, _STOPWORDS, _WORD

MAP_FILE = "topic_map.json"

#: A query needs this many impressions before it shapes a site's structure.
#: Lower than the toolkit's finding floor, because a topic is built from many
#: small queries and dropping all of them would leave only the head terms.
CLUSTER_MIN_IMPRESSIONS = 20

#: A cluster below this is a query, not a topic, and does not need a strategy.
MIN_CLUSTER_QUERIES = 3

#: A subtopic buried in a page that cannot answer it: enough demand to earn a
#: page of its own, and far enough down to show the current page is not doing
#: the job.
SUBTOPIC_MIN_IMPRESSIONS = 300
SUBTOPIC_MIN_POSITION = 10.0

#: Where a dedicated page can reasonably land. Deliberately modest: a new page
#: starts with no links and no history.
NEW_PAGE_POSITION = 8.0

#: Two pages each taking a real share of one topic is a structure problem
#: rather than a query-level one.
SPLIT_MIN_SHARE = 0.25

#: How much of a topic's queries have to be the ones we saw last time before
#: it is the same topic under a new name.
#:
#: A cluster's label is generated from the words its queries agree on, so
#: adding one query can rename it — "spring garage door" becomes "spring
#: torsion door". Keying the stored map on that name made a topic look brand
#: new every time it grew, and made its predecessor look as though it had
#: vanished. Identity comes from the queries instead; the label is free to
#: drift.
SAME_TOPIC_OVERLAP = 0.5

#: Words that name nothing on their own, on top of the shared stop list.
_WEAK_LABEL_WORDS = {
    "cost", "price", "near", "best", "top", "cheap", "free", "service",
    "services", "company", "repair", "מחיר", "עלות", "שירות", "חברה",
}


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _terms(text: str) -> list[str]:
    return [w for w in _WORD.findall(text.lower()) if w not in _STOPWORDS]


def slugify(label: str) -> str:
    cleaned = re.sub(r"[^\w֐-׿]+", "-", label.lower()).strip("-")
    return cleaned or "topic"


# ═══════════════════════════════════════════════════════
#  Building the clusters
# ═══════════════════════════════════════════════════════

@dataclass
class Cluster:
    """One topic: the queries in it, and the pages currently answering them."""

    label: str
    queries: list[QueryRow] = field(default_factory=list)

    @property
    def slug(self) -> str:
        return slugify(self.label)

    @property
    def clicks(self) -> int:
        return sum(q.clicks for q in self.queries)

    @property
    def impressions(self) -> int:
        return sum(q.impressions for q in self.queries)

    @property
    def urls(self) -> dict[str, int]:
        """Every page answering part of this topic, by impressions."""
        totals: dict[str, int] = {}
        for query in self.queries:
            url = normalise(query.url)
            totals[url] = totals.get(url, 0) + query.impressions
        return dict(sorted(totals.items(), key=lambda kv: kv[1], reverse=True))

    @property
    def pillar(self) -> str:
        """The page that should own the topic: most clicks, then most reach."""
        by_url: dict[str, tuple[int, int]] = {}
        for query in self.queries:
            url = normalise(query.url)
            clicks, impressions = by_url.get(url, (0, 0))
            by_url[url] = (clicks + query.clicks, impressions + query.impressions)
        return max(by_url, key=lambda u: by_url[u], default="")

    @property
    def is_split(self) -> bool:
        """Whether a second page holds a real share of the topic."""
        totals = list(self.urls.values())
        if len(totals) < 2 or not self.impressions:
            return False
        return totals[1] / self.impressions >= SPLIT_MIN_SHARE

    def subtopics(self) -> list[QueryRow]:
        """Queries with their own demand that the pillar is not answering."""
        return sorted(
            (q for q in self.queries
             if q.impressions >= SUBTOPIC_MIN_IMPRESSIONS
             and q.position >= SUBTOPIC_MIN_POSITION),
            key=lambda q: q.impressions, reverse=True,
        )

    def summary(self) -> str:
        return (
            f"{count(len(self.queries), 'שאילתה אחת', 'שאילתות')} · "
            f"{self.impressions:,} הופעות · {self.clicks:,} קליקים · "
            f"{count(len(self.urls), 'דף אחד', 'דפים')}"
        )


def _label_for(rows: Iterable[QueryRow]) -> str:
    """Name a cluster from the words its queries agree on.

    Words are used here and nowhere else: naming a group is a presentation
    problem, and getting the name slightly wrong costs nothing. Building the
    group from words would cost correctness.
    """
    rows = list(rows)
    counts: Counter[str] = Counter()
    for row in rows:
        weight = max(1, row.impressions)
        for term in set(_terms(row.query)):
            counts[term] += weight

    strong = [t for t, _ in counts.most_common(8) if t not in _WEAK_LABEL_WORDS]
    if not strong:
        strong = [t for t, _ in counts.most_common(2)]
    if not strong:
        return max(rows, key=lambda r: r.impressions, default=None).query

    return " ".join(strong[:3])


def build(rows: Iterable[QueryRow]) -> list[Cluster]:
    """Group queries into topics using the page Google chose for each.

    Two pages belong to one topic when a query ranks on one of them while
    another query in the same group ranks on the other. The union-find below
    is doing one thing: following those shared queries until nothing more
    joins.
    """
    material = [r for r in rows if r.impressions >= CLUSTER_MIN_IMPRESSIONS]
    if not material:
        return []

    # Every URL starts as its own topic.
    parent: dict[str, str] = {}

    def find(url: str) -> str:
        parent.setdefault(url, url)
        while parent[url] != url:
            parent[url] = parent[parent[url]]
            url = parent[url]
        return url

    def union(a: str, b: str) -> None:
        root_a, root_b = find(a), find(b)
        if root_a != root_b:
            parent[root_b] = root_a

    by_query: dict[str, list[str]] = {}
    for row in material:
        by_query.setdefault(row.query, []).append(normalise(row.url))

    for url in {normalise(r.url) for r in material}:
        find(url)
    for urls in by_query.values():
        for other in urls[1:]:
            union(urls[0], other)

    grouped: dict[str, list[QueryRow]] = {}
    for row in material:
        grouped.setdefault(find(normalise(row.url)), []).append(row)

    clusters = [
        Cluster(label=_label_for(rows), queries=sorted(
            rows, key=lambda r: r.impressions, reverse=True))
        for rows in grouped.values()
        if len({r.query for r in rows}) >= MIN_CLUSTER_QUERIES
    ]
    return sorted(clusters, key=lambda c: c.impressions, reverse=True)


# ═══════════════════════════════════════════════════════
#  The map that remembers
# ═══════════════════════════════════════════════════════

@dataclass
class Delta:
    """What changed since the last run — the point of storing the map."""

    new_clusters: list[str] = field(default_factory=list)
    new_queries: dict[str, list[str]] = field(default_factory=dict)
    lost_queries: dict[str, list[str]] = field(default_factory=dict)
    moved_pillars: list[tuple[str, str, str]] = field(default_factory=list)
    renamed: list[tuple[str, str, str]] = field(default_factory=list)
    first_run: bool = False

    @property
    def is_empty(self) -> bool:
        return not (self.new_clusters or self.new_queries
                    or self.lost_queries or self.moved_pillars)

    def summary(self) -> str:
        if self.first_run:
            return "מיפוי ראשון — אין מול מה להשוות"
        if self.is_empty:
            return "אין שינוי מאז המיפוי הקודם"
        return (
            f"{count(len(self.new_clusters), 'נושא חדש אחד', 'נושאים חדשים')} · "
            f"{count(sum(len(v) for v in self.new_queries.values()), 'שאילתה חדשה אחת', 'שאילתות חדשות')} · "
            f"{count(sum(len(v) for v in self.lost_queries.values()), 'אחת שנעלמה', 'שנעלמו')} · "
            f"{count(len(self.moved_pillars), 'פילר אחד שהתחלף', 'דפי פילר שהתחלפו')}"
        )


def load_map(directory: Path) -> dict[str, Any]:
    path = directory / MAP_FILE
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_map(payload: dict[str, Any], directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / MAP_FILE
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    return path


def _match_existing(previous: dict[str, Any], cluster: Cluster,
                    taken: set[str]) -> str | None:
    """Find the stored topic this cluster is a later version of.

    Matched on shared queries rather than on the label, so a topic that grew
    a query and changed its generated name is still recognised as itself.
    """
    queries = {q.query for q in cluster.queries}
    best, best_score = None, 0.0

    for slug, stored in previous.items():
        if slug in taken:
            continue
        before = set(stored.get("queries") or {})
        if not before:
            continue
        overlap = len(queries & before) / min(len(queries), len(before))
        if overlap > best_score:
            best, best_score = slug, overlap

    return best if best_score >= SAME_TOPIC_OVERLAP else None


def merge(existing: dict[str, Any], clusters: list[Cluster], client: str
          ) -> tuple[dict[str, Any], Delta]:
    """Fold this run into the stored map and say what moved.

    A query that stops appearing is kept with its `last_seen` date rather than
    deleted. A topic that quietly disappeared is the most interesting thing a
    second run can tell anyone, and deleting the row would delete the finding.
    """
    now = _utcnow()
    previous = existing.get("clusters") or {}
    delta = Delta(first_run=not previous)

    merged: dict[str, Any] = {}
    taken: set[str] = set()
    for cluster in clusters:
        slug = _match_existing(previous, cluster, taken)
        if slug is None:
            slug = cluster.slug
            while slug in merged or slug in previous:
                slug = f"{slug}-2"
        taken.add(slug)

        before = previous.get(slug) or {}
        before_queries = before.get("queries") or {}

        if not before:
            delta.new_clusters.append(slug)
        elif before.get("label") and before["label"] != cluster.label:
            delta.renamed.append((slug, before["label"], cluster.label))

        queries: dict[str, Any] = dict(before_queries)
        appeared: list[str] = []
        for row in cluster.queries:
            seen = queries.get(row.query)
            if seen is None:
                appeared.append(row.query)
            queries[row.query] = {
                "first_seen": (seen or {}).get("first_seen", now),
                "last_seen": now,
                "url": normalise(row.url),
                "clicks": row.clicks,
                "impressions": row.impressions,
                "position": round(row.position, 1),
            }
        if appeared and before:
            delta.new_queries[slug] = appeared

        gone = [q for q in before_queries
                if q not in {r.query for r in cluster.queries}]
        if gone:
            delta.lost_queries[slug] = gone

        pillar = cluster.pillar
        if before.get("pillar") and before["pillar"] != pillar:
            delta.moved_pillars.append((slug, before["pillar"], pillar))

        merged[slug] = {
            "label": cluster.label,
            "pillar": pillar,
            "created_at": before.get("created_at", now),
            "updated_at": now,
            "impressions": cluster.impressions,
            "clicks": cluster.clicks,
            "is_split": cluster.is_split,
            "pages": cluster.urls,
            "queries": queries,
        }

    # A whole topic that stopped appearing keeps its record, unchanged.
    for slug, before in previous.items():
        if slug not in merged:
            merged[slug] = before
            delta.lost_queries.setdefault(slug, list(before.get("queries") or {}))

    payload = {
        "client": client,
        "updated_at": now,
        "runs": int(existing.get("runs") or 0) + 1,
        "clusters": merged,
    }
    return payload, delta


def load_and_merge(directory: Path, clusters: list[Cluster], client: str
                   ) -> Result:
    """Read the stored map, fold this run in, write it back."""
    payload, delta = merge(load_map(directory), clusters, client)
    path = save_map(payload, directory)
    return Result.success(
        "merged",
        f"מפת הנושאים עודכנה — ריצה מספר {payload['runs']}",
        path=path, payload=payload, delta=delta,
    )


# ═══════════════════════════════════════════════════════
#  Findings
# ═══════════════════════════════════════════════════════

def _gain(impressions: int, clicks: int, target: float, curve: CTRCurve) -> float:
    return max(0.0, impressions * curve.expected(target) - clicks)


def to_findings(clusters: list[Cluster], client: str, curve: CTRCurve
                ) -> list[Finding]:
    """Two structural findings per topic: a missing page, and a split one."""
    findings: list[Finding] = []

    for cluster in clusters:
        for subtopic in cluster.subtopics()[:3]:
            gain = _gain(subtopic.impressions, subtopic.clicks,
                         NEW_PAGE_POSITION, curve)
            if gain <= 0:
                continue
            findings.append(Finding(
                skill="topic-cluster", client=client, type="subtopic_needs_page",
                severity="medium" if gain >= 30 else "low",
                source="derived", url=normalise(subtopic.url),
                query=subtopic.query,
                impact_clicks=round(gain, 1),
                impact_basis=(
                    f"“{subtopic.query}” מביאה "
                    f"{subtopic.impressions:,} הופעות במיקום "
                    f"{subtopic.position:.1f}, קבורה בתוך {cluster.pillar}. "
                    f"דף ייעודי במיקום {NEW_PAGE_POSITION:.0f} שווה "
                    f"כ-{gain:.0f} קליקים"
                ),
                confidence="low",
                confidence_reason=(
                    "דף חדש מתחיל בלי קישורים ובלי היסטוריה, והאומדן מניח "
                    "שהוא יגיע לעמוד הראשון — זו הנחה, לא מדידה"
                ),
                effort="l",
                evidence={
                    "cluster": cluster.slug,
                    "impressions": subtopic.impressions,
                    "clicks": subtopic.clicks,
                    "position": round(subtopic.position, 1),
                    "currently_on": normalise(subtopic.url),
                    "curve_source": curve.source,
                },
                action={
                    "kind": "create_supporting_page",
                    "query": subtopic.query,
                    "instruction": (
                        "דף תומך שעונה על השאילתה הזו במלואה, מקושר מדף "
                        "הפילר ומקשר אליו חזרה"
                    ),
                    "pillar": cluster.pillar,
                },
                baseline={"impressions": subtopic.impressions,
                          "clicks": subtopic.clicks,
                          "position": round(subtopic.position, 1)},
            ))

        if cluster.is_split:
            pages = list(cluster.urls)
            gain = _gain(cluster.impressions, cluster.clicks,
                         max(1.0, _weighted_position(cluster) - 2.0), curve) * 0.5
            findings.append(Finding(
                skill="topic-cluster", client=client, type="topic_split",
                severity="medium", source="derived", url=cluster.pillar,
                impact_clicks=round(gain, 1),
                impact_basis=(
                    f"הנושא “{cluster.label}” מפוזר על "
                    f"{len(pages)} דפים, והשני מחזיק לפחות "
                    f"{SPLIT_MIN_SHARE:.0%} מההופעות. איחוד סביב "
                    f"{cluster.pillar} מרכז את הסמכות"
                ),
                confidence="low",
                confidence_reason=(
                    "איחוד נושא הוא החלטת תוכן שדורשת קריאה של שני הדפים. "
                    "האומדן מניח מחצית מהפער בלבד"
                ),
                effort="l",
                evidence={"cluster": cluster.slug, "pages": cluster.urls,
                          "impressions": cluster.impressions,
                          "clicks": cluster.clicks},
                action={"kind": "consolidate_topic", "pillar": cluster.pillar,
                        "instruction": (
                            "החלט איזה דף מוביל את הנושא, והפוך את השאר "
                            "לדפים תומכים שמקשרים אליו"
                        ),
                        "pages": pages},
                baseline={"pages": len(pages), "clicks": cluster.clicks,
                          "impressions": cluster.impressions},
            ))

    return findings


def _weighted_position(cluster: Cluster) -> float:
    if not cluster.impressions:
        return 0.0
    return sum(q.position * q.impressions for q in cluster.queries) / cluster.impressions
