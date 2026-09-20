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


def count(n: int, one: str, many: str) -> str:
    """Hebrew does not say "1 פריטים".

    Every report in this toolkit prints counts, and the singular reads as a
    bug to anyone who speaks the language — which is the one person these
    reports are for.
    """
    return one if n == 1 else f"{n} {many}"


def hours(value: float) -> str:
    """A rough duration, in words that survive rounding to one hour."""
    if value <= 0:
        return "ללא"
    if value < 1.5:
        return "כשעה"
    if value < 2.5:
        return "כשעתיים"
    return f"~{value:.0f} שעות"
