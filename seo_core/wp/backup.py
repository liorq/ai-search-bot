"""
Backups, and proving they restore.
==================================

A backup nobody has checked is not a backup. The rule here is that a backup
must be *verified* before the write it protects is allowed to proceed, and
verification is structural: the snapshot has to carry everything a restore
would need, and the restore payload has to be one the client would actually
accept.

That matters most for Elementor, where a snapshot that captured `post_content`
but not `_elementor_data` looks complete and restores nothing that anyone sees.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..schema import Result
from .content import ELEMENTOR_DATA_KEY, PostContent

BACKUP_VERSION = 1


@dataclass
class Backup:
    """Everything needed to put a post back exactly as it was."""

    backup_id: str
    created_at: str
    post_id: int
    post_type: str
    builder: str
    url: str
    content: str
    meta: dict[str, Any] = field(default_factory=dict)
    revision_id: int | None = None
    version: int = BACKUP_VERSION

    def restore_payload(self) -> dict[str, Any]:
        """The write that undoes everything, shaped for `update_post`."""
        payload: dict[str, Any] = {}
        if self.builder == "elementor":
            # post_content is a dead copy on an Elementor page; restoring it
            # alone would leave the page visually unchanged.
            payload["meta"] = dict(self.meta)
        else:
            payload["content"] = self.content
            if self.meta:
                payload["meta"] = dict(self.meta)
        return payload

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def create(
    content: PostContent, url: str, backup_id: str, meta_keys: list[str] | None = None
) -> Backup:
    """Snapshot a post before it is written to.

    `meta_keys` narrows what is stored to the fields the pending write will
    touch, plus whatever the builder needs to render. Storing every meta key a
    site happens to carry would drag unrelated plugin state into the restore.
    """
    keys = set(meta_keys or [])
    if content.builder == "elementor":
        # `_elementor_data` only. `_elementor_css` is not exposed in REST, so
        # it always read back as empty — a backup of a value we never held,
        # and a comparison that could only ever be meaningless. Elementor
        # regenerates it from the tree anyway; it is a cache, not content.
        keys.add(ELEMENTOR_DATA_KEY)

    source = content.meta or {}
    snapshot = {key: source.get(key, "") for key in keys}

    return Backup(
        backup_id=backup_id,
        created_at=datetime.now(timezone.utc).isoformat(),
        post_id=content.post_id,
        post_type=content.post_type,
        builder=content.builder,
        url=url,
        content=content.raw_content,
        meta=snapshot,
    )


def verify(backup: Backup) -> Result:
    """Prove the backup could actually restore, before the write happens."""
    problems: list[str] = []

    if backup.version != BACKUP_VERSION:
        problems.append(f"גרסת גיבוי לא מוכרת: {backup.version}")
    if not backup.post_id:
        problems.append("חסר post_id")

    if backup.builder == "elementor":
        tree = backup.meta.get(ELEMENTOR_DATA_KEY)
        if not tree:
            problems.append(
                "גיבוי של דף Elementor בלי _elementor_data — "
                "שחזור ממנו לא יחזיר שום דבר שנראה על המסך"
            )
        else:
            try:
                parsed = json.loads(tree) if isinstance(tree, str) else tree
                if not isinstance(parsed, list):
                    problems.append("_elementor_data בגיבוי אינו מערך")
            except json.JSONDecodeError:
                problems.append("_elementor_data בגיבוי אינו JSON תקין")
    elif not backup.content.strip():
        problems.append("הגיבוי ריק מתוכן")

    if not backup.restore_payload():
        problems.append("לא נוצר payload לשחזור")

    if problems:
        return Result.failure(
            "backup_unusable",
            "הגיבוי לא ניתן לשחזור:\n  - " + "\n  - ".join(problems),
            recoverable=False,
            problems=problems,
        )
    return Result.success("verified", "הגיבוי אומת כשחזורי", backup=backup)


def save(backup: Backup, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{backup.backup_id}.json"
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(backup.to_dict(), fh, ensure_ascii=False, indent=2)
    return path


def load(path: Path) -> Result:
    if not path.exists():
        return Result.failure(
            "backup_missing", f"קובץ הגיבוי לא נמצא: {path}", recoverable=False
        )
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return Result.success("loaded", "הגיבוי נטען", backup=Backup(**json.load(fh)))
    except (json.JSONDecodeError, TypeError) as exc:
        return Result.failure(
            "backup_corrupt", f"קובץ הגיבוי פגום: {exc}", recoverable=False
        )


def restore(client: Any, backup: Backup) -> Result:
    """Put the post back. Verifies the backup again first, on principle.

    Re-verifying costs nothing and covers the case where the file was written
    correctly and then damaged on disk between the write and the rollback.
    """
    check = verify(backup)
    if not check:
        return check

    payload = backup.restore_payload()
    result = client.update_post(
        backup.post_id,
        post_type=backup.post_type,
        content=payload.get("content"),
        meta=payload.get("meta"),
    )
    if not result:
        return Result.failure(
            "restore_failed",
            f"השחזור נכשל: {result.detail}. "
            "עצור והתערב ידנית — אל תנסה שוב אוטומטית",
            recoverable=False,
            underlying=result.code,
        )
    return Result.success("restored", f"הדף שוחזר מגיבוי {backup.backup_id}")
