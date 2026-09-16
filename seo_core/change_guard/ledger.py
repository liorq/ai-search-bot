"""
The change log.
===============

One append-only record per change, per client. It is what makes a change
reviewable weeks later: what was written, what would undo it, which backup
protects it, and when it is next due to be measured.

Checkpoints are set at 14, 28 and 56 days. Fourteen is early enough to notice
something badly wrong, twenty-eight matches the Search Console reporting window
most comparisons use, and fifty-six gives a slow-moving page time to settle
before anyone concludes the change did nothing.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..schema import ChangeRecord, ChangeStatus, Result

CHECKPOINT_DAYS = (14, 28, 56)
LEDGER_NAME = "change_log.json"


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
    changes = _read(directory)
    for entry in changes:
        if entry.get("change_id") != change_id:
            continue
        for checkpoint in entry.get("checkpoints", []):
            if checkpoint.get("day") == day:
                checkpoint["status"] = outcome
                checkpoint["measured"] = measured
                _write(directory, changes)
                return Result.success(
                    "checkpoint_done", f"{change_id} יום {day}: {outcome}"
                )
        return Result.failure("no_checkpoint", f"אין נקודת מדידה ביום {day}")
    return Result.failure("not_found", f"שינוי {change_id} לא נמצא ביומן")
