"""
Did the algorithm do this, or did we?
=====================================

A client sends a message that says traffic dropped, and the first answer
everyone reaches for is "there was a core update". Sometimes that is true.
Often the site changed, a season ended, one page lost one big query, or
nothing happened at all and the comparison was between four weeks and three.

The rule this module enforces is the one the rollback policy already states:
**a drop that coincides with an update is correlation.** Two things have to be
true before a drop is attributed to an algorithm, and the module refuses to
report one without the other:

    1. the change is bigger than this site's own ordinary movement, measured
       from the spread of every page's change rather than from a round number
    2. the site moved together — a single page falling while the rest of the
       site holds is a page problem that happens to share a date with an update

The bundled list of update windows is a fact about the world that goes stale.
It carries the date it was last reviewed, and a comparison window that falls
after that date is reported as unmatchable rather than as "no update found" —
those are very different answers, and only one of them is honest.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

from ..schema import Finding, Result
from .crawl import normalise
from .queries import QueryRow

#: The last date this list was checked against Google's own announcements.
#: A window after it cannot be matched, and the skill says so rather than
#: reporting a clean bill of health it has no basis for.
REVIEWED_THROUGH = date(2025, 8, 31)

#: Announced rollout windows. Dates are the publicly stated start and end of
#: each rollout, which is not the same as the day rankings moved — a rollout
#: that "completed" on the 27th was moving results from the 13th.
UPDATES: list[dict[str, str]] = [
    {"name": "May 2022 core update",        "start": "2022-05-25", "end": "2022-06-09", "kind": "core"},
    {"name": "Helpful Content (Aug 2022)",  "start": "2022-08-25", "end": "2022-09-09", "kind": "content"},
    {"name": "September 2022 core update",  "start": "2022-09-12", "end": "2022-09-26", "kind": "core"},
    {"name": "October 2022 spam update",    "start": "2022-10-19", "end": "2022-10-21", "kind": "spam"},
    {"name": "December 2022 helpful content", "start": "2022-12-05", "end": "2023-01-12", "kind": "content"},
    {"name": "March 2023 core update",      "start": "2023-03-15", "end": "2023-03-28", "kind": "core"},
    {"name": "April 2023 reviews update",   "start": "2023-04-12", "end": "2023-04-26", "kind": "reviews"},
    {"name": "August 2023 core update",     "start": "2023-08-22", "end": "2023-09-07", "kind": "core"},
    {"name": "September 2023 helpful content", "start": "2023-09-14", "end": "2023-09-28", "kind": "content"},
    {"name": "October 2023 core update",    "start": "2023-10-05", "end": "2023-10-19", "kind": "core"},
    {"name": "October 2023 spam update",    "start": "2023-10-04", "end": "2023-10-20", "kind": "spam"},
    {"name": "November 2023 core update",   "start": "2023-11-02", "end": "2023-11-28", "kind": "core"},
    {"name": "March 2024 core update",      "start": "2024-03-05", "end": "2024-04-19", "kind": "core"},
    {"name": "March 2024 spam update",      "start": "2024-03-05", "end": "2024-03-20", "kind": "spam"},
    {"name": "AI Overviews (US rollout)",   "start": "2024-05-14", "end": "2024-05-30", "kind": "serp"},
    {"name": "June 2024 spam update",       "start": "2024-06-20", "end": "2024-06-27", "kind": "spam"},
    {"name": "August 2024 core update",     "start": "2024-08-15", "end": "2024-09-03", "kind": "core"},
    {"name": "November 2024 core update",   "start": "2024-11-11", "end": "2024-12-05", "kind": "core"},
    {"name": "December 2024 core update",   "start": "2024-12-12", "end": "2024-12-18", "kind": "core"},
    {"name": "December 2024 spam update",   "start": "2024-12-19", "end": "2024-12-26", "kind": "spam"},
    {"name": "March 2025 core update",      "start": "2025-03-13", "end": "2025-03-27", "kind": "core"},
    {"name": "June 2025 core update",       "start": "2025-06-30", "end": "2025-07-17", "kind": "core"},
]

#: A page needs this many clicks in the earlier window before its change can
#: be read at all. Below it, "down 60%" is three clicks becoming one.
MIN_CLICKS_BEFORE = 30

#: How far the whole site has to move before the site itself is the story.
SITE_WIDE_DROP = 0.15

#: The multiplier on the interquartile range that makes a page an outlier —
#: the standard definition, used here so the threshold is not a number
#: somebody picked because it looked serious.
OUTLIER_MULTIPLIER = 1.5

#: With fewer pages than this the spread is not a distribution and the
#: outlier test means nothing.
MIN_PAGES_FOR_SPREAD = 8

Verdict = str      # site_wide · page_specific · within_noise · unreadable


def _parse(value: str) -> date:
    return datetime.strptime(value.strip(), "%Y-%m-%d").date()


# ═══════════════════════════════════════════════════════
#  The update list
# ═══════════════════════════════════════════════════════

@dataclass(frozen=True)
class Update:
    name: str
    start: date
    end: date
    kind: str

    def overlaps(self, start: date, end: date) -> bool:
        return self.start <= end and start <= self.end

    def describe(self) -> str:
        return f"{self.name} ({self.start:%d/%m/%Y}–{self.end:%d/%m/%Y})"


def load_updates(path: Path | None = None) -> list[Update]:
    """The bundled list, or a fresher one supplied by the caller."""
    raw: Iterable[dict[str, str]] = UPDATES
    if path and path.exists():
        payload = json.loads(path.read_text(encoding="utf-8"))
        raw = payload.get("updates", payload)

    found = []
    for entry in raw:
        try:
            found.append(Update(
                name=str(entry["name"]), start=_parse(entry["start"]),
                end=_parse(entry["end"]), kind=str(entry.get("kind", "core")),
            ))
        except (KeyError, ValueError):
            continue
    return sorted(found, key=lambda u: u.start)


def in_window(start: date, end: date, updates: list[Update] | None = None
              ) -> list[Update]:
    return [u for u in (updates or load_updates()) if u.overlaps(start, end)]


def coverage(end: date, updates: list[Update] | None = None) -> Result:
    """Whether the list can speak to a window ending on this date.

    A window past the last review is not "no update" — it is "we do not
    know", and reporting the first as the second is how a client is told
    nothing happened during a core update.
    """
    latest = max((u.end for u in (updates or load_updates())), default=None)
    horizon = max(REVIEWED_THROUGH, latest or REVIEWED_THROUGH)
    if end <= horizon:
        return Result.success(
            "covered", f"רשימת העדכונים מכסה עד {horizon:%d/%m/%Y}")
    return Result.failure(
        "stale_list",
        f"החלון נגמר ב-{end:%d/%m/%Y}, והרשימה המובנית מכוסה רק עד "
        f"{horizon:%d/%m/%Y}. “לא נמצא עדכון” כאן אינו ממצא — "
        "ספק רשימה עדכנית עם --updates",
        recoverable=True, horizon=horizon.isoformat(),
    )


# ═══════════════════════════════════════════════════════
#  What moved
# ═══════════════════════════════════════════════════════

@dataclass
class PageChange:
    url: str
    before: int
    after: int
    position_before: float
    position_after: float

    @property
    def delta(self) -> int:
        return self.after - self.before

    @property
    def change(self) -> float:
        return (self.after - self.before) / self.before if self.before else 0.0

    @property
    def readable(self) -> bool:
        return self.before >= MIN_CLICKS_BEFORE

    def describe(self) -> str:
        return (
            f"{self.before:,} → {self.after:,} קליקים ({self.change:+.0%}), "
            f"מיקום {self.position_before:.1f} → {self.position_after:.1f}"
        )


def _aggregate(rows: Iterable[QueryRow]) -> dict[str, dict[str, float]]:
    totals: dict[str, dict[str, float]] = {}
    for row in rows:
        url = normalise(row.url)
        entry = totals.setdefault(url, {"clicks": 0.0, "impressions": 0.0,
                                        "weighted": 0.0})
        entry["clicks"] += row.clicks
        entry["impressions"] += row.impressions
        entry["weighted"] += row.position * row.impressions
    return totals


def compare(before: Iterable[QueryRow], after: Iterable[QueryRow]
            ) -> list[PageChange]:
    """Per-page click change between two windows."""
    first, second = _aggregate(before), _aggregate(after)

    changes: list[PageChange] = []
    for url, was in first.items():
        now = second.get(url, {"clicks": 0.0, "impressions": 0.0, "weighted": 0.0})
        changes.append(PageChange(
            url=url,
            before=int(was["clicks"]), after=int(now["clicks"]),
            position_before=(was["weighted"] / was["impressions"]
                             if was["impressions"] else 0.0),
            position_after=(now["weighted"] / now["impressions"]
                            if now["impressions"] else 0.0),
        ))
    return sorted(changes, key=lambda c: c.delta)


# ═══════════════════════════════════════════════════════
#  Reading it
# ═══════════════════════════════════════════════════════

@dataclass
class Spread:
    """This site's own ordinary movement, so the threshold is not invented."""

    median: float = 0.0
    low: float = 0.0            # below this a page is an outlier
    high: float = 0.0
    sample: int = 0

    @property
    def measurable(self) -> bool:
        return self.sample >= MIN_PAGES_FOR_SPREAD

    def is_outlier(self, change: float) -> bool:
        return self.measurable and (change < self.low or change > self.high)

    def describe(self) -> str:
        if not self.measurable:
            return (
                f"{self.sample} דפים בלבד — מעט מדי כדי למדוד את התנודה "
                "הרגילה של האתר"
            )
        return (
            f"האתר זז {self.median:+.0%} בחציון; תנודה רגילה היא בין "
            f"{self.low:+.0%} ל-{self.high:+.0%}"
        )


def spread_of(changes: Iterable[PageChange]) -> Spread:
    """Measure the site's own noise from the distribution of its pages.

    A fixed "20% is a drop" threshold flags half the pages on a volatile site
    and none on a stable one. The interquartile range says what this site's
    ordinary week looks like, and only what falls outside it is a finding.
    """
    values = sorted(c.change for c in changes if c.readable)
    if len(values) < MIN_PAGES_FOR_SPREAD:
        return Spread(sample=len(values))

    quarters = statistics.quantiles(values, n=4)
    q1, q3 = quarters[0], quarters[2]
    iqr = q3 - q1
    return Spread(
        median=statistics.median(values),
        low=q1 - OUTLIER_MULTIPLIER * iqr,
        high=q3 + OUTLIER_MULTIPLIER * iqr,
        sample=len(values),
    )


@dataclass
class Diagnosis:
    page: PageChange
    verdict: Verdict
    spread: Spread
    updates: list[Update] = field(default_factory=list)

    @property
    def blames_update(self) -> bool:
        """Only a site-wide move during an announced window points at Google."""
        return self.verdict == "site_wide" and bool(self.updates)

    def describe(self) -> str:
        if self.verdict == "unreadable":
            return (
                f"{self.page.before} קליקים לפני — מעט מדי כדי לקרוא שינוי "
                "באחוזים"
            )
        if self.verdict == "within_noise":
            return f"בתוך התנודה הרגילה של האתר. {self.spread.describe()}"
        if self.verdict == "site_wide":
            if self.updates:
                return (
                    f"כל האתר זז יחד ({self.spread.median:+.0%}), בחלון של "
                    f"{', '.join(u.name for u in self.updates)}. "
                    "זו קורלציה, לא הוכחה"
                )
            return (
                f"כל האתר זז יחד ({self.spread.median:+.0%}) ואין עדכון ידוע "
                "בחלון — חפש שינוי טכני, עונתיות, או שינוי שנעשה באתר"
            )
        return (
            f"הדף ירד לבד בזמן שהאתר זז {self.spread.median:+.0%}. "
            "עדכון אלגוריתם לא מפיל דף אחד ומשאיר את השאר — זו בעיה של הדף"
        )


def diagnose(changes: list[PageChange], start: date, end: date,
             updates: list[Update] | None = None) -> list[Diagnosis]:
    """Judge each page against the site, then against the calendar.

    The order matters. A page is compared with its own site first, and only a
    move the whole site made is even offered to the calendar — which is what
    stops every unlucky page in a quiet month from being blamed on a core
    update.
    """
    spread = spread_of(changes)
    matched = in_window(start, end, updates)
    site_wide = spread.measurable and spread.median <= -SITE_WIDE_DROP

    found: list[Diagnosis] = []
    for page in changes:
        if not page.readable:
            verdict = "unreadable"
        elif site_wide:
            verdict = "site_wide"
        elif spread.is_outlier(page.change) and page.change < 0:
            verdict = "page_specific"
        else:
            verdict = "within_noise"

        found.append(Diagnosis(page=page, verdict=verdict, spread=spread,
                               updates=matched if verdict == "site_wide" else []))

    return found


def losses(diagnoses: Iterable[Diagnosis]) -> list[Diagnosis]:
    """Only the pages that actually lost something worth explaining."""
    return sorted(
        (d for d in diagnoses
         if d.verdict in ("site_wide", "page_specific") and d.page.delta < 0),
        key=lambda d: d.page.delta,
    )


# ═══════════════════════════════════════════════════════
#  Findings
# ═══════════════════════════════════════════════════════

def to_findings(diagnoses: Iterable[Diagnosis], client: str) -> list[Finding]:
    """One finding per real loss. None of them proposes an automatic rollback.

    The rollback policy is explicit that a ranking drop never triggers one:
    a restore that is wrong destroys work nobody asked to lose, and a drop is
    never proof of what caused it.
    """
    findings: list[Finding] = []

    for diagnosis in losses(diagnoses):
        page = diagnosis.page
        findings.append(Finding(
            skill="algorithm-update-watch", client=client,
            type="ranking_loss_" + diagnosis.verdict,
            severity="high" if page.delta <= -100 else "medium",
            source="gsc_wizard", url=page.url,
            impact_clicks=float(abs(page.delta)),
            impact_basis=(
                f"{page.describe()}. {diagnosis.describe()}"
            ),
            confidence="medium" if diagnosis.verdict == "page_specific" else "low",
            confidence_reason=(
                "הדף חורג מהתנודה הרגילה של האתר, אבל הסיבה עדיין לא נמדדה — "
                "רק מוקמה בדף ולא באתר"
                if diagnosis.verdict == "page_specific" else
                "חפיפה בין ירידה לחלון עדכון היא קורלציה. אין כאן מדידה של "
                "סיבתיות, ואין שחזור אוטומטי"
            ),
            effort="m",
            evidence={
                "clicks_before": page.before, "clicks_after": page.after,
                "change": round(page.change, 3),
                "position_before": round(page.position_before, 1),
                "position_after": round(page.position_after, 1),
                "site_median_change": round(diagnosis.spread.median, 3),
                "site_noise_band": [round(diagnosis.spread.low, 3),
                                    round(diagnosis.spread.high, 3)],
                "updates_in_window": [u.name for u in diagnosis.updates],
            },
            action={
                "kind": "investigate_loss",
                "url": page.url,
                "instruction": (
                    "השווה מול דפי ביקורת ומול מה שנעשה באתר בחלון. "
                    "ירידה בדירוג לא מפעילה שחזור אוטומטי"
                    if diagnosis.verdict == "site_wide" else
                    "הדף ירד לבד — בדוק מה השתנה בו, לא מה השתנה בגוגל"
                ),
                "verdict": diagnosis.verdict,
            },
            baseline={"clicks": page.before,
                      "position": round(page.position_before, 1)},
        ))

    return findings
