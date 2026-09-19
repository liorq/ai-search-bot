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
from urllib.parse import urlparse

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

#: A query needs this many impressions, across all its URLs, to be worth a look.
CANNIBAL_MIN_IMPRESSIONS = 30

#: What "in the game" means for a rival URL. Scored on sass-srq.com against
#: day-level data (2+ pages each reaching the top 20 on a quarter of the days):
#: 7 flagged, 7 true, 0 false, 2 missed — and the 2 missed were the two the live
#: results page did not confirm either. At position ≤ 30 three false ones return.
CANNIBAL_MAX_POSITION = 25.0
CANNIBAL_MIN_SHARE = 0.15

#: The home page this high on a query means a brand search.
BRAND_HOME_POSITION = 3.0

#: Fewer places than this between two click-less pages, and neither has won.
CLEAR_WINNER_GAP = 3.0

#: A redirect is never recommended from split rankings alone. It throws away a
#: URL with its own links, traffic and conversions, and none of that is visible
#: in a query table.
REDIRECT_POLICY = (
    "הפניית 301 אינה חלק מההמלצה. היא דורשת בדיקה נפרדת של הדף המשני — קישורים "
    "נכנסים, תנועה והמרות — ואישור מפורש. דף הבית לא מופנה לעולם"
)

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
    dropped = 0
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
            dropped += 1          # counted and reported — never skipped in silence

    if not rows:
        return Result.failure(
            "export_empty",
            "הקובץ לא מכיל שורות שמישות — כל שורה צריכה query, url, "
            "clicks, impressions ו-position",
        )
    detail = f"{len(rows)} שורות שאילתה נטענו"
    if dropped:
        detail += f", {dropped} שורות פגומות דולגו"
    return Result.success(
        "loaded", detail,
        rows=rows, window=raw.get("range", "28d"), dropped=dropped,
        completeness=raw.get("completeness"),
    )


#: Past this share of hidden queries, an estimate is built on a minority of the
#: traffic and may not claim full confidence. From docs/data-contract.md.
MAX_HIDDEN_SHARE = 0.3

_CONFIDENCE_ORDER = ("low", "medium", "high")


def cap_confidence(confidence: str, reason: str,
                   completeness: dict[str, Any] | None) -> tuple[str, str]:
    """Hold a finding's confidence to what the data underneath it can carry.

    An analysis of partial rows is still worth reading, but it may not present
    itself as certain — and the finding has to say why it was held back.
    """
    def capped(ceiling: str, why: str) -> tuple[str, str]:
        if _CONFIDENCE_ORDER.index(confidence) <= _CONFIDENCE_ORDER.index(ceiling):
            return confidence, f"{reason}. {why}"
        return ceiling, f"{reason}. הביטחון הורד ל-{ceiling}: {why}"

    if not completeness:
        return capped("medium", "שלמות הנתונים לא ידועה — הקובץ לא מדווח כמה מהנכס הוא מכסה")
    if completeness.get("truncated") is True:
        return capped("low", "המשיכה נחתכה לפני סוף הנתונים")
    if completeness.get("truncated") == "unknown":
        return capped("medium", "לא ידוע אם המשיכה הגיעה לסוף הנתונים")
    hidden = [completeness.get("anonymised_share"), completeness.get("anonymised_click_share")]
    worst = max((h for h in hidden if h is not None), default=None)
    if worst is not None and worst > MAX_HIDDEN_SHARE:
        return capped("medium", f"{worst:.0%} מהתנועה של הנכס יושבים בשאילתות "
                                "ש-Search Console לא חושף, ולכן לא נכללו בניתוח")
    return confidence, reason


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


def _is_home(url: str) -> bool:
    return urlparse(url).path in ("", "/")


def find_cannibalisation(rows: Iterable[QueryRow], curve: CTRCurve) -> list[Opportunity]:
    """Queries where two of our own pages are really competing.

    "Really" is the whole point. The first version asked only that each URL had
    30 impressions, and on a real site 20 of its 22 findings were wrong: the
    "rival" sat at position 40–88, where it competes with nobody. Checked
    against day-by-day data and the live results page, a rival is in the game
    when it ranks near the front *and* holds a real share of the query:

        position ≤ CANNIBAL_MAX_POSITION  and  share ≥ CANNIBAL_MIN_SHARE

    Two kinds of row are never rivals. The home page, because on a local query
    its impressions come from the map pack's website button, not from a second
    organic listing. And any query the home page leads from the top — that is a
    brand search, and the extra URLs are sitelinks.
    """
    by_query: dict[str, list[QueryRow]] = {}
    for row in rows:
        by_query.setdefault(row.query, []).append(row)

    found: list[Opportunity] = []
    for query, pages in by_query.items():
        total = sum(r.impressions for r in pages)
        if len(pages) < 2 or total < CANNIBAL_MIN_IMPRESSIONS:
            continue
        if any(_is_home(r.url) and r.position <= BRAND_HOME_POSITION for r in pages):
            continue                                   # a brand search — sitelinks, not rivals

        players = [r for r in pages
                   if not _is_home(r.url)
                   and r.position <= CANNIBAL_MAX_POSITION
                   and r.impressions / total >= CANNIBAL_MIN_SHARE]
        if len(players) < 2:
            continue

        # The winner is the page already doing best, by clicks then position.
        ranked = sorted(players, key=lambda r: (-r.clicks, r.position))
        winner, rivals = ranked[0], ranked[1:]
        # With no clicks between them and a couple of places apart, the numbers
        # do not pick an owner — that is a content decision, and the finding says so.
        clear = (winner.clicks > rivals[0].clicks
                 or rivals[0].position - winner.position >= CLEAR_WINNER_GAP)

        impressions = sum(r.impressions for r in players)
        clicks = sum(r.clicks for r in players)
        target = max(1.0, winner.position - 1.0)
        gain = _gain(impressions, clicks, target, curve) * CONSOLIDATION_RECOVERY

        found.append(Opportunity(
            kind="cannibalised", query=query, url=winner.url,
            impressions=impressions, clicks=clicks, position=winner.position,
            potential_clicks=gain,
            basis=(
                f"{len(players)} דפים שלנו מדורגים על אותה שאילתה (מיקום "
                + " ו-".join(f"{r.position:.0f}" for r in ranked)
                + f"). ריכוז האות ב-{winner.url} מחזיר כ-{CONSOLIDATION_RECOVERY:.0%} מהפער"
                + ("" if clear else ". אין מנצח ברור במספרים — איזה דף יחזיק בשאילתה "
                                    "היא החלטת תוכן")
            ),
            detail={
                "winner": winner.url,
                "clear_winner": clear,
                "losers": [{"url": r.url, "clicks": r.clicks,
                            "impressions": r.impressions,
                            "position": round(r.position, 1),
                            "share": round(r.impressions / total, 2)} for r in rivals],
                "redirect": REDIRECT_POLICY,
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
        "differentiate", "m",
        "קבע איזה דף הוא הבעלים של השאילתה: קישור פנימי מהדף המשני אליו עם "
        "השאילתה כעוגן, והבדלת הטייטל וה-H1 של הדף המשני כך שיענה על כוונה אחרת",
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
            f"ריכוז האות בדף אחד מחזיר בדרך כלל את רוב הפער, לא את כולו; "
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


_NOT_MEASURED: Any = object()


def to_finding(
    opportunity: Opportunity, client: str, curve: CTRCurve,
    conversion_rate: float | None = None,
    completeness: dict[str, Any] | None = _NOT_MEASURED,
) -> Finding:
    """One opportunity as a ranked, evidence-carrying finding.

    Pass the export's `completeness` block (or None when the export has none)
    and the confidence is held to what the data can carry.
    """
    kind, effort, instruction = _ACTIONS[opportunity.kind]
    confidence, reason = _confidence(opportunity, curve)
    if completeness is not _NOT_MEASURED:
        confidence, reason = cap_confidence(confidence, reason, completeness)
    potential = round(opportunity.potential_clicks, 1)

    # An estimate can only speak for the queries Search Console showed us. When
    # most of the property's clicks sit in hidden queries, the number is marked
    # as covering the visible part only — it is not the site's upside.
    coverage: dict[str, Any] = {}
    basis = opportunity.basis
    if completeness is not _NOT_MEASURED and completeness:
        hidden = completeness.get("anonymised_click_share")
        coverage = {"data_coverage": {
            "hidden_click_share": hidden,
            "hidden_impression_share": completeness.get("anonymised_share"),
        }}
        if hidden is not None and hidden > MAX_HIDDEN_SHARE:
            basis += (f". האומדן מתייחס רק לשאילתות הגלויות — {hidden:.0%} מהקליקים "
                      "של הנכס מגיעים משאילתות ש-Search Console מסתיר")

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
        impact_basis=basis,
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
            **coverage,
            **opportunity.detail,
        },
        action=action,
        baseline={
            "position": round(opportunity.position, 1),
            "clicks": opportunity.clicks,
            "impressions": opportunity.impressions,
        },
    )
