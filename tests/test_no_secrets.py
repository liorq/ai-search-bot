"""
The repository itself must be clean.
====================================

`secrets.scan_for_leaks` already knows what a credential looks like, and
`tests/test_clients.py` proves it recognises one. What was missing is anyone
running it over this repository.

It was a habit — grep before every commit — and a habit is not a guarantee.
A test is, because the suite runs before every commit anyway.

Two further checks live here because they catch the mistake most likely in
this particular toolkit: a `clients.json` whose `auth_env` holds a password
instead of the *name* of the environment variable that holds it, and a
`.gitignore` that stopped covering the files the whole design depends on
staying untracked.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from seo_core import secrets  # noqa: E402

_ENV_VAR_NAME = re.compile(r"[A-Z][A-Z0-9_]*")


def test_the_repository_contains_no_credentials():
    hits = secrets.scan_for_leaks(REPO)
    listed = "\n  ".join(
        f"{path.relative_to(REPO)}:{line} — {reason}" for path, line, reason in hits
    )
    assert not hits, f"נמצאו מפתחות בתוך הריפו:\n  {listed}"


def test_auth_env_names_a_variable_and_does_not_hold_a_password():
    """`auth_env` is the name of an environment variable, never its value.

    A registry that carries the password itself would work perfectly, which
    is what makes it dangerous: nothing would fail, and the credential would
    ride along into every copy of the file.
    """
    offences: list[str] = []

    for path in REPO.rglob("clients*.json"):
        if ".git" in path.parts:
            continue
        registry = json.loads(path.read_text(encoding="utf-8"))
        for domain, entry in registry.items():
            if not isinstance(entry, dict):
                continue                       # "_comment" and friends
            name = (entry.get("cms") or {}).get("auth_env", "")
            if name and not _ENV_VAR_NAME.fullmatch(name):
                offences.append(
                    f"{path.relative_to(REPO)} / {domain}: "
                    f"auth_env={name!r} אינו שם של משתנה סביבה"
                )

    assert not offences, "\n  ".join(offences)


def test_the_gitignore_still_covers_the_files_that_hold_secrets():
    ignored = (REPO / ".gitignore").read_text(encoding="utf-8").split()
    for pattern in (".env", "clients.json"):
        assert pattern in ignored, f"{pattern} חייב להיות ב-.gitignore"
