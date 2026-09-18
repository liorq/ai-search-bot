"""
Gates 2 and 3 — compose, then get approval before anything is written.
======================================================================

Every change becomes a plan document first: what changes, why, what it puts at
risk, and the exact diff. Lior reads it and approves. Only then can the change
be published.

The approval is bound to the plan's content, not merely to its id. If the plan
is regenerated after approval — different copy, a different target — the old
approval no longer applies, because what he agreed to is no longer what would
be written.
"""

from __future__ import annotations

import difflib
import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..schema import Finding, Result, content_hash
from .risk import RiskReport


@dataclass
class ChangePlan:
    """A proposed change, pending approval."""

    plan_id: str
    client: str
    skill: str
    url: str
    post_id: int
    post_type: str
    builder: str

    summary: str
    rationale: str
    payload: dict[str, Any]
    inverse: dict[str, Any]

    before_text: str
    after_text: str
    before_hash: str

    risk_level: str
    risk_reasons: list[str] = field(default_factory=list)
    protected_queries: list[str] = field(default_factory=list)
    findings: list[dict[str, Any]] = field(default_factory=list)

    #: Anything the skill that wrote the plan needs back at publish time and
    #: that is not part of the payload — the target of an internal link, for
    #: instance, which has to be checked for a 404 after the write. Outside
    #: the fingerprint on purpose: it describes the change, it is not the
    #: change, so adding it cannot invalidate an approval.
    context: dict[str, Any] = field(default_factory=dict)

    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    @property
    def fingerprint(self) -> str:
        """Identity of what would actually be written.

        An approval is tied to this, so regenerating the plan with different
        copy invalidates it rather than silently inheriting consent.
        """
        return content_hash(self.payload, self.url, self.post_id)

    @property
    def diff(self) -> str:
        return "\n".join(
            difflib.unified_diff(
                self.before_text.splitlines(),
                self.after_text.splitlines(),
                fromfile="לפני",
                tofile="אחרי",
                lineterm="",
            )
        )

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["fingerprint"] = self.fingerprint
        return d


def compose(
    *,
    plan_id: str,
    client: str,
    skill: str,
    url: str,
    post_id: int,
    post_type: str,
    builder: str,
    summary: str,
    rationale: str,
    payload: dict[str, Any],
    inverse: dict[str, Any],
    before_text: str,
    after_text: str,
    before_hash: str,
    risk: RiskReport,
    findings: list[Finding] | None = None,
    context: dict[str, Any] | None = None,
) -> Result:
    """Build a plan. Refuses to propose a change that does nothing."""
    if not payload:
        return Result.failure("empty_plan", "אין מה לשנות — payload ריק")
    if not inverse:
        return Result.failure(
            "no_inverse",
            "אין inverse — בלי דרך לבטל, השינוי לא יוצא לדרך",
            recoverable=False,
        )
    if before_text.strip() == after_text.strip():
        return Result.failure("no_change", "הטקסט אחרי זהה לטקסט לפני")

    plan = ChangePlan(
        plan_id=plan_id, client=client, skill=skill, url=url, post_id=post_id,
        post_type=post_type, builder=builder, summary=summary, rationale=rationale,
        payload=payload, inverse=inverse, before_text=before_text,
        after_text=after_text, before_hash=before_hash,
        risk_level=risk.level, risk_reasons=list(risk.reasons),
        protected_queries=[q.query for q in risk.protected],
        findings=[f.to_dict() for f in (findings or [])],
        context=dict(context or {}),
    )
    return Result.success("planned", f"תוכנית {plan_id} מוכנה לאישור", plan=plan)


# ═══════════════════════════════════════════════════════
#  The plan document
# ═══════════════════════════════════════════════════════

def render(plan: ChangePlan) -> str:
    """The Hebrew document Lior reads before approving."""
    risk_icon = {"red": "🔴", "amber": "🟡", "green": "🟢"}.get(plan.risk_level, "⚪")

    lines = [
        f"# תוכנית שינוי — {plan.plan_id}",
        "",
        f"**לקוח:** {plan.client}  ",
        f"**דף:** {plan.url}  ",
        f"**סקיל:** `{plan.skill}`  ",
        f"**בונה:** {plan.builder}  ",
        f"**נוצר:** {plan.created_at}",
        "",
        "## מה משתנה",
        "",
        plan.summary,
        "",
        "## למה",
        "",
        plan.rationale,
        "",
        f"## סיכון — {risk_icon} {plan.risk_level}",
        "",
    ]

    if plan.risk_reasons:
        lines += [f"- {reason}" for reason in plan.risk_reasons]
    else:
        lines.append("לא זוהה סיכון לשאילתות קיימות.")

    if plan.protected_queries:
        shown = plan.protected_queries[:10]
        lines += [
            "",
            f"**שאילתות מוגנות בדף ({len(plan.protected_queries)}):**",
            "",
            *[f"- `{q}`" for q in shown],
        ]
        if len(plan.protected_queries) > len(shown):
            lines.append(f"- …ועוד {len(plan.protected_queries) - len(shown)}")

    lines += ["", "## ה-diff המלא", "", "```diff", plan.diff, "```", ""]

    if plan.risk_level == "red":
        lines += [
            "> ⚠️ **דורש אישור כפול.** השינוי נוגע בטקסט שנושא שאילתה מוגנת.",
            "",
        ]

    lines += [
        "## אישור",
        "",
        "```bash",
        f"python -m seo_core.change_guard.plan approve {plan.plan_id}",
        "```",
        "",
        "בלי אישור, הפרסום מסרב לרוץ.",
    ]
    return "\n".join(lines)


def save(plan: ChangePlan, directory: Path) -> tuple[Path, Path]:
    """Write the plan as both a readable document and machine-readable JSON."""
    directory.mkdir(parents=True, exist_ok=True)
    doc = directory / f"{plan.plan_id}.md"
    raw = directory / f"{plan.plan_id}.json"
    doc.write_text(render(plan), encoding="utf-8")
    with open(raw, "w", encoding="utf-8") as fh:
        json.dump(plan.to_dict(), fh, ensure_ascii=False, indent=2)
    return doc, raw


# ═══════════════════════════════════════════════════════
#  Approval
# ═══════════════════════════════════════════════════════

def _store_path(directory: Path) -> Path:
    return directory / "approvals.json"


def _load_store(directory: Path) -> dict[str, str]:
    path = _store_path(directory)
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError:
        return {}


def approve(plan: ChangePlan, directory: Path) -> Result:
    """Record consent for exactly this plan's content."""
    store = _load_store(directory)
    store[plan.plan_id] = plan.fingerprint
    directory.mkdir(parents=True, exist_ok=True)
    with open(_store_path(directory), "w", encoding="utf-8") as fh:
        json.dump(store, fh, ensure_ascii=False, indent=2)
    return Result.success("approved", f"תוכנית {plan.plan_id} אושרה")


def is_approved(plan: ChangePlan, directory: Path) -> Result:
    """Check consent, and that the plan has not changed since it was given."""
    store = _load_store(directory)
    recorded = store.get(plan.plan_id)

    if recorded is None:
        return Result.failure(
            "not_approved",
            f"תוכנית {plan.plan_id} לא אושרה. קרא את {plan.plan_id}.md ואשר",
            recoverable=True,
        )
    if recorded != plan.fingerprint:
        return Result.failure(
            "plan_changed",
            f"תוכנית {plan.plan_id} השתנתה מאז האישור — "
            "מה שאושר אינו מה שייכתב. צריך אישור מחדש",
            recoverable=True,
        )
    return Result.success("approved", "אושר")


def load(plan_id: str, directory: Path) -> Result:
    """Read a saved plan back from disk."""
    path = directory / f"{plan_id}.json"
    if not path.exists():
        return Result.failure("plan_missing", f"תוכנית {plan_id} לא נמצאה ב-{directory}")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except json.JSONDecodeError as exc:
        return Result.failure("plan_corrupt", f"קובץ התוכנית פגום: {exc}", recoverable=False)

    raw.pop("fingerprint", None)          # derived, not a field
    try:
        return Result.success("loaded", "התוכנית נטענה", plan=ChangePlan(**raw))
    except TypeError as exc:
        return Result.failure("plan_corrupt", f"מבנה התוכנית לא תקין: {exc}", recoverable=False)


# ═══════════════════════════════════════════════════════
#  CLI — the approval command the plan document points at
# ═══════════════════════════════════════════════════════

def main(argv: list[str] | None = None) -> int:
    import argparse

    from ..log import log

    parser = argparse.ArgumentParser(description="Change plan approval")
    parser.add_argument("action", choices=["approve", "show"], help="פעולה: approve או show")
    parser.add_argument("plan_id", help="מזהה התוכנית, למשל plan_001")
    parser.add_argument(
        "--dir",
        default=str(Path.home() / ".claude" / "seo" / "plans"),
        help="תיקיית התוכניות (ברירת מחדל: ~/.claude/seo/plans)",
    )
    args = parser.parse_args(argv)
    directory = Path(args.dir)

    loaded = load(args.plan_id, directory)
    if not loaded:
        log(loaded.detail, "ERR")
        return 1

    change_plan = loaded.data["plan"]
    if args.action == "show":
        print(render(change_plan))
        return 0

    if change_plan.risk_level == "red":
        log("התוכנית מסומנת אדום — קרא את הסיכון לפני שאתה מאשר", "WARN")
        for reason in change_plan.risk_reasons:
            log(f"  {reason}", "WARN")
        answer = input("  ➤ לאשר בכל זאת? (כתוב 'כן'): ").strip()
        if answer != "כן":
            log("האישור בוטל", "SKIP")
            return 1

    approve(change_plan, directory)
    log(f"תוכנית {args.plan_id} אושרה — אפשר לפרסם", "OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
