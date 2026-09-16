"""
Terminal output helpers.
========================

`log()` is carried over verbatim from the existing skills so that a run of a
new skill looks identical to a run of `gsc-crawl-checker`. The banner and
report helpers formalise the box-drawing style those scripts already use by
hand, at the same 55-character width.
"""

from __future__ import annotations

import sys
from datetime import datetime

RULE_WIDTH = 55


def log(msg: str, level: str = "INFO") -> None:
    icons = {"INFO": "ℹ️ ", "OK": "✅", "WARN": "⚠️ ", "ERR": "❌", "WAIT": "⏳", "SKIP": "⏭️ "}
    icon = icons.get(level, "•")
    timestamp = datetime.now().strftime("%H:%M:%S")
    print(f"[{timestamp}] {icon} {msg}", flush=True)


def banner(title: str) -> None:
    """A framed section heading, e.g. around a report."""
    print("\n" + "═" * RULE_WIDTH)
    print(f"  {title}")
    print("═" * RULE_WIDTH)


def rule(char: str = "─") -> None:
    print(char * RULE_WIDTH)


def kv(label: str, value: object, width: int = 28) -> None:
    """A padded label/value line, so columns line up in a report."""
    print(f"  {label:<{width}}{value}")


def die(msg: str, code: int = 1) -> None:
    """Stop the run with a clear reason. Used when continuing would be unsafe."""
    log(msg, "ERR")
    sys.exit(code)
