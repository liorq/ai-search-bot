"""
What a run cost, in dollars, separated into measured and guessed.
=================================================================

DataForSEO bills per call. There is no cap here and no approval before each
one — that was decided deliberately — so the obligation is the other one:
every run ends by saying what it spent.

Two numbers, never added into one. `actual` is what the response reported in
its own `cost` field. `estimate` is what we assumed when the response did not
say, which is what happens when a call goes through the MCP connector rather
than the API. Presenting a guess beside a measurement without labelling which
is which is how a bill becomes a surprise.

Two things this does that are not budgeting: it refuses to send the same
request twice in one run, and it stops a run that has made an implausible
number of calls. Neither is a spending limit; both are loop guards.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import paths
from .schema import Result

LEDGER_NAME = "costs.json"

#: No single run of a skill legitimately makes this many paid calls. Past it,
#: something is looping — stop and say so rather than keep paying for it.
MAX_CALLS_PER_RUN = 200

#: Used only when a response does not report its own cost. Rough, and labelled
#: as rough everywhere it appears.
ESTIMATES = {
    "serp/google/organic/live/advanced": 0.0049,
    "dataforseo_labs": 0.011,
    "backlinks": 0.02,
    "on_page": 0.00125,
    "keywords_data": 0.05,
    "ai_optimization": 0.02,
}
FALLBACK_ESTIMATE = 0.01


def estimate_for(endpoint: str) -> float:
    for prefix, price in ESTIMATES.items():
        if endpoint.startswith(prefix) or prefix in endpoint:
            return price
    return FALLBACK_ESTIMATE


@dataclass
class Call:
    service: str
    endpoint: str
    cost: float
    measured: bool                    # did the response say, or did we assume?
    at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    note: str = ""


@dataclass
class RunCosts:
    """One skill run's spending. Created per run, reported at the end of it."""

    client: str
    skill: str
    calls: list[Call] = field(default_factory=list)
    _seen: dict[str, Any] = field(default_factory=dict)

    # ---------- during the run ----------

    def cached(self, key: str) -> Any | None:
        """A reply already paid for in this run, if the same thing was asked."""
        return self._seen.get(key)

    def remember(self, key: str, reply: Any) -> None:
        self._seen[key] = reply

    def may_call(self) -> Result:
        if len(self.calls) >= MAX_CALLS_PER_RUN:
            return Result.failure(
                "call_guard_tripped",
                f"{len(self.calls)} קריאות בתשלום בהרצה אחת — זה לא שימוש סביר "
                "אלא לולאה. ההרצה נעצרת, והעלות עד כאן מדווחת", recoverable=False)
        return Result.success("ok", "אפשר להמשיך")

    def record(self, service: str, endpoint: str, response: Any = None,
               note: str = "") -> Call:
        """Log one paid call, preferring what the response said it cost."""
        reported = None
        if isinstance(response, dict):
            raw = response.get("cost")
            if isinstance(raw, (int, float)):
                reported = float(raw)
        call = Call(service=service, endpoint=endpoint,
                    cost=reported if reported is not None else estimate_for(endpoint),
                    measured=reported is not None, note=note)
        self.calls.append(call)
        return call

    # ---------- at the end of it ----------

    @property
    def actual(self) -> float:
        return round(sum(c.cost for c in self.calls if c.measured), 4)

    @property
    def estimated(self) -> float:
        return round(sum(c.cost for c in self.calls if not c.measured), 4)

    def summary_lines(self) -> list[str]:
        """Printed at the end of every run, including one that failed."""
        if not self.calls:
            return ["עלות: לא בוצעו קריאות בתשלום"]
        measured = sum(1 for c in self.calls if c.measured)
        lines = [f"קריאות בתשלום: {len(self.calls)} "
                 f"({measured} עם עלות מדווחת, {len(self.calls) - measured} באומדן)",
                 f"עלות בפועל: ${self.actual:.4f}"]
        if self.estimated:
            lines.append(f"אומדן נוסף: ${self.estimated:.4f} — "
                         "הקריאות האלה לא החזירו עלות, המספר הוא הערכה")
        by_service: dict[str, float] = {}
        for call in self.calls:
            by_service[call.service] = round(by_service.get(call.service, 0) + call.cost, 4)
        lines.append("לפי שירות: " + ", ".join(f"{k} ${v:.4f}" for k, v in sorted(by_service.items())))
        return lines

    def save(self, directory: Path | None = None) -> Path:
        """Append this run to the client's cost log."""
        target = directory or paths.data_dir(self.client)
        target.mkdir(parents=True, exist_ok=True)
        path = target / LEDGER_NAME
        history = []
        if path.exists():
            try:
                history = json.loads(path.read_text(encoding="utf-8")).get("runs", [])
            except json.JSONDecodeError:
                history = []
        history.append({
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "skill": self.skill, "calls": len(self.calls),
            "actual": self.actual, "estimated": self.estimated,
            "detail": [vars(c) for c in self.calls],
        })
        path.write_text(json.dumps({"runs": history}, ensure_ascii=False, indent=1),
                        encoding="utf-8")
        return path


def month_to_date(client: str, directory: Path | None = None) -> dict[str, Any]:
    """What this client has cost so far this month. Reporting, not a limit."""
    path = (directory or paths.data_dir(client)) / LEDGER_NAME
    if not path.exists():
        return {"actual": 0.0, "estimated": 0.0, "runs": 0}
    try:
        runs = json.loads(path.read_text(encoding="utf-8")).get("runs", [])
    except json.JSONDecodeError:
        return {"actual": 0.0, "estimated": 0.0, "runs": 0}
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    mine = [r for r in runs if str(r.get("at", "")).startswith(month)]
    return {"actual": round(sum(r.get("actual", 0) for r in mine), 4),
            "estimated": round(sum(r.get("estimated", 0) for r in mine), 4),
            "runs": len(mine)}
