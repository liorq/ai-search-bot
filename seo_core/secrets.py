"""
Credential loading and leak prevention.
=======================================

Until now this toolkit needed no credentials at all — every skill inherited
its authentication from an already-logged-in Chrome profile. Writing to live
WordPress sites and calling paid APIs changes that, so there is now exactly
one `.env`, kept outside the repository.

`scan_for_leaks()` exists because the failure it prevents is unrecoverable:
a client's application password committed to git has to be treated as
compromised even after the commit is removed.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from . import paths

#: Snapshot taken at import for anything that just wants a default to show.
#: Functions resolve the path freshly instead, so SEO_HOME still applies
#: when it is set after this module is imported.
ENV_PATH = paths.env_path()

#: Every credential the toolkit can use, and whether a run can proceed without
#: it. Absent-but-optional degrades a skill; absent-but-required stops it.
KNOWN_KEYS: dict[str, str] = {
    "GSC_WIZARD_API_KEY":   "GSC Wizard MCP — נתוני Search Console",
    "DATAFORSEO_LOGIN":     "DataForSEO — תוצאות SERP",
    "DATAFORSEO_PASSWORD":  "DataForSEO — תוצאות SERP",
    "CLARITY_API_TOKEN":    "Microsoft Clarity — נתוני התנהגות",
    "PAGESPEED_API_KEY":    "PageSpeed Insights — מהירות ו-CWV",
    "GA4_CREDENTIALS_PATH": "GA4 — קובץ service account להמרות",
}

#: Patterns that look like a live credential sitting in source code.
_LEAK_PATTERNS = [
    # A prefixed name is the common shape of a real leak (DATAFORSEO_PASSWORD),
    # so the identifier may carry leading word characters before the keyword.
    (re.compile(r"""(?i)\w*(api[_-]?key|secret|passwd|password|token)\w*\s*[=:]\s*["'][^"'\s]{12,}["']"""),
     "מפתח או סיסמה מוטמעים בקוד"),
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}"),        "מפתח Google API"),
    (re.compile(r"\bghp_[0-9A-Za-z]{30,}"),           "טוקן GitHub"),
    (re.compile(r"(?i)\bxox[baprs]-[0-9A-Za-z\-]{10,}"), "טוקן Slack"),
    # WordPress application passwords: five groups of four, space separated.
    (re.compile(r"\b(?:[A-Za-z0-9]{4}\s){5}[A-Za-z0-9]{4}\b"),
     "סיסמת אפליקציה של WordPress"),
]

#: Assignments that read from the environment are the correct pattern, not a leak.
_SAFE_LINE = re.compile(r"(?i)os\.environ|getenv|env:|auth_env|example|CHANGEME|<[^>]+>")


class SecretsError(Exception):
    """Raised when credentials are missing or when one was found in source."""


def load_env(path: Path | None = None) -> dict[str, str]:
    """Read the .env into os.environ without overwriting anything already set.

    Values already present in the environment win, so a one-off override on the
    command line behaves the way anyone would expect.
    """
    env_file = path or paths.env_path()
    loaded: dict[str, str] = {}
    if not env_file.exists():
        return loaded

    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("\"'")
        loaded[key] = value
        os.environ.setdefault(key, value)
    return loaded


def require(*keys: str) -> None:
    """Stop before doing any work if a needed credential is absent."""
    load_env()
    missing = [k for k in keys if not os.environ.get(k)]
    if missing:
        lines = [f"  - {k}  ({KNOWN_KEYS.get(k, 'לא מתועד')})" for k in missing]
        raise SecretsError(
            "חסרים מפתחות ב-" + str(paths.env_path()) + ":\n" + "\n".join(lines)
        )


def available() -> dict[str, bool]:
    """Which credentials are present. Used by --self-check."""
    load_env()
    return {key: bool(os.environ.get(key)) for key in KNOWN_KEYS}


def scan_for_leaks(root: Path) -> list[tuple[Path, int, str]]:
    """Find credentials committed into source. Returns (file, line, reason)."""
    hits: list[tuple[Path, int, str]] = []
    # `tests` and `fixtures` are excluded on purpose: that is exactly where
    # fake credentials belong, including the ones proving this scanner works.
    skip_dirs = {
        ".git", "__pycache__", "node_modules", ".venv", "venv",
        "fixtures", "tests",
    }

    for path in root.rglob("*"):
        if not path.is_file() or path.suffix not in {".py", ".json", ".sh", ".md", ".yml", ".yaml"}:
            continue
        if path.name in {".env.example", "secrets.py"} or set(path.parts) & skip_dirs:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue

        for lineno, line in enumerate(text.splitlines(), start=1):
            if _SAFE_LINE.search(line):
                continue
            for pattern, reason in _LEAK_PATTERNS:
                if pattern.search(line):
                    hits.append((path, lineno, reason))
                    break
    return hits


def assert_no_leaks(root: Path) -> None:
    """Refuse to run if a credential is sitting in the source tree."""
    hits = scan_for_leaks(root)
    if hits:
        lines = [f"  - {p}:{n} — {why}" for p, n, why in hits]
        raise SecretsError(
            "נמצאו מפתחות בתוך הקוד. העבר אותם ל-.env לפני שממשיכים:\n"
            + "\n".join(lines)
        )
