"""
The overlap map cites code. Every citation has to point at something real.
=========================================================================

`docs/gsc-wizard-overlap.md` is the document a decision gets made on, and a
row that names a function which does not exist is a row that argues for
keeping (or deleting) nothing. Every `path.py:name()` in it is checked
against a `def name(` in that file — methods included.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DOC = REPO / "docs" / "gsc-wizard-overlap.md"

_CITATION = re.compile(r"`(seo_core/[\w/]+\.py):(\w+)\(\)`")


def test_every_cited_function_exists():
    text = DOC.read_text(encoding="utf-8")
    citations = sorted(set(_CITATION.findall(text)))
    assert citations, "המפה לא מצטטת אף פונקציה — משהו השתנה בפורמט"

    missing = []
    for path, name in citations:
        source = REPO / path
        if not source.exists():
            missing.append(f"{path} — הקובץ לא קיים")
            continue
        if not re.search(rf"^\s*def {re.escape(name)}\(", source.read_text(encoding="utf-8"), re.M):
            missing.append(f"{path}:{name}() — אין def כזה")

    assert not missing, "ציטוטים שבורים במפה:\n  " + "\n  ".join(missing)


def test_the_map_never_marks_a_tool_absent_without_evidence():
    """The one rule of the document: unreachable is not the same as missing."""
    text = DOC.read_text(encoding="utf-8")
    for phrase in ("לא קיים אצלו", "אין לו כלי", "GSC Wizard לא עושה"):
        assert phrase not in text, f"המפה טוענת חסר בלי ראיה: {phrase!r}"
