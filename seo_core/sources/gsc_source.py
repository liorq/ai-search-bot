"""
Search Console data, and what a traffic loss actually means.
============================================================

Data arrives as JSON that Claude has already pulled from the GSC Wizard MCP
(or the Search Console API) and written to disk. MCP tools belong to Claude,
not to a Python subprocess, so the handoff is a file — which has the pleasant
side effect of making every analysis here testable offline against a saved
response.

The analysis is the part worth getting right. "This page lost traffic" is not
an instruction; five different causes produce that same shape and four of them
should not be treated by rewriting the page:

    position_loss   the page genuinely slipped          → refresh the content
    seasonality     demand left, rankings held           → do nothing, plan ahead
    serp_takeover   someone new outranks us              → study them first
    cannibalized    our own newer page is competing      → consolidate, not rewrite
    deindexed       impressions collapsed to near zero   → an indexing problem
    unclear         not enough signal to say             → measure, do not act

Rewriting a page that lost traffic to seasonality is work that cannot succeed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from ..schema import Finding, Result

DecayCause = Literal[
    "position_loss", "seasonality", "serp_takeover",
    "cannibalized", "deindexed", "unclear",
]

#: Below this, a percentage swing is noise dressed up as a trend.
MIN_IMPRESSIONS = 100
MIN_CLICKS_BEFORE = 10

#: How much of a drop counts as decay at all.
DECAY_CLICK_THRESHOLD = 0.20

#: Position moved by less than this and the ranking effectively held.
POSITION_HELD = 1.0

#: Impressions at this fraction of before means the page stopped being shown.
DEINDEXED_IMPRESSION_RATIO = 0.10


@dataclass
class QueryRow:
    query: str
    clicks: int = 0
    impressions: int = 0
    position: float = 99.0
    ctr: float = 0.0

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "QueryRow":
        return cls(
            query=str(raw.get("query", "")),
            clicks=int(raw.get("clicks", 0) or 0),
            impressions=int(raw.get("impressions", 0) or 0),
            position=float(raw.get("position", 99) or 99),
            ctr=float(raw.get("ctr", 0) or 0),
        )


@dataclass
class PageWindow:
    """One page's performance over one time window."""

    url: str
    clicks: int = 0
    impressions: int = 0
    position: float = 99.0
    queries: list[QueryRow] = field(default_factory=list)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "PageWindow":
        return cls(
            url=str(raw.get("url", "")),
            clicks=int(raw.get("clicks", 0) or 0),
            impressions=int(raw.get("impressions", 0) or 0),
            position=float(raw.get("position", 99) or 99),
            queries=[QueryRow.from_dict(q) for q in raw.get("queries", [])],
        )


@dataclass
class DecayVerdict:
    url: str
    cause: DecayCause
    clicks_lost: int
    clicks_pct: float
    position_delta: float
    impressions_pct: float
    explanation: str
    confidence: Literal["high", "medium", "low"]
    confidence_reason: str
    worst_queries: list[str] = field(default_factory=list)

    @property
    def is_actionable(self) -> bool:
        """Whether rewriting the page is the right response.

        Only two causes are. A seasonal dip needs a calendar, not copy; a SERP
        takeover needs the competitor read first; cannibalisation needs a
        consolidation decision, which is a different change entirely.
        """
        return self.cause in ("position_loss", "serp_takeover")


# ═══════════════════════════════════════════════════════
#  Loading
# ═══════════════════════════════════════════════════════

def load_export(path: Path) -> Result:
    """Read the JSON Claude produced from the MCP.

    Expected shape::

        {"property": "...", "current": {"range": "...", "pages": [...]},
         "prior":   {"range": "...", "pages": [...]},
         "site":    {"clicks_pct": -0.04},
         "algorithm_updates": ["2026-08-12"]}
    """
    if not path.exists():
        return Result.failure(
            "export_missing",
            f"לא נמצא קובץ הנתונים: {path}. "
            "שלוף אותו קודם מ-GSC Wizard ושמור אותו כאן",
        )
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except json.JSONDecodeError as exc:
        return Result.failure("export_corrupt", f"קובץ הנתונים אינו JSON תקין: {exc}")

    for key in ("current", "prior"):
        if key not in raw:
            return Result.failure(
                "export_incomplete",
                f"חסר '{key}' בקובץ — צריך שתי תקופות כדי להשוות",
            )

    current = {p["url"]: PageWindow.from_dict(p) for p in raw["current"].get("pages", [])}
    prior = {p["url"]: PageWindow.from_dict(p) for p in raw["prior"].get("pages", [])}

    return Result.success(
        "loaded",
        f"{len(current)} דפים בתקופה הנוכחית, {len(prior)} בקודמת",
        current=current,
        prior=prior,
        site_trend=float(raw.get("site", {}).get("clicks_pct", 0.0)),
        algorithm_updates=list(raw.get("algorithm_updates", [])),
        property=raw.get("property", ""),
    )


# ═══════════════════════════════════════════════════════
#  Classification
# ═══════════════════════════════════════════════════════

def classify(
    current: PageWindow,
    prior: PageWindow,
    *,
    site_trend: float = 0.0,
    competing_urls: list[str] | None = None,
    new_competitors: list[str] | None = None,
) -> DecayVerdict | None:
    """Work out why a page lost traffic, or return None if it did not.

    Two of the causes cannot be read out of Search Console at all, so each one
    requires its evidence to be passed in:

    `competing_urls` — our own pages now ranking for these queries, which is
    what separates cannibalisation from an ordinary slip.

    `new_competitors` — domains that entered the top results since the prior
    window. Without them, "we slipped" and "someone overtook us" produce an
    identical signal here, and claiming to tell them apart would be a guess
    dressed as a diagnosis. That comparison belongs to SERP data.
    """
    if prior.clicks < MIN_CLICKS_BEFORE or prior.impressions < MIN_IMPRESSIONS:
        return None                      # too small to have a readable trend

    clicks_pct = (current.clicks - prior.clicks) / prior.clicks
    if clicks_pct > -DECAY_CLICK_THRESHOLD:
        return None                      # not a decay

    impressions_pct = (
        (current.impressions - prior.impressions) / prior.impressions
        if prior.impressions else 0.0
    )
    position_delta = current.position - prior.position      # positive = fell

    worst = sorted(
        (q for q in prior.queries if q.clicks > 0),
        key=lambda q: -q.clicks,
    )[:5]
    worst_queries = [q.query for q in worst]

    common = dict(
        url=current.url,
        clicks_lost=prior.clicks - current.clicks,
        clicks_pct=clicks_pct,
        position_delta=position_delta,
        impressions_pct=impressions_pct,
        worst_queries=worst_queries,
    )

    # Impressions gone means the page stopped being shown at all.
    if current.impressions <= prior.impressions * DEINDEXED_IMPRESSION_RATIO:
        return DecayVerdict(
            cause="deindexed",
            explanation=(
                f"ההופעות צנחו ב-{abs(impressions_pct):.0%} — הדף כמעט לא מוצג. "
                "זו בעיית אינדוקס, לא בעיית תוכן"
            ),
            confidence="high",
            confidence_reason="צניחת הופעות כמעט מוחלטת היא סיגנל חד-משמעי",
            **common,
        )

    # Our own newer page now ranks for the same queries.
    if competing_urls:
        return DecayVerdict(
            cause="cannibalized",
            explanation=(
                f"דפים אחרים באתר מדורגים על אותן שאילתות: "
                f"{', '.join(competing_urls[:2])}. מיזוג, לא שכתוב"
            ),
            confidence="medium",
            confidence_reason="חפיפת שאילתות מצביעה על קניבליזציה אבל לא מוכיחה אותה",
            **common,
        )

    # A named competitor entered the results. Read their page before rewriting.
    if new_competitors and position_delta >= POSITION_HELD:
        return DecayVerdict(
            cause="serp_takeover",
            explanation=(
                f"המיקום ירד ב-{position_delta:.1f} ובתוצאות נכנסו: "
                f"{', '.join(new_competitors[:2])}. כדאי לקרוא את הדף שלהם לפני שמשכתבים"
            ),
            confidence="medium",
            confidence_reason="כניסת מתחרה מתועדת, אבל בלי הוכחה שהיא הסיבה לירידה",
            **common,
        )

    # Rankings held and impressions fell: demand left, not the page.
    if abs(position_delta) < POSITION_HELD and impressions_pct < -DECAY_CLICK_THRESHOLD:
        return DecayVerdict(
            cause="seasonality",
            explanation=(
                f"המיקום כמעט לא זז ({position_delta:+.1f}) אבל ההופעות ירדו "
                f"ב-{abs(impressions_pct):.0%} — הביקוש ירד, לא הדף. "
                "שכתוב כאן לא יחזיר תנועה"
            ),
            confidence="medium",
            confidence_reason="ירידת ביקוש ומיקום יציב, אבל בלי נתוני עונתיות רב-שנתיים",
            **common,
        )

    if position_delta >= POSITION_HELD:
        drop_vs_site = clicks_pct - site_trend
        return DecayVerdict(
            cause="position_loss",
            explanation=(
                f"הדף ירד {position_delta:.1f} מקומות ואיבד "
                f"{abs(clicks_pct):.0%} מהקליקים, בזמן שהאתר זז {site_trend:+.0%}"
            ),
            confidence="high" if abs(drop_vs_site) > 0.25 else "medium",
            confidence_reason=(
                f"ירידה של {abs(drop_vs_site):.0%} מעבר למגמת האתר"
                if abs(drop_vs_site) > 0.25
                else "הירידה קרובה למגמת האתר, ייתכן שאינה ייחודית לדף"
            ),
            **common,
        )

    return DecayVerdict(
        cause="unclear",
        explanation=(
            f"הקליקים ירדו ב-{abs(clicks_pct):.0%} בלי דפוס ברור במיקום "
            f"({position_delta:+.1f}) או בהופעות ({impressions_pct:+.0%})"
        ),
        confidence="low",
        confidence_reason="אין סיגנל מבחין — נדרשת מדידה נוספת לפני פעולה",
        **common,
    )


#: Clicks lost is not clicks recoverable. Acting on a seasonal dip returns
#: nothing, so ranking it by what was lost would push real work down the list.
#: Effort reflects the work each fix actually takes, not whether it is worth it.
RECOVERY = {
    "position_loss": (1.0, "m"),   # refresh the content
    "serp_takeover": (1.0, "m"),   # read the competitor, then refresh
    "deindexed":     (1.0, "s"),   # usually a robots, canonical or sitemap fix
    "cannibalized":  (0.7, "l"),   # consolidation recovers most, and costs a lot
    "seasonality":   (0.0, "s"),   # nothing to recover by acting
    "unclear":       (0.0, "s"),   # unknown until measured again
}


def to_finding(verdict: DecayVerdict, client: str, conversion_rate: float | None = None) -> Finding:
    """Turn a verdict into a ranked, evidence-backed finding."""
    recovery_rate, effort = RECOVERY[verdict.cause]
    recoverable = verdict.clicks_lost * recovery_rate

    severity = "high" if recoverable >= 50 else "medium"
    if verdict.cause == "deindexed":
        severity = "blocker"
    elif recoverable == 0:
        severity = "low"

    action = {
        "position_loss": {"kind": "refresh_content",
                          "target_queries": verdict.worst_queries,
                          "note": "רענון התוכן סביב השאילתות שאיבדו הכי הרבה"},
        "serp_takeover": {"kind": "study_then_refresh",
                          "target_queries": verdict.worst_queries,
                          "note": "לקרוא את הדף שעקף לפני כתיבה"},
        "seasonality":   {"kind": "schedule_for_season",
                          "note": "לא לגעת עכשיו; לתזמן חיזוק לפני העונה הבאה"},
        "cannibalized":  {"kind": "consolidate",
                          "note": "להחליט איזה דף קנוני ולמזג"},
        "deindexed":     {"kind": "investigate_indexing",
                          "note": "לבדוק כיסוי ו-URL Inspection"},
        "unclear":       {"kind": "measure_again",
                          "note": "להמתין למחזור מדידה נוסף"},
    }[verdict.cause]

    return Finding(
        skill="content-decay",
        client=client,
        type=f"decay_{verdict.cause}",
        severity=severity,                                  # type: ignore[arg-type]
        source="gsc_wizard",
        url=verdict.url,
        query=verdict.worst_queries[0] if verdict.worst_queries else None,
        impact_clicks=round(recoverable, 1),
        impact_conversions=(
            round(recoverable * conversion_rate, 1)
            if conversion_rate is not None else None
        ),
        impact_basis=(
            f"{verdict.clicks_lost} קליקים אבדו בהשוואת 28 יום מול אותה תקופה אשתקד; "
            f"מתוכם {recoverable:.0f} ניתנים להשבה בסיווג {verdict.cause}"
        ),
        confidence=verdict.confidence,
        confidence_reason=verdict.confidence_reason,
        effort=effort,
        evidence={
            "clicks_pct": round(verdict.clicks_pct, 3),
            "position_delta": round(verdict.position_delta, 2),
            "impressions_pct": round(verdict.impressions_pct, 3),
            "worst_queries": verdict.worst_queries,
        },
        action=action,
        baseline={
            "clicks_before": verdict.clicks_lost + 0,
            "position": round(verdict.position_delta, 2),
            "explanation": verdict.explanation,
        },
    )
