"""
Gate 1 — what a change puts at risk.
====================================

Before any edit, the page's existing rankings are inventoried. A query the
page already earns clicks from, or sits near the top for, is a *protected
query*: an asset the change must not damage.

The interesting case is not the target query — it is the one nobody was
thinking about. A page ranking third for a phrase that happens to live in the
heading we are about to rewrite has more to lose than the new phrase has to
gain, and only an explicit inventory surfaces that before the write.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

#: A query is protected if it earns clicks, or ranks close enough that small
#: movements are still worth real traffic.
PROTECTED_MAX_POSITION = 15.0
PROTECTED_MIN_CLICKS   = 1

#: Below this, month-to-month noise exceeds any effect we could measure, so the
#: query is tracked but never blocks a change on its own.
MATERIAL_MIN_IMPRESSIONS = 50

RiskLevel = Literal["red", "amber", "green"]

_WORD = re.compile(r"[\w֐-׿]+", re.UNICODE)

#: Short function words carry no topical weight; treating them as overlap would
#: make every edit look risky.
_STOPWORDS = {
    "the", "a", "an", "and", "or", "for", "to", "of", "in", "on", "at", "is",
    "are", "my", "your", "near", "me", "best", "how", "what", "with",
    "של", "את", "עם", "על", "לי", "מה", "איך", "הכי",
}


@dataclass
class ProtectedQuery:
    query: str
    clicks: int
    impressions: int
    position: float

    @property
    def is_material(self) -> bool:
        return self.impressions >= MATERIAL_MIN_IMPRESSIONS or self.clicks > 0

    @property
    def terms(self) -> set[str]:
        return {
            w.lower() for w in _WORD.findall(self.query)
            if w.lower() not in _STOPWORDS and len(w) > 2
        }


@dataclass
class RiskReport:
    level: RiskLevel
    protected: list[ProtectedQuery] = field(default_factory=list)
    at_risk: list[ProtectedQuery] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    @property
    def needs_double_approval(self) -> bool:
        return self.level == "red"

    def summary(self) -> str:
        if self.level == "green":
            return f"אין סיכון מזוהה · {len(self.protected)} שאילתות מוגנות בדף"
        listed = ", ".join(q.query for q in self.at_risk[:3])
        more = f" ועוד {len(self.at_risk) - 3}" if len(self.at_risk) > 3 else ""
        return f"{len(self.at_risk)} שאילתות מוגנות בסיכון: {listed}{more}"


def protected_queries(rows: list[dict[str, Any]]) -> list[ProtectedQuery]:
    """Pick out the queries this page cannot afford to lose.

    `rows` are Search Console query rows for one page.
    """
    found: list[ProtectedQuery] = []
    for row in rows:
        clicks = int(row.get("clicks", 0) or 0)
        position = float(row.get("position", 99) or 99)
        if clicks >= PROTECTED_MIN_CLICKS or position <= PROTECTED_MAX_POSITION:
            found.append(
                ProtectedQuery(
                    query=str(row.get("query", "")),
                    clicks=clicks,
                    impressions=int(row.get("impressions", 0) or 0),
                    position=position,
                )
            )
    return sorted(found, key=lambda q: (-q.clicks, q.position))


def assess(
    removed_text: str,
    protected: list[ProtectedQuery],
    target_query: str | None = None,
) -> RiskReport:
    """Judge a change by what its removed text was carrying.

    Only removed text is examined. Adding a paragraph cannot take a phrase away
    from a page; rewriting a heading can, and that is the edit worth stopping.
    """
    removed_terms = {
        w.lower() for w in _WORD.findall(removed_text)
        if w.lower() not in _STOPWORDS and len(w) > 2
    }
    if not removed_terms:
        return RiskReport("green", protected, [], ["השינוי מוסיף בלבד ולא מסיר טקסט"])

    target = (target_query or "").strip().lower()

    at_risk: list[ProtectedQuery] = []
    reasons: list[str] = []

    for query in protected:
        if target and query.query.strip().lower() == target:
            continue                        # the query we are deliberately serving
        if not query.terms:
            continue
        overlap = query.terms & removed_terms
        if overlap and len(overlap) == len(query.terms):
            at_risk.append(query)
            reasons.append(
                f"כל מונחי {query.query!r} מוסרים מהדף "
                f"({query.clicks} קליקים, מיקום {query.position:.1f})"
            )

    if any(q.clicks > 0 or q.position <= 5 for q in at_risk):
        level: RiskLevel = "red"
    elif at_risk:
        level = "amber"
    else:
        level = "green"

    return RiskReport(level, protected, at_risk, reasons)
