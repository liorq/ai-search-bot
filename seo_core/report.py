"""
One work queue out of everything the skills found.
==================================================

Seven skills each write their own findings file, and every one of them is
right on its own terms. Put side by side they produce a number that is not.
One page — 1,500 impressions, 60 clicks, position 6.2 — collects four:

    onpage-optimizer     expanding the answer is worth 80 clicks
    internal-anchors     one editorial link in; linking it properly, 45
    ctr-titles           2% where 5% is par; a new title, 60
    click-depth          five clicks from home; moving it up, 25

That is 210 clicks from a page that would earn 165 in total if it reached
position 3 and every searcher behaved like the average. All four estimates
describe the same page climbing the same results list, and adding them is
how a plan promises a year of growth in a quarter.

So claims are **pooled per URL and capped at what the page could earn at
all**: its impressions times the best click-through rate a page can expect.
The largest claim keeps its full value, and the rest share whatever headroom
is left. This is the same rule the speed skill applies to overlapping
PageSpeed opportunities, for the same reason.

The cap is not always binding, and that is the point: a page with plenty of
unearned impressions really can take several fixes. The rule removes the
arithmetic that was never possible, not the optimism that was.

Findings that claim no clicks — a broken link, a noindex left on after a
migration — are not pooled. They are faults, they are ranked by severity,
and they sit above the estimates because they are the only things here that
are certain.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .log import count, hours as hours_label
from .schema import Finding, SEVERITIES
from .sources.queries import REFERENCE_CTR

#: The best click-through rate a page can realistically be held to. Taken
#: from the reference curve at position 3, because the last two places are
#: won with brand and links rather than with anything in this toolkit.
#:
#: It is a published average, so it is generous for a weak brand and mean for
#: a strong one. Every skill measures the client's own curve where it can;
#: this is the one place that cannot, because a findings file on disk does not
#: carry one. A caller with a measured curve should pass its value in.
CEILING_CTR = REFERENCE_CTR[3]

#: Rough hours per effort level, so a plan can be read as a week's work
#: rather than as a wish list.
EFFORT_HOURS = {"s": 1.0, "m": 4.0, "l": 16.0}

#: A week's worth. Above this the "now" bucket stops being a plan.
WEEKLY_BUDGET_HOURS = 12.0

#: Findings below this confidence never lead a plan, whatever they claim.
LEADS_PLAN = ("high", "medium")

BUCKETS = ("now", "soon", "watch")

BUCKET_TITLES = {
    "now":   "השבוע",
    "soon":  "החודש",
    "watch": "לעקוב",
}


# ═══════════════════════════════════════════════════════
#  Reading what the skills wrote
# ═══════════════════════════════════════════════════════

def collect(directory: Path) -> list[dict[str, Any]]:
    """Every finding every skill has written for this client.

    Findings are read as dictionaries rather than rebuilt into `Finding`
    objects: the constructor validates, and a report that refuses to run
    because one skill wrote one incomplete record is a report nobody sees.
    Whatever is unreadable is skipped and counted.
    """
    found: list[dict[str, Any]] = []
    if not directory.exists():
        return found

    for path in sorted(directory.glob("*findings*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        for record in payload.get("findings") or []:
            if isinstance(record, dict) and record.get("skill"):
                record.setdefault("_source_file", path.name)
                found.append(record)
    return found


def dedupe(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse the same finding raised twice, keeping the stronger one."""
    best: dict[tuple, dict[str, Any]] = {}
    for record in records:
        key = (record.get("client"), record.get("url"),
               record.get("query"), record.get("type"))
        current = best.get(key)
        if current is None or _priority(record) > _priority(current):
            best[key] = record
    return list(best.values())


def _priority(record: dict[str, Any]) -> float:
    return float(record.get("priority") or 0.0)


# ═══════════════════════════════════════════════════════
#  Pooling — the same page, counted once
# ═══════════════════════════════════════════════════════

@dataclass
class PooledPage:
    """One URL, everything proposed for it, and what it can actually earn."""

    url: str
    findings: list[dict[str, Any]] = field(default_factory=list)
    headroom: float | None = None            # None = we could not work it out
    raw_claim: float = 0.0
    pooled_claim: float = 0.0

    @property
    def was_capped(self) -> bool:
        return self.pooled_claim < self.raw_claim - 0.5

    def describe(self) -> str:
        fixes = count(len(self.findings), "תיקון אחד", "תיקונים")
        if not self.was_capped:
            return f"{self.pooled_claim:.0f} קליקים מ-{fixes}"
        return (
            f"{self.pooled_claim:.0f} קליקים במקום {self.raw_claim:.0f} — "
            f"{fixes} על אותו דף מזיזים אותו ברשימה אחת"
        )


def _baseline_numbers(record: dict[str, Any]) -> tuple[float, float] | None:
    """Impressions and current clicks, from wherever the skill put them."""
    for source in (record.get("baseline") or {}, record.get("evidence") or {}):
        impressions = source.get("impressions")
        if impressions:
            return float(impressions), float(source.get("clicks") or 0)
    return None


def pool(records: Iterable[dict[str, Any]], ceiling_ctr: float = CEILING_CTR
         ) -> list[PooledPage]:
    """Group click estimates by URL and cap each page at what it could earn.

    The largest estimate for a page keeps its full value — it is the one most
    likely to be the real constraint — and the others are fitted into whatever
    headroom is left, in order. A page whose impressions we cannot find is
    left uncapped rather than capped on a guess, and says so.
    """
    pages: dict[str, PooledPage] = {}
    for record in records:
        url = record.get("url")
        if not url or not record.get("impact_clicks"):
            continue
        pages.setdefault(url, PooledPage(url=url)).findings.append(record)

    for page in pages.values():
        page.findings.sort(key=lambda r: float(r["impact_clicks"]), reverse=True)
        page.raw_claim = sum(float(r["impact_clicks"]) for r in page.findings)

        numbers = next(
            (n for n in (_baseline_numbers(r) for r in page.findings) if n), None)
        if numbers is None:
            page.headroom = None
            page.pooled_claim = page.raw_claim
            continue

        impressions, clicks = numbers
        page.headroom = max(0.0, impressions * ceiling_ctr - clicks)
        page.pooled_claim = min(page.raw_claim, page.headroom)

    return sorted(pages.values(), key=lambda p: p.pooled_claim, reverse=True)


# ═══════════════════════════════════════════════════════
#  Triage
# ═══════════════════════════════════════════════════════

@dataclass
class Portfolio:
    client: str
    faults: list[dict[str, Any]] = field(default_factory=list)
    estimates: list[dict[str, Any]] = field(default_factory=list)
    pages: list[PooledPage] = field(default_factory=list)
    unreadable: int = 0
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def total_claim(self) -> float:
        """The honest number: pooled, not summed."""
        return sum(p.pooled_claim for p in self.pages)

    @property
    def naive_claim(self) -> float:
        return sum(p.raw_claim for p in self.pages)

    @property
    def double_counted(self) -> float:
        return max(0.0, self.naive_claim - self.total_claim)

    def by_skill(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for record in self.faults + self.estimates:
            counts[record["skill"]] = counts.get(record["skill"], 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: kv[1], reverse=True))

    def by_severity(self) -> dict[str, int]:
        counts = {level: 0 for level in SEVERITIES}
        for record in self.faults + self.estimates:
            level = record.get("severity", "low")
            counts[level] = counts.get(level, 0) + 1
        return counts

    def summary(self) -> str:
        return (
            f"{count(len(self.faults), 'תקלה ודאית אחת', 'תקלות ודאיות')} · "
            f"{count(len(self.estimates), 'אומדן אחד', 'אומדנים')} · "
            f"{self.total_claim:.0f} קליקים בפוטנציאל"
        )


def build(client: str, directory: Path,
          ceiling_ctr: float = CEILING_CTR) -> Portfolio:
    records = dedupe(collect(directory))

    faults = [r for r in records if not r.get("impact_clicks")]
    estimates = [r for r in records if r.get("impact_clicks")]

    faults.sort(key=lambda r: (SEVERITIES.index(r.get("severity", "low")),
                               -_priority(r)))
    estimates.sort(key=_priority, reverse=True)

    return Portfolio(
        client=client, faults=faults, estimates=estimates,
        pages=pool(estimates, ceiling_ctr),
    )


def triage(portfolio: Portfolio) -> dict[str, list[dict[str, Any]]]:
    """Split the work into a week, a month, and a watch list.

    Faults come first and in full: they are certain, they are usually small,
    and leaving a 404 in place for a month to make room for a rewrite is the
    wrong trade every time.

    After that the week is filled by priority until the hours run out, and a
    finding the skill was not confident about never leads — it can be done,
    it just cannot be what the week is built around.
    """
    buckets: dict[str, list[dict[str, Any]]] = {name: [] for name in BUCKETS}

    spent = 0.0
    for record in portfolio.faults:
        cost = EFFORT_HOURS.get(record.get("effort", "m"), 4.0)
        urgent = record.get("severity") in ("blocker", "high")
        if urgent or spent + cost <= WEEKLY_BUDGET_HOURS:
            buckets["now"].append(record)
            spent += cost
        else:
            buckets["soon"].append(record)

    for record in portfolio.estimates:
        cost = EFFORT_HOURS.get(record.get("effort", "m"), 4.0)
        confident = record.get("confidence") in LEADS_PLAN
        if confident and spent + cost <= WEEKLY_BUDGET_HOURS:
            buckets["now"].append(record)
            spent += cost
        elif confident:
            buckets["soon"].append(record)
        else:
            buckets["watch"].append(record)

    return buckets


def planned_hours(records: Iterable[dict[str, Any]]) -> float:
    return sum(EFFORT_HOURS.get(r.get("effort", "m"), 4.0) for r in records)


# ═══════════════════════════════════════════════════════
#  The document
# ═══════════════════════════════════════════════════════

def render_markdown(portfolio: Portfolio, buckets: dict[str, list[dict[str, Any]]]
                    ) -> str:
    """The audit as a document for the team — technical, ranked, and honest.

    Deliberately not the monthly client report. That one says what was done,
    warmly; this one says what is broken, in the order it should be fixed.
    """
    stamp = portfolio.generated_at.strftime("%Y-%m-%d")
    lines = [
        f"# אודיט SEO — {portfolio.client}",
        "",
        f"נוצר {stamp} · {portfolio.summary()}",
        "",
        "## התמונה",
        "",
        "| | |",
        "|---|---|",
        f"| תקלות ודאיות | {len(portfolio.faults)} |",
        f"| אומדנים | {len(portfolio.estimates)} |",
        f"| דפים מעורבים | {len(portfolio.pages)} |",
        f"| קליקים בפוטנציאל | **{portfolio.total_claim:.0f}** |",
    ]

    if portfolio.double_counted > 0.5:
        lines += [
            f"| סכום נאיבי | {portfolio.naive_claim:.0f} |",
            "",
            f"> {portfolio.double_counted:.0f} קליקים מהסכום הנאיבי הם ספירה "
            "כפולה: כמה תיקונים על אותו דף מזיזים אותו באותה רשימת תוצאות "
            "אחת. המספר למעלה כבר מקוזז.",
        ]

    counts = portfolio.by_skill()
    if counts:
        lines += ["", "## לפי סקיל", "", "| סקיל | ממצאים |", "|---|---|"]
        lines += [f"| `{skill}` | {count} |" for skill, count in counts.items()]

    for name in BUCKETS:
        records = buckets.get(name) or []
        if not records:
            continue
        lines += [
            "", f"## {BUCKET_TITLES[name]} — "
                f"{count(len(records), 'פריט אחד', 'פריטים')} "
                f"({hours_label(planned_hours(records))})", "",
        ]
        for record in records:
            lines += _render_record(record)

    lines += [
        "", "---", "",
        "כל אומדן כאן נושא את הבסיס שלו ואת רמת הביטחון. ממצא ברמת ביטחון "
        "`low` לא מוביל תוכנית עבודה — הוא נבדק, לא מובטח.",
        "",
    ]
    return "\n".join(lines)


def _render_record(record: dict[str, Any]) -> list[str]:
    icon = {"blocker": "🛑", "high": "🔴", "medium": "🟡", "low": "⚪"}.get(
        record.get("severity", "low"), "•")
    clicks = float(record.get("impact_clicks") or 0)
    claim = f"+{clicks:.0f} קליקים · " if clicks else ""
    head = f"### {icon} {record.get('type')} — {claim}`{record.get('skill')}`"

    lines = [head, ""]
    if record.get("url"):
        lines.append(f"- **דף:** {record['url']}")
    if record.get("query"):
        lines.append(f"- **שאילתה:** {record['query']}")
    lines += [
        f"- **הבסיס:** {record.get('impact_basis', '')}",
        f"- **ביטחון:** {record.get('confidence')} — "
        f"{record.get('confidence_reason', '')}",
        f"- **מאמץ:** {record.get('effort')} "
        f"({hours_label(EFFORT_HOURS.get(record.get('effort', 'm'), 4.0))})",
    ]
    action = record.get("action") or {}
    if action.get("instruction"):
        lines.append(f"- **מה לעשות:** {action['instruction']}")
    lines.append("")
    return lines


def save(markdown: str, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(markdown, encoding="utf-8")
    return path
