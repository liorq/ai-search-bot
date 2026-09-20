"""
Did it work, and can we say the change is why?
==============================================

Two questions, and the whole point of this module is that they are not the
same one. A page can rise because the change worked, because the whole site
rose, because a competitor dropped off, or because December is December. The
first question is arithmetic. The second is a claim, and a claim needs a
control: what comparable pages did over the same days.

So every assessment carries both, in separate words. `נצפה` for what the
numbers did. `ניתן לייחס` only when the rules below allow it, and never on a
single window with nothing to hold it against.

Twenty-eight days is a first performance check, not a verdict. When the data
cannot answer, the honest output is `inconclusive`, the follow-up stays open,
and the same question is asked again at 56 and 84 days.

Pure functions, no I/O: the caller fetches the two windows and hands them over.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: Below this many impressions in either window, the difference between the two
#: is noise. A page with 30 impressions can double its clicks on one visitor.
MIN_IMPRESSIONS = 100

#: A move smaller than this is not worth calling a change in either direction.
MATERIAL_CHANGE = 0.10

#: When the page and the whole site moved together, within this much of each
#: other, the site is the simpler explanation.
CONTROL_MARGIN = 0.05

#: Past this share of hidden clicks, the visible queries are a minority of the
#: traffic and no attribution can be better than partial. Matches the ceiling
#: in `sources/queries.py`.
MAX_HIDDEN_SHARE = 0.3


@dataclass
class Assessment:
    """What moved, and how much of it can be laid at the change's door."""

    observed: dict[str, Any] = field(default_factory=dict)
    control_change: float | None = None
    verdict: str = "inconclusive"          # improved / declined / no_change / inconclusive
    attributable: str = "unknown"          # yes / partly / no / unknown
    reason: str = ""

    @property
    def conclusive(self) -> bool:
        return self.verdict != "inconclusive"


def _ratio(before: float | int | None, after: float | int | None) -> float | None:
    if before in (None, 0) or after is None:
        return None
    return round((after - before) / before, 4)


def _delta(before: Any, after: Any) -> float | None:
    if before is None or after is None:
        return None
    return round(after - before, 4)


def observe(baseline: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """The arithmetic, with nothing claimed about why.

    Position is reported apart from CTR on purpose: a page that rose two
    places and kept the same CTR earned its extra clicks from the ranking,
    not from the title — and the opposite case is the one a title change is
    supposed to produce.
    """
    seen = {}
    for metric in ("clicks", "impressions"):
        seen[metric] = {"before": baseline.get(metric), "after": current.get(metric),
                        "change": _delta(baseline.get(metric), current.get(metric)),
                        "relative": _ratio(baseline.get(metric), current.get(metric))}
    # Lower is better for position, so its "change" is stated as places gained.
    before_pos, after_pos = baseline.get("position"), current.get("position")
    seen["position"] = {"before": before_pos, "after": after_pos,
                        "places_gained": (round(before_pos - after_pos, 2)
                                          if before_pos is not None and after_pos is not None
                                          else None)}
    before_ctr = _rate(baseline)
    after_ctr = _rate(current)
    seen["ctr"] = {"before": before_ctr, "after": after_ctr,
                   "change": _delta(before_ctr, after_ctr),
                   "relative": _ratio(before_ctr, after_ctr)}
    return seen


def _rate(window: dict[str, Any]) -> float | None:
    clicks, impressions = window.get("clicks"), window.get("impressions")
    if clicks is None or not impressions:
        return None
    return round(clicks / impressions, 4)


def _too_small(baseline: dict[str, Any], current: dict[str, Any]) -> bool:
    return min(baseline.get("impressions") or 0, current.get("impressions") or 0) < MIN_IMPRESSIONS


def assess(baseline: dict[str, Any], current: dict[str, Any],
           control: dict[str, Any] | None = None,
           completeness: dict[str, Any] | None = None) -> Assessment:
    """Compare two equal-length windows and say what can honestly be claimed.

    `control` is the same pair of windows for something that was NOT changed —
    the whole property, or a group of comparable untouched pages. Without it
    the answer tops out at "unknown": there is no way to tell a change that
    worked from a week when everything rose.
    """
    observed = observe(baseline, current)
    # The control is the same two windows for something nobody touched:
    # {"clicks_before": n, "clicks_after": n}.
    control_change = (_ratio(control.get("clicks_before"), control.get("clicks_after"))
                      if control else None)

    if _too_small(baseline, current):
        return Assessment(
            observed, control_change, "inconclusive", "unknown",
            f"פחות מ-{MIN_IMPRESSIONS} הופעות באחד החלונות — ההפרש בתוך הרעש. "
            "המדידה נשארת פתוחה לנקודה הבאה")

    moved = observed["clicks"]["relative"]
    if moved is None:
        return Assessment(observed, control_change, "inconclusive", "unknown",
                          "אין בסיס קליקים להשוואה")

    verdict = ("improved" if moved >= MATERIAL_CHANGE else
               "declined" if moved <= -MATERIAL_CHANGE else "no_change")
    direction = f"הקליקים {'עלו' if moved > 0 else 'ירדו' if moved < 0 else 'לא זזו'} ב-{abs(moved):.0%}"

    if control_change is None:
        return Assessment(observed, None, verdict, "unknown",
                          f"{direction}. אין קבוצת ביקורת — אי אפשר לדעת אם זה השינוי "
                          "או תנועה כללית באתר, ולכן לא מוצג ייחוס")

    # The site moved the same way, by about as much: the site is the explanation.
    if abs(moved - control_change) <= CONTROL_MARGIN and moved * control_change > 0:
        return Assessment(observed, control_change, verdict, "no",
                          f"{direction}, והאתר כולו זז ב-{control_change:.0%} — "
                          "התנועה הזו לא מיוחדת לדף")

    hidden = (completeness or {}).get("anonymised_click_share")
    beyond = round(moved - control_change, 4)
    if hidden is not None and hidden > MAX_HIDDEN_SHARE:
        return Assessment(observed, control_change, verdict, "partly",
                          f"{direction}, מעבר ל-{control_change:.0%} של האתר. "
                          f"{hidden:.0%} מהקליקים מוסתרים ב-Search Console, ולכן הייחוס חלקי")

    places = observed["position"]["places_gained"]
    if places is not None and abs(places) >= 1.0:
        return Assessment(observed, control_change, verdict, "partly",
                          f"{direction} ({beyond:+.0%} מעבר לאתר), אבל גם המיקום זז "
                          f"ב-{places:+.1f} מקומות — חלק מהשינוי הוא דירוג ולא הדף עצמו")

    return Assessment(observed, control_change, verdict, "yes",
                      f"{direction}, מעבר ל-{control_change:.0%} של האתר, "
                      "והמיקום כמעט לא זז — מה שנשאר הוא השינוי בדף")


def report_lines(change: dict[str, Any], assessment: Assessment) -> list[str]:
    """The measurement as a skill prints it: observed and attributable apart."""
    reason = change.get("reason") or {}
    baseline = change.get("baseline") or {}
    window = baseline.get("window") or {}
    observed = assessment.observed

    def moved(metric: str) -> str:
        block = observed.get(metric) or {}
        before, after = block.get("before"), block.get("after")
        relative = block.get("relative")
        if before is None or after is None:
            return "לא זמין"
        tail = f" ({relative:+.0%})" if relative is not None else ""
        return f"{before:,} ← {after:,}{tail}"

    lines = [
        f"דף: {change.get('url', '—')}",
        f"פעולה: {reason.get('instruction') or reason.get('type') or change.get('skill', '—')}",
        f"סיבה: {reason.get('impact_basis', '—')}",
        f"בוצע: {(change.get('applied_at') or '—')[:10]}",
        f"בסיס: {window.get('start', '?')}..{window.get('end', '?')} · "
        f"{baseline.get('clicks', '—')} קליקים, {baseline.get('impressions', '—')} הופעות, "
        f"מיקום {baseline.get('position', '—')}",
        f"נצפה: קליקים {moved('clicks')} · הופעות {moved('impressions')}",
    ]
    places = observed.get("position", {}).get("places_gained")
    if places is not None:
        lines.append(f"נצפה: מיקום {places:+.1f} מקומות")
    lines.append(
        "ניתן לייחס לפעולה: " + {
            "yes": "כן", "partly": "חלקית", "no": "לא", "unknown": "לא ניתן לקבוע",
        }[assessment.attributable] + f" — {assessment.reason}")
    return lines
