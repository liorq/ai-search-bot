"""
The limits that decide whether an import can even start.
========================================================

Every failed WordPress migration I have seen failed for one of four reasons,
and all four are knowable *before* the archive is built: the file is larger
than PHP will accept, larger than the migration plugin will accept, larger than
the memory limit can unpack, or larger than the free disk on the target.

So the numbers are gathered first and the archive is measured against them,
rather than discovering the ceiling halfway through an upload.

The 500MB cap is separate and stricter: it is a hard limit on the *final*
`.wpress`, after compression, not an estimate taken before it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ..schema import Result

#: Hard ceiling on the finished archive. Not negotiable, and not an estimate —
#: it is checked against the file on disk.
HARD_ARCHIVE_LIMIT = 500 * 1024 * 1024

#: Unpacking needs room for the archive plus what comes out of it, plus slack
#: for the database dump. Measured against free disk on both sides.
EXTRACTION_HEADROOM = 2.5

_UNITS = {"k": 1024, "m": 1024 ** 2, "g": 1024 ** 3, "t": 1024 ** 4}
_SIZE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*([kmgt]?)b?\s*$", re.I)


def parse_size(value: Any) -> int | None:
    """PHP shorthand to bytes. `None` means unlimited (`-1` or `0M`).

    PHP accepts `512M`, `1G`, `1048576` and `-1` in the same setting, and a
    naive `int()` on any of the first three silently produces nonsense.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return None if number < 0 else int(number)

    match = _SIZE.match(str(value))
    if not match:
        return None
    number, unit = float(match.group(1)), match.group(2).lower()
    if number < 0:
        return None
    return int(number * _UNITS.get(unit, 1))


def human(size: int | None) -> str:
    if size is None:
        return "ללא הגבלה"
    for unit, step in (("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024)):
        if size >= step:
            return f"{size / step:.1f}{unit}"
    return f"{size}B"


@dataclass
class ServerLimits:
    """What one WordPress install will actually accept.

    `collected` separates *unknown* from *unlimited*. Without it, a site whose
    limits could not be read looks identical to a site with no limits at all,
    and the archive gate waves through a 400MB file that `post_max_size` was
    always going to reject at 64MB.
    """

    site: str = ""
    collected: bool = False
    upload_max_filesize: int | None = None
    post_max_size: int | None = None
    memory_limit: int | None = None
    max_execution_time: int | None = None
    free_disk_bytes: int | None = None
    plugin_max_upload: int | None = None        # what the migration plugin displays
    php_version: str = ""
    wp_version: str = ""

    @property
    def ceilings(self) -> dict[str, int]:
        """Every upload ceiling that applies, unlimited ones dropped."""
        named = {
            "upload_max_filesize": self.upload_max_filesize,
            "post_max_size": self.post_max_size,
            "מגבלת התוסף": self.plugin_max_upload,
        }
        return {name: value for name, value in named.items() if value is not None}

    @property
    def import_ceiling(self) -> int | None:
        ceilings = self.ceilings
        return min(ceilings.values()) if ceilings else None

    @property
    def binding_constraint(self) -> str:
        ceilings = self.ceilings
        if not ceilings:
            return "אין מגבלה מוצהרת"
        return min(ceilings, key=lambda name: ceilings[name])

    def summary(self) -> str:
        if not self.collected:
            return (
                "המגבלות לא נאספו — WP-CLI לא היה זמין. "
                "קרא את המגבלה שהתוסף מציג והעבר אותה ידנית"
            )
        return (
            f"העלאה מרבית {human(self.import_ceiling)} "
            f"({self.binding_constraint}) · זיכרון {human(self.memory_limit)} · "
            f"דיסק פנוי {human(self.free_disk_bytes)}"
        )


def unknown(site: str = "", plugin_max_upload: Any = None) -> ServerLimits:
    """Limits we could not read. Optionally carrying the one a human read off
    the plugin's own import screen, which is the number that actually binds."""
    ceiling = parse_size(plugin_max_upload)
    return ServerLimits(
        site=site, plugin_max_upload=ceiling, collected=ceiling is not None
    )


def from_wp_cli(payload: dict[str, Any], site: str = "") -> ServerLimits:
    """Build limits from `wp config get` / `wp eval` output collected as JSON.

    Keys are accepted in PHP's own spelling so the collecting command can pass
    `ini_get()` results straight through.
    """
    return ServerLimits(
        site=site,
        collected=True,
        upload_max_filesize=parse_size(payload.get("upload_max_filesize")),
        post_max_size=parse_size(payload.get("post_max_size")),
        memory_limit=parse_size(payload.get("memory_limit")),
        max_execution_time=_as_int(payload.get("max_execution_time")),
        free_disk_bytes=parse_size(payload.get("free_disk_bytes")),
        plugin_max_upload=parse_size(payload.get("plugin_max_upload")),
        php_version=str(payload.get("php_version", "")),
        wp_version=str(payload.get("wp_version", "")),
    )


def _as_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return None if number <= 0 else number       # 0 means unlimited in PHP


def check_archive(archive_bytes: int, limits: ServerLimits) -> Result:
    """Can this exact file be imported into this exact site?

    Returns a failure naming the binding constraint, because "the import
    failed" is not something anyone can act on and "post_max_size is 64MB"
    is.
    """
    problems: list[str] = []

    if archive_bytes > HARD_ARCHIVE_LIMIT:
        problems.append(
            f"הקובץ {human(archive_bytes)} — מעל הגבול הקשיח של "
            f"{human(HARD_ARCHIVE_LIMIT)}"
        )

    ceiling = limits.import_ceiling
    if ceiling is not None and archive_bytes > ceiling:
        problems.append(
            f"הקובץ {human(archive_bytes)} גדול מהמגבלה בפועל "
            f"{human(ceiling)} ({limits.binding_constraint})"
        )

    needed = disk_needed(archive_bytes)
    if limits.free_disk_bytes is not None and limits.free_disk_bytes < needed:
        problems.append(
            f"נדרש {human(needed)} דיסק פנוי לפריסה, קיימים "
            f"{human(limits.free_disk_bytes)}"
        )

    if limits.memory_limit is not None and limits.memory_limit < 256 * 1024 ** 2:
        problems.append(
            f"memory_limit הוא {human(limits.memory_limit)} — "
            "ייבוא נוטה ליפול מתחת ל-256MB"
        )

    caveat = (
        "" if limits.collected else
        " — אבל מגבלות השרת לא נבדקו, אז זה רק הגבול הקשיח שלנו"
    )

    if problems:
        return Result.failure(
            "limits_exceeded",
            "הייבוא לא יעבור:\n  - " + "\n  - ".join(problems),
            recoverable=True,
            problems=problems,
            archive_bytes=archive_bytes,
            ceiling=ceiling,
        )

    return Result.success(
        "within_limits",
        f"{human(archive_bytes)} נכנס במגבלות ({human(ceiling)} מותר){caveat}",
        archive_bytes=archive_bytes,
        ceiling=ceiling,
        limits_known=limits.collected,
    )


def disk_needed(archive_bytes: int) -> int:
    """Free space the target needs: the archive plus what unpacks out of it."""
    return int(archive_bytes * EXTRACTION_HEADROOM)
