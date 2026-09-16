"""
The rollback decision.
======================

Three tiers, and the distinction between them is the whole point:

    technical    the page is broken. Restore automatically — but only when a
                 verified backup exists and nobody else has edited since.
    performance  a protected query lost ground. Never automatic. Report, and
                 let Lior decide.
    inconclusive no readable signal. Keep watching; touch nothing.

A ranking drop twenty-eight days after a change is correlation. The site's own
trend moved, control pages moved, and a core update may have landed inside the
window. Reverting on that signal alone would mean undoing good work roughly as
often as bad.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from ..schema import ChangeRecord, Result

Trigger = Literal["technical", "performance"]
Action = Literal["auto_rollback", "halt", "report_only", "keep_watching"]

#: A protected query has to lose this much, net of the site-wide trend, before
#: the drop is even worth reporting. Below it, this is ordinary movement.
MATERIAL_POSITION_DROP = 1.5
MATERIAL_CLICK_LOSS_PCT = 0.20

#: Control pages are untouched pages on the same site. Attribution is judged on
#: *excess* loss — how much worse this page did than the controls — because a
#: page that fell 40% while the rest of the site fell 42% did not fall because
#: of the change.
CONTROL_EXCESS_LOSS_PCT = 0.20


@dataclass
class Decision:
    action: Action
    reason: str
    confidence: Literal["high", "medium", "low"] = "medium"
    details: list[str] = field(default_factory=list)

    @property
    def is_automatic(self) -> bool:
        return self.action == "auto_rollback"

    @property
    def needs_human(self) -> bool:
        return self.action in ("halt", "report_only")


def on_technical_failure(change: ChangeRecord, failures: list[str]) -> Decision:
    """The page is broken. Restore it — if restoring is actually safe."""
    if not change.backup_verified:
        return Decision(
            "halt",
            "הדף שבור אבל אין גיבוי מאומת — שחזור מגיבוי שלא נבדק "
            "עלול להחמיר. עצור והתערב ידנית",
            confidence="high",
            details=failures,
        )
    if not change.inverse:
        return Decision(
            "halt",
            "הדף שבור ואין inverse לשחזור. עצור והתערב ידנית",
            confidence="high",
            details=failures,
        )
    return Decision(
        "auto_rollback",
        "כשל טכני אחרי הפרסום, יש גיבוי מאומת — משחזר אוטומטית",
        confidence="high",
        details=failures,
    )


def on_concurrent_edit(change: ChangeRecord, current_hash: str) -> Decision:
    """Someone else edited the post. Their work is not ours to discard."""
    return Decision(
        "halt",
        f"התוכן השתנה מאז הצילום ({change.before_hash} → {current_hash}). "
        "מישהו ערך את הדף בינתיים — שחזור היה מוחק גם את העבודה שלו",
        confidence="high",
    )


def on_performance_change(
    *,
    protected_deltas: dict[str, dict[str, float]],
    site_trend_pct: float,
    control_trend_pct: float,
    algorithm_update_in_window: bool,
    days_elapsed: int,
) -> Decision:
    """Judge a measured change in rankings. Never returns auto_rollback.

    `protected_deltas` maps each protected query to its movement, e.g.
    ``{"garage door repair": {"position_delta": 2.4, "clicks_pct": -0.31}}``
    where a positive position delta means the page moved down.
    """
    if days_elapsed < 14:
        return Decision(
            "keep_watching",
            f"עברו {days_elapsed} ימים בלבד — מוקדם מדי למסקנה",
            confidence="low",
        )

    hurt = {
        query: delta
        for query, delta in protected_deltas.items()
        if delta.get("position_delta", 0) >= MATERIAL_POSITION_DROP
        or delta.get("clicks_pct", 0) <= -MATERIAL_CLICK_LOSS_PCT
    }

    if not hurt:
        return Decision(
            "keep_watching",
            "אף שאילתה מוגנת לא ירדה משמעותית",
            confidence="high" if days_elapsed >= 28 else "medium",
        )

    details = [
        f"{query}: מיקום {delta.get('position_delta', 0):+.1f}, "
        f"קליקים {delta.get('clicks_pct', 0):+.0%}"
        for query, delta in sorted(hurt.items())
    ]

    # If the untouched control pages fell about as far, the change is not the
    # cause. What matters is the excess: how much worse this page did.
    if control_trend_pct <= -MATERIAL_CLICK_LOSS_PCT:
        worst = min(d.get("clicks_pct", 0) for d in hurt.values())
        excess = worst - control_trend_pct
        if excess > -CONTROL_EXCESS_LOSS_PCT:
            return Decision(
                "keep_watching",
                f"דפי הביקורת ירדו ב-{control_trend_pct:+.0%} והדף ב-{worst:+.0%} — "
                "הירידה כלל-אתרית ולא נובעת מהשינוי הזה",
                confidence="medium",
                details=details,
            )

    if algorithm_update_in_window:
        return Decision(
            "report_only",
            "שאילתות מוגנות ירדו, אבל היה עדכון אלגוריתם בחלון המדידה — "
            "אי אפשר לייחס את הירידה לשינוי. הנתונים למטה, ההחלטה שלך",
            confidence="low",
            details=details,
        )

    confidence = "medium" if days_elapsed >= 28 else "low"
    if days_elapsed >= 56 and abs(site_trend_pct) < 0.1:
        confidence = "high"

    return Decision(
        "report_only",
        f"{len(hurt)} שאילתות מוגנות ירדו אחרי {days_elapsed} ימים, "
        f"בזמן שהאתר בכללותו זז {site_trend_pct:+.0%}. "
        "שחזור לא מתבצע אוטומטית — ההחלטה שלך",
        confidence=confidence,
        details=details,
    )


def execute(decision: Decision, change: ChangeRecord, restore_fn: Any) -> Result:
    """Carry out an automatic rollback. Refuses anything else.

    Deliberately narrow: this function cannot be talked into reverting a
    performance decision, whatever the caller passes.
    """
    if not decision.is_automatic:
        return Result.failure(
            "not_automatic",
            f"ההחלטה היא {decision.action!r} ולא שחזור אוטומטי: {decision.reason}",
            recoverable=True,
            decision=decision,
        )
    if not change.safe_to_auto_rollback:
        return Result.failure(
            "unsafe_rollback",
            "שחזור אוטומטי נחסם — חסר גיבוי מאומת או inverse",
            recoverable=False,
        )

    result = restore_fn()
    if not result:
        return Result.failure(
            "rollback_failed",
            f"השחזור נכשל: {result.detail}. "
            "הדף נשאר במצב שבור — נדרשת התערבות ידנית מיידית",
            recoverable=False,
        )
    return Result.success("rolled_back", "הדף שוחזר")
