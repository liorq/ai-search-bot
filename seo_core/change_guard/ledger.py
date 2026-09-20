"""
The change log.
===============

One append-only record per change, per client. It is what makes a change
reviewable weeks later: what was written, what would undo it, which backup
protects it, and when it is next due to be measured.

Checkpoints are set at 28, 56 and 84 days. Twenty-eight matches the Search
Console window most comparisons use and is the first read on whether anything
moved — a first performance check, not proof that the change caused it. When
that read cannot tell (too little traffic, no control, the site moved as much
as the page), the follow-up stays open and asks again at 56 and 84 days.
A checkpoint that can answer closes the follow-up.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..schema import ChangeRecord, ChangeStatus, Result

CHECKPOINT_DAYS = (28, 56, 84)
LEDGER_NAME = "change_log.json"

#: Verdicts that settle the question. Anything else leaves the follow-up open.
CONCLUSIVE = ("improved", "declined", "no_change")


def _path(directory: Path) -> Path:
    return directory / LEDGER_NAME


def _read(directory: Path) -> list[dict[str, Any]]:
    path = _path(directory)
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except json.JSONDecodeError:
        return []
    return data.get("changes", []) if isinstance(data, dict) else []


def _write(directory: Path, changes: list[dict[str, Any]]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = _path(directory)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(
            {"updated_at": datetime.now(timezone.utc).isoformat(), "changes": changes},
            fh,
            ensure_ascii=False,
            indent=2,
        )
    return path


def schedule_checkpoints(applied_at: datetime) -> list[dict[str, Any]]:
    return [
        {
            "due_at": (applied_at + timedelta(days=days)).isoformat(),
            "day": days,
            "status": "pending",
            "measured": None,
        }
        for days in CHECKPOINT_DAYS
    ]


def record(change: ChangeRecord, directory: Path) -> Result:
    """Append a change, or update it in place if it is already logged."""
    changes = _read(directory)
    entry = change.to_dict()

    for index, existing in enumerate(changes):
        if existing.get("change_id") == change.change_id:
            changes[index] = entry
            _write(directory, changes)
            return Result.success("updated", f"שינוי {change.change_id} עודכן ביומן")

    changes.append(entry)
    _write(directory, changes)
    return Result.success("recorded", f"שינוי {change.change_id} נרשם ביומן")


def load(directory: Path) -> list[dict[str, Any]]:
    return _read(directory)


def get(change_id: str, directory: Path) -> Result:
    for entry in _read(directory):
        if entry.get("change_id") == change_id:
            return Result.success("found", "נמצא", change=entry)
    return Result.failure("not_found", f"שינוי {change_id} לא נמצא ביומן")


def set_status(
    change_id: str, status: ChangeStatus, directory: Path, note: str = ""
) -> Result:
    changes = _read(directory)
    for entry in changes:
        if entry.get("change_id") != change_id:
            continue
        entry["status"] = status
        if note:
            entry.setdefault("notes", []).append(
                f"{datetime.now(timezone.utc).isoformat()} — {note}"
            )
        _write(directory, changes)
        return Result.success("status_set", f"{change_id} → {status}")
    return Result.failure("not_found", f"שינוי {change_id} לא נמצא ביומן")


def add_note(change_ids: list[str], note: str, directory: Path) -> Result:
    """Append a note to one or more changes.

    The log is the record of what was done to a page, and "somebody re-saved it
    in the editor afterwards" belongs in it just as much as the write did.
    """
    changes = _read(directory)
    if not changes:
        return Result.failure("ledger_empty", "אין יומן שינויים ללקוח הזה")

    wanted = set(change_ids)
    touched = 0
    for change in changes:
        if change.get("change_id") in wanted:
            change.setdefault("notes", []).append(note)
            touched += 1

    if not touched:
        return Result.failure(
            "not_found", f"אף אחד מהמזהים לא נמצא ביומן: {', '.join(sorted(wanted))}"
        )
    return Result.success("noted", f"{touched} שינויים עודכנו",
                          path=str(_write(directory, changes)), count=touched)


def due_checkpoints(directory: Path, now: datetime | None = None) -> list[dict[str, Any]]:
    """Changes with a measurement checkpoint that has come due.

    Changes already rolled back are skipped: there is nothing left to measure.
    """
    moment = now or datetime.now(timezone.utc)
    due: list[dict[str, Any]] = []

    for entry in _read(directory):
        if entry.get("status") in ("rolled_back", "planned"):
            continue
        for checkpoint in entry.get("checkpoints", []):
            if checkpoint.get("status") != "pending":
                continue
            try:
                due_at = datetime.fromisoformat(checkpoint["due_at"])
            except (KeyError, ValueError):
                continue
            if due_at <= moment:
                due.append({"change": entry, "checkpoint": checkpoint})
    return due


def complete_checkpoint(
    change_id: str, day: int, outcome: str, measured: dict[str, Any], directory: Path
) -> Result:
    """Close one checkpoint — and the later ones too, if this one could answer.

    An inconclusive read is not a result. It leaves 56 and 84 standing, because
    a page with little traffic often needs the longer window before anything
    can be said about it at all.
    """
    changes = _read(directory)
    for entry in changes:
        if entry.get("change_id") != change_id:
            continue
        checkpoints = entry.get("checkpoints", [])
        for checkpoint in checkpoints:
            if checkpoint.get("day") == day:
                checkpoint["status"] = outcome
                checkpoint["measured"] = measured
                closed = 0
                if outcome in CONCLUSIVE:
                    for later in checkpoints:
                        if later.get("day", 0) > day and later.get("status") == "pending":
                            later["status"] = "not_needed"
                            closed += 1
                _write(directory, changes)
                tail = f", {closed} מדידות המשך נסגרו" if closed else ""
                return Result.success(
                    "checkpoint_done", f"{change_id} יום {day}: {outcome}{tail}",
                    follow_up_open=outcome not in CONCLUSIVE)
        return Result.failure("no_checkpoint", f"אין נקודת מדידה ביום {day}")
    return Result.failure("not_found", f"שינוי {change_id} לא נמצא ביומן")


def next_checkpoint(entry: dict[str, Any]) -> dict[str, Any] | None:
    """The next measurement still owed on a change, if any."""
    pending = [c for c in entry.get("checkpoints", []) if c.get("status") == "pending"]
    return min(pending, key=lambda c: c.get("day", 0)) if pending else None


def applied_between(directory: Path, start: date, end: date) -> list[dict[str, Any]]:
    """Every change written to the live site inside a date window.

    The control that matters when traffic drops. Before an algorithm update
    is blamed for anything, the first question is what *we* changed in the
    same weeks — and that answer has been sitting in this file the whole time.
    """
    found: list[dict[str, Any]] = []
    for change in _read(directory):
        stamp = change.get("applied_at")
        if not stamp or change.get("status") == "planned":
            continue
        try:
            applied = datetime.fromisoformat(stamp).date()
        except ValueError:
            continue
        if start <= applied <= end:
            found.append(change)
    return sorted(found, key=lambda c: c["applied_at"])
