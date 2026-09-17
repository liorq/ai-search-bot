"""
Query-to-page analysis: where the clicks are being left on the table.
=====================================================================

Search Console knows, for every query, which URL ranked and what it earned.
Three different problems hide in that one table, and they need different fixes:

    near_miss        one page, position 4–20, more impressions than clicks
    cannibalised     two of our pages competing for the same query
    coverage_gap     the query's own words are missing from the page ranking for it

The estimate of what a fix is worth rests entirely on a CTR curve, and a
published industry curve is a poor substitute for the truth. A local plumber
and a SaaS comparison site have wildly different curves at the same position.
So the curve is **measured from the client's own data** when there is enough of
it, and only falls back to a reference table when there is not — with the
finding saying which was used.
"""

from __future__ import annotations

import json
import re
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from ..schema import Finding, Result

#: Below this, a query's numbers are noise. Matches the toolkit-wide floor.
MIN_IMPRESSIONS = 50

#: Positions worth working on. Above 3 there is little room; past 20 a rewrite
#: is not what stands between the page and the first page of results.
NEAR_MISS_BAND = (3.5, 20.0)

#: How far a realistic fix moves a page. Three places is what an on-page
#: improvement achieves on its own; anything more needs links or authority.
REALISTIC_GAIN = 3.0

#: And however far it moves, it does not promise the top of the page. A page at
#: 4 is not three places from number one in any sense a client should be told:
#: the last places are won with links and brand, not with a better H2.
BEST_CLAIMABLE_POSITION = 3.0

#: A curve bucket needs this many queries before its median means anything.
MIN_ROWS_PER_BUCKET = 8

#: Two URLs count as competing only when both are really in the game.
CANNIBAL_MIN_IMPRESSIONS = 30

#: Published averages, used only when the client's own data cannot support a
#: curve. Deliberately conservative — every finding built on these says so.
REFERENCE_CTR = {
    1: 0.270, 2: 0.150, 3: 0.110, 4: 0.080, 5: 0.060,
    6: 0.050, 7: 0.040, 8: 0.032, 9: 0.028, 10: 0.025,
    12: 0.018, 15: 0.013, 20: 0.008, 30: 0.004,
}

_WORD = re.compile(r"[\w֐-׿']+", re.UNICODE)

#: Words that carry no topical meaning, so their absence from a page proves
#: nothing. Both languages, because the toolkit writes in both.
_STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "for", "to", "in", "on", "at", "is",
    "are", "my", "your", "near", "me", "best", "how", "what", "why", "with",
    "של", "את", "עם", "על", "הוא", "היא", "זה", "מה", "איך", "כמה", "הכי",
}


# ═══════════════════════════════════════════════════════
#  Rows
# ═══════════════════════════════════════════════════════

@dataclass
class QueryRow:
    """One query/page pair as Search Console reports it."""

    query: str
    url: str
    clicks: int
    impressions: int
    position: float

    @property
    def ctr(self) -> float:
        return self.clicks / self.impressions if self.impressions else 0.0

    @property
    def is_material(self) -> bool:
        return self.impressions >= MIN_IMPRESSIONS

    def terms(self) -> set[str]:
        """The query's meaningful words, for checking whether a page covers it."""
        return {w for w in _WORD.findall(self.query.lower()) if w not in _STOPWORDS}


def load_export(path: Path) -> Result:
    """Read the query export Claude assembles from GSC Wizard.

    Shape:
        {"property": "...", "range": "28d",
         "rows": [{"query": "...", "url": "...", "clicks": 12,
                   "impressions": 900, "position": 8.4}, ...]}
    """
    if not path.exists():
        return Result.failure(
            "export_missing",
            f"לא נמצא קובץ שאילתות ב-{path}. שלוף מ-GSC Wizard שאילתות עם "
            "הדף שדורג, קליקים, הופעות ומיקום, ושמור כ-JSON.",
            recoverable=True,
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return Result.failure("export_corrupt", f"קובץ השאילתות אינו JSON תקין: {exc}")

    rows: list[QueryRow] = []
    for entry in raw.get("rows") or []:
        try:
            rows.append(QueryRow(
                query=str(entry["query"]).strip().lower(),
                url=str(entry["url"]).strip(),
                clicks=int(entry.get("clicks") or 0),
                impressions=int(entry.get("impressions") or 0),
                position=float(entry.get("position") or 0),
            ))
        except (KeyError, TypeError, ValueError):
            continue

    if not rows:
        return Result.failure(
            "export_empty",
            "הקובץ לא מכיל שורות שמישות — כל שורה צריכה query, url, "
            "clicks, impressions ו-position",
        )
    return Result.success(
        "loaded", f"{len(rows)} שורות שאילתה נטענו",
        rows=rows, window=raw.get("range", "28d"),
    )


# ═══════════════════════════════════════════════════════
#  The CTR curve — measured, not assumed
# ═══════════════════════════════════════════════════════

@dataclass
class CTRCurve:
    """Expected click-through rate by position, for this site specifically."""

    buckets: dict[int, float] = field(default_factory=dict)
    source: str = "reference"          # "site" or "reference"
    sample: int = 0

    def expected(self, position: float) -> float:
        """CTR we would expect at this position, interpolating between buckets."""
        table = self.buckets or REFERENCE_CTR
        slot = max(1, int(round(position)))
        if slot in table:
            return table[slot]

        known = sorted(table)
        if slot <= known[0]:
            return table[known[0]]
        if slot >= known[-1]:
            return table[known[-1]]

        below = max(k for k in known if k < slot)
        above = min(k for k in known if k > slot)
        span = above - below
        weight = (slot - below) / span
        return table[below] + (table[above] - table[below]) * weight

    def describe(self) -> str:
        if self.source == "site":
            return f"עקומת CTR שנמדדה מהאתר עצמו על {self.sample} שאילתות"
        return "עקומת CTR ממוצעת מהתעשייה — לאתר אין מספיק נתונים לעקומה משלו"


def build_curve(rows: Iterable[QueryRow]) -> CTRCurve:
    """Measure the site's own CTR by position, where the data supports it.

    The median is used rather than the mean: one branded query at position 1
    with a 60% CTR would drag an average far away from what a service page can
    actually expect.
    """
    grouped: dict[int, list[float]] = {}
    usable = 0
    for row in rows:
        if not row.is_material or row.position < 1:
            continue
        usable += 1
        grouped.setdefault(_bucket(row.position), []).append(row.ctr)

    measured = {
        slot: statistics.median(values)
        for slot, values in grouped.items()
        if len(values) >= MIN_ROWS_PER_BUCKET
    }
    # A curve with one or two points cannot be interpolated across the band.
    if len(measured) < 3:
        return CTRCurve(source="reference", sample=usable)

    return CTRCurve(buckets=_monotonic(measured), source="site", sample=usable)


def _bucket(position: float) -> int:
    """Positions 1–10 stand alone; beyond that they group, as the data thins."""
    slot = int(round(position))
    if slot <= 10:
        return max(1, slot)
    if slot <= 15:
        return 12
    if slot <= 25:
        return 20
    return 30


def _monotonic(measured: dict[int, float]) -> dict[int, float]:
    """Force the curve to fall with position.

    A sample can easily show position 6 out-earning position 4. Left alone, that
    produces a "move up and lose clicks" estimate, which is never what we mean.
    """
    fixed: dict[int, float] = {}
    ceiling = 1.0
    for slot in sorted(measured):
        ceiling = min(ceiling, measured[slot])
        fixed[slot] = ceiling
    return fixed


# ═══════════════════════════════════════════════════════
#  Opportunities
# ═══════════════════════════════════════════════════════

#: A page earning far less than its position should is a title and snippet
#: problem, not a content one. That is `ctr-titles`, and saying so is more
#: useful than silently proposing the wrong fix.
LOW_CTR_RATIO = 0.5

#: But only where CTR means anything. At position 12 the expected rate is about
#: 1%, so "half of expected" is a handful of clicks and mostly noise — and the
#: real problem is almost always that the page is on the second page at all.
LOW_CTR_MAX_POSITION = 10.0

#: Consolidation recovers most of a split query, not all of it. One page still
#: has to absorb the other's intent, and some of the combined signal is lost.
CONSOLIDATION_RECOVERY = 0.7

#: Where a page can realistically land once it actually answers the query.
COVERAGE_TARGET_POSITION = 8.0


@dataclass
class Opportunity:
    """One concrete thing to change, with what it is expected to return."""

    kind: str                       # near_miss · cannibalised · coverage_gap · low_ctr
    query: str
    url: str
    impressions: int
    clicks: int
    position: float
    potential_clicks: float
    basis: str
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def is_actionable(self) -> bool:
        """`low_ctr` belongs to another skill, so it is reported and not acted on."""
        return self.kind != "low_ctr" and self.potential_clicks > 0


def _gain(impressions: int, clicks: int, target: float, curve: CTRCurve) -> float:
    """Clicks at the target position, minus what the page already earns."""
    return max(0.0, impressions * curve.expected(target) - clicks)


def find_near_misses(rows: Iterable[QueryRow], curve: CTRCurve) -> list[Opportunity]:
    """Queries a few places away from real traffic.

    A page at 9 with 2,000 impressions is worth more attention than a page at
    30 with the same, because three places is a distance on-page work covers.
    """
    found: list[Opportunity] = []
    low, high = NEAR_MISS_BAND

    for row in rows:
        if not row.is_material or not low <= row.position <= high:
            continue

        par = curve.expected(row.position)
        if par and row.position <= LOW_CTR_MAX_POSITION and row.ctr < par * LOW_CTR_RATIO:
            found.append(Opportunity(
                kind="low_ctr", query=row.query, url=row.url,
                impressions=row.impressions, clicks=row.clicks, position=row.position,
                potential_clicks=_gain(row.impressions, row.clicks, row.position, curve),
                basis=(
                    f"במיקום {row.position:.1f} העמוד מקבל {row.ctr:.1%} במקום "
                    f"{par:.1%} הצפויים — הבעיה בטייטל ובתיאור, לא בתוכן"
                ),
                detail={"expected_ctr": round(par, 4), "actual_ctr": round(row.ctr, 4),
                        "belongs_to": "ctr-titles"},
            ))
            continue

        target = max(BEST_CLAIMABLE_POSITION, row.position - REALISTIC_GAIN)
        gain = _gain(row.impressions, row.clicks, target, curve)
        if gain <= 0:
            continue

        found.append(Opportunity(
            kind="near_miss", query=row.query, url=row.url,
            impressions=row.impressions, clicks=row.clicks, position=row.position,
            potential_clicks=gain,
            basis=(
                f"{row.impressions:,} הופעות במיקום {row.position:.1f}. "
                f"עלייה למיקום {target:.0f} בעקומת האתר שווה כ-{gain:.0f} קליקים"
            ),
            detail={"target_position": round(target, 1)},
        ))
    return found


def find_cannibalisation(rows: Iterable[QueryRow], curve: CTRCurve) -> list[Opportunity]:
    """Queries where two of our own pages are competing.

    Google picks one and the signal splits. The fix is consolidation — deciding
    which page owns the query — and never rewriting both.
    """
    by_query: dict[str, list[QueryRow]] = {}
    for row in rows:
        if row.impressions >= CANNIBAL_MIN_IMPRESSIONS:
            by_query.setdefault(row.query, []).append(row)

    found: list[Opportunity] = []
    for query, competitors in by_query.items():
        if len(competitors) < 2:
            continue

        # The winner is the page already doing best, by clicks then position.
        ranked = sorted(competitors, key=lambda r: (-r.clicks, r.position))
        winner, losers = ranked[0], ranked[1:]

        impressions = sum(r.impressions for r in competitors)
        clicks = sum(r.clicks for r in competitors)
        target = max(1.0, winner.position - 1.0)
        gain = _gain(impressions, clicks, target, curve) * CONSOLIDATION_RECOVERY

        found.append(Opportunity(
            kind="cannibalised", query=query, url=winner.url,
            impressions=impressions, clicks=clicks, position=winner.position,
            potential_clicks=gain,
            basis=(
                f"{len(competitors)} דפים מתחרים על השאילתה. איחוד סביב "
                f"{winner.url} מחזיר כ-{CONSOLIDATION_RECOVERY:.0%} מהפער"
            ),
            detail={
                "winner": winner.url,
                "losers": [{"url": r.url, "clicks": r.clicks,
                            "impressions": r.impressions,
                            "position": round(r.position, 1)} for r in losers],
            },
        ))
    return found


#: Suffixes and prefixes that make the same word look like a different one.
#: Without these, a page about "springs" reads as missing "spring" — a finding
#: that is not only wrong but insulting to whoever wrote the page.
_EN_SUFFIXES = ("s", "es", "ing", "ed")
_HE_PREFIXES = ("ה", "ב", "ל", "ו", "מ", "ש", "כ")


def covers(term: str, words: set[str]) -> bool:
    """Whether the page says this word, allowing for ordinary inflection."""
    if term in words:
        return True
    if any(term + suffix in words for suffix in _EN_SUFFIXES):
        return True
    if any(word + suffix == term for word in words for suffix in _EN_SUFFIXES):
        return True
    # Hebrew attaches its articles and conjunctions to the front of the word.
    if any(prefix + term in words for prefix in _HE_PREFIXES):
        return True
    return any(term == word[1:] and word[0] in _HE_PREFIXES for word in words)


def find_coverage_gaps(
    rows: Iterable[QueryRow], page_text: dict[str, str], curve: CTRCurve
) -> list[Opportunity]:
    """Queries whose own words never appear on the page ranking for them.

    This is the clearest on-page signal there is: Google decided the page is
    about the topic, and the page does not say so. Missing terms are checked
    against the rendered text, not the markup, and against inflected forms —
    "springs" on the page covers "spring" in the query.
    """
    found: list[Opportunity] = []
    normalised = {url: set(_WORD.findall(text.lower())) for url, text in page_text.items()}

    for row in rows:
        if not row.is_material or row.position < 5:
            continue
        words = normalised.get(row.url)
        if words is None:                       # page text was not supplied
            continue

        missing = sorted(term for term in row.terms() if not covers(term, words))
        if not missing:
            continue

        gain = _gain(row.impressions, row.clicks, COVERAGE_TARGET_POSITION, curve)
        if gain <= 0:
            continue

        found.append(Opportunity(
            kind="coverage_gap", query=row.query, url=row.url,
            impressions=row.impressions, clicks=row.clicks, position=row.position,
            potential_clicks=gain,
            basis=(
                f"הדף מדורג {row.position:.1f} על השאילתה אבל לא מכיל את "
                f"{', '.join(missing)}. מענה ישיר מקרב אותו למיקום "
                f"{COVERAGE_TARGET_POSITION:.0f}"
            ),
            detail={"missing_terms": missing},
        ))
    return found


def analyse(
    rows: list[QueryRow], page_text: dict[str, str] | None = None
) -> dict[str, Any]:
    """Everything the skill needs, in the order it wants to present it."""
    curve = build_curve(rows)
    cannibalised = find_cannibalisation(rows, curve)

    # A query already flagged as split between pages does not also need a
    # "move up three places" note against one of those pages.
    split = {o.query for o in cannibalised}
    near = [o for o in find_near_misses(rows, curve) if o.query not in split]

    # A query routed to ctr-titles is somebody else's job; listing it again as a
    # content gap would have two skills claiming the same clicks.
    claimed = split | {(o.query) for o in near if o.kind == "low_ctr"}
    gaps = [o for o in find_coverage_gaps(rows, page_text or {}, curve)
            if o.query not in claimed]

    return {
        "curve": curve,
        "opportunities": sorted(
            cannibalised + near + gaps,
            key=lambda o: o.potential_clicks, reverse=True,
        ),
    }


# ═══════════════════════════════════════════════════════
#  Findings
# ═══════════════════════════════════════════════════════

_SEVERITY_FLOOR = ((100, "high"), (30, "medium"))

_ACTIONS = {
    "near_miss": (
        "expand_section", "m",
        "הרחב את המענה לשאילתה בדף הקיים — כותרת משנה ופסקה שעונה ישירות",
    ),
    "coverage_gap": (
        "add_section", "m",
        "הוסף סעיף שעונה על השאילתה, עם המונחים החסרים בטקסט עצמו",
    ),
    "cannibalised": (
        "consolidate", "l",
        "אחד את הדפים: אחד נשאר, השאר מפנים אליו ב-301 וקישור פנימי",
    ),
    "low_ctr": (
        "rewrite_title", "s",
        "שכתוב טייטל ותיאור — הטיפול שייך ל-ctr-titles",
    ),
}


def _severity(potential: float) -> str:
    for floor, level in _SEVERITY_FLOOR:
        if potential >= floor:
            return level
    return "low"


def _confidence(opportunity: Opportunity, curve: CTRCurve) -> tuple[str, str]:
    if curve.source != "site":
        return "low", (
            "האומדן נשען על עקומת CTR ממוצעת מהתעשייה — "
            "לאתר אין מספיק שאילתות לעקומה משלו"
        )
    if opportunity.kind == "cannibalised":
        return "medium", (
            f"איחוד דפים מחזיר בדרך כלל את רוב הפער, לא את כולו; "
            f"חושב לפי {CONSOLIDATION_RECOVERY:.0%} על {curve.describe()}"
        )
    if opportunity.kind == "coverage_gap":
        return "medium", (
            "המונחים החסרים הם סימן ברור, אבל המיקום שאליו הדף יגיע "
            "תלוי גם במתחרים ולא רק בתוכן"
        )
    return "high", (
        f"{curve.describe()}, ועלייה של {REALISTIC_GAIN:.0f} מקומות היא "
        "מה שעבודה על הדף עצמו משיגה"
    )


def to_finding(
    opportunity: Opportunity, client: str, curve: CTRCurve,
    conversion_rate: float | None = None,
) -> Finding:
    """One opportunity as a ranked, evidence-carrying finding."""
    kind, effort, instruction = _ACTIONS[opportunity.kind]
    confidence, reason = _confidence(opportunity, curve)
    potential = round(opportunity.potential_clicks, 1)

    action: dict[str, Any] = {
        "kind": kind,
        "url": opportunity.url,
        "query": opportunity.query,
        "instruction": instruction,
    }
    action.update(opportunity.detail)

    return Finding(
        skill="onpage-optimizer",
        client=client,
        type=opportunity.kind,
        severity="low" if opportunity.kind == "low_ctr" else _severity(potential),
        source="gsc_wizard",
        url=opportunity.url,
        query=opportunity.query,
        impact_clicks=0.0 if opportunity.kind == "low_ctr" else potential,
        impact_conversions=(
            round(potential * conversion_rate, 2)
            if conversion_rate and opportunity.kind != "low_ctr" else None
        ),
        impact_basis=opportunity.basis,
        confidence=confidence,
        confidence_reason=reason,
        effort=effort,
        evidence={
            "impressions": opportunity.impressions,
            "clicks": opportunity.clicks,
            "position": round(opportunity.position, 1),
            "ctr": round(opportunity.clicks / opportunity.impressions, 4)
            if opportunity.impressions else 0.0,
            "expected_ctr": round(curve.expected(opportunity.position), 4),
            "curve_source": curve.source,
            **opportunity.detail,
        },
        action=action,
        baseline={
            "position": round(opportunity.position, 1),
            "clicks": opportunity.clicks,
            "impressions": opportunity.impressions,
        },
    )
