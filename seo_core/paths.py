"""
Where the toolkit keeps things, and whether that place is safe.
==============================================================

One directory holds everything that is not code: the `.env` with the API keys
and WordPress passwords, the client registry, and per-client data. It defaults
to `~/.claude/seo`, and `SEO_HOME` moves it.

Moving it is supported and warned about, because the obvious place to move it —
the Desktop — is the one place almost guaranteed to be synced to somebody's
cloud. `warnings_for()` names the specific problem rather than refusing: it is
the user's machine and the user's decision, but it should be an informed one.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

DEFAULT_HOME = Path.home() / ".claude" / "seo"

#: Path fragments that mean "this folder is replicated to a server somewhere".
#: A credential dropped in one of these leaves the machine within seconds.
SYNCED_MARKERS = (
    "library/mobile documents",     # iCloud Drive
    "/icloud",
    "/dropbox",
    "/onedrive",
    "/google drive",
    "/googledrive",
    "/pcloud",
    "/mega",
    "/yandex.disk",
)


def home() -> Path:
    """The toolkit's data directory. `SEO_HOME` overrides the default."""
    override = os.environ.get("SEO_HOME", "").strip()
    return Path(override).expanduser() if override else DEFAULT_HOME


def env_path() -> Path:
    return home() / ".env"


def registry_path() -> Path:
    return home() / "clients.json"


def data_dir(domain: str) -> Path:
    return home() / "data" / domain


def desktop() -> Path | None:
    """The user's Desktop, if there is one. Localised names are display-only."""
    for candidate in (Path.home() / "Desktop", Path.home() / "OneDrive" / "Desktop"):
        if candidate.is_dir():
            return candidate
    return None


def is_synced(path: Path) -> bool:
    """Whether this path sits inside a folder that replicates to a cloud."""
    text = str(path).lower()
    return any(marker in text for marker in SYNCED_MARKERS)


def warnings_for(path: Path | None = None) -> list[str]:
    """Everything wrong with where the secrets currently live.

    Empty means the location is sound. Nothing here blocks a run — the point is
    that a bad choice should be visible, not silently tolerated.
    """
    target = path or home()
    problems: list[str] = []

    if is_synced(target):
        problems.append(
            f"{target} נמצא בתיקייה מסונכרנת לענן — המפתחות ייצאו מהמחשב "
            "ויישמרו אצל ספק הסנכרון, כולל היסטוריית גרסאות"
        )

    desk = desktop()
    if desk and (target == desk or desk in target.parents):
        problems.append(
            "הקבצים יושבים על שולחן העבודה — זה מה שנראה בכל שיתוף מסך, "
            "וברוב המחשבים הוא מסונכרן לענן"
        )

    secrets_file = target / ".env"
    if secrets_file.exists():
        mode = secrets_file.stat().st_mode
        if mode & (stat.S_IRGRP | stat.S_IROTH):
            problems.append(
                f"{secrets_file} קריא למשתמשים אחרים במחשב — "
                f"הרץ chmod 600 עליו"
            )

    if (target / ".git").exists():
        problems.append(
            f"{target} הוא מאגר git — קובץ .env כאן עלול להידחף בטעות"
        )

    return problems
