"""
Gate 5 — what must still be true after the page is published.
=============================================================

These run against the live page immediately after a write. They are the only
checks that can trigger an automatic rollback, because each one detects a
definite break rather than an unfavourable measurement: the page errors, the
copy never rendered, a form vanished, the canonical moved.

Everything here is a pure function over fetched HTML, so the whole gate is
testable offline against saved pages.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Callable

from ..schema import Result

Fetcher = Callable[[str], tuple[int, str]]      # url -> (status, html)

_SCRIPT_OR_STYLE = re.compile(r"<(script|style)\b.*?</\1>", re.I | re.S)
_TAG             = re.compile(r"<[^>]+>")
_HEADINGS        = re.compile(r"<h([1-6])\b[^>]*>(.*?)</h\1>", re.I | re.S)
_FORMS           = re.compile(r"<form\b", re.I)
_CANONICAL       = re.compile(
    r"""<link[^>]+rel=["']canonical["'][^>]*href=["']([^"']+)""", re.I
)
_META_ROBOTS     = re.compile(
    r"""<meta[^>]+name=["']robots["'][^>]*content=["']([^"']+)""", re.I
)
_JSON_LD         = re.compile(
    r"""<script[^>]+type=["']application/ld\+json["'][^>]*>(.*?)</script>""",
    re.I | re.S,
)


@dataclass
class Check:
    name: str
    passed: bool
    detail: str
    breaking: bool = True       # a breaking failure justifies an auto rollback


@dataclass
class CheckReport:
    checks: list[Check]

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)

    @property
    def breaking_failures(self) -> list[Check]:
        return [c for c in self.checks if not c.passed and c.breaking]

    @property
    def warnings(self) -> list[Check]:
        return [c for c in self.checks if not c.passed and not c.breaking]

    @property
    def should_roll_back(self) -> bool:
        return bool(self.breaking_failures)

    def summary(self) -> str:
        if self.passed:
            return f"כל {len(self.checks)} הבדיקות עברו"
        parts = []
        if self.breaking_failures:
            parts.append(f"{len(self.breaking_failures)} כשלים חוסמים")
        if self.warnings:
            parts.append(f"{len(self.warnings)} אזהרות")
        return " · ".join(parts)


@dataclass
class Baseline:
    """What the page looked like before the write."""

    form_count: int
    canonical: str
    robots: str
    heading_count: int
    text_length: int

    @classmethod
    def capture(cls, html: str) -> "Baseline":
        return cls(
            form_count=len(_FORMS.findall(html)),
            canonical=_first_group(_CANONICAL, html),
            robots=_first_group(_META_ROBOTS, html),
            heading_count=len(_HEADINGS.findall(html)),
            text_length=len(visible_text(html)),
        )


def _first_group(pattern: re.Pattern[str], html: str) -> str:
    match = pattern.search(html)
    return match.group(1) if match else ""


def visible_text(html: str) -> str:
    """Text a reader would see, with scripts and markup removed."""
    without_code = _SCRIPT_OR_STYLE.sub(" ", html)
    return re.sub(r"\s+", " ", _TAG.sub(" ", without_code)).strip()


def run(
    url: str,
    expected_text: str,
    baseline: Baseline,
    fetch: Fetcher,
    internal_links: list[str] | None = None,
) -> CheckReport:
    """Run the whole post-publish gate against the live page."""
    checks: list[Check] = []

    try:
        status, html = fetch(url)
    except Exception as exc:
        return CheckReport([Check("הדף נטען", False, f"שגיאה בטעינה: {exc}")])

    checks.append(
        Check("הדף מחזיר 200", status == 200, f"סטטוס {status}")
    )
    if status != 200:
        return CheckReport(checks)        # nothing below is meaningful

    # The copy has to be in the HTML the crawler receives, not injected later.
    needle = re.sub(r"\s+", " ", visible_text(expected_text))[:80].strip()
    present = needle.lower() in visible_text(html).lower()
    checks.append(
        Check(
            "הטקסט קיים ב-HTML הגולמי",
            present,
            "נמצא" if present else f"לא נמצא: {needle[:50]!r} — "
            "ייתכן שהוא מוזרק ב-JS ולא ייסרק",
        )
    )

    checks.append(_check_forms(html, baseline))
    checks.append(_check_canonical(html, baseline))
    checks.append(_check_robots(html, baseline))
    checks.append(_check_layout(html, baseline))
    checks.append(_check_headings(html))
    checks.append(_check_json_ld(html))

    for link in internal_links or []:
        checks.append(_check_link(link, fetch))

    return CheckReport(checks)


# ═══════════════════════════════════════════════════════
#  Individual checks
# ═══════════════════════════════════════════════════════

def _check_forms(html: str, baseline: Baseline) -> Check:
    """A form that disappeared is a lost conversion path, not a style issue."""
    now = len(_FORMS.findall(html))
    ok = now >= baseline.form_count
    return Check(
        "הטפסים במקומם",
        ok,
        f"{now} טפסים (היו {baseline.form_count})"
        + ("" if ok else " — נעלם טופס, זו דרך המרה שאבדה"),
    )


def _check_canonical(html: str, baseline: Baseline) -> Check:
    now = _first_group(_CANONICAL, html)
    ok = now == baseline.canonical
    return Check("הקנוניקל לא זז", ok, f"{now!r} (היה {baseline.canonical!r})")


def _check_robots(html: str, baseline: Baseline) -> Check:
    now = _first_group(_META_ROBOTS, html)
    became_noindex = "noindex" in now.lower() and "noindex" not in baseline.robots.lower()
    return Check(
        "meta robots לא השתנה",
        not became_noindex,
        f"{now!r} (היה {baseline.robots!r})"
        + (" — הדף הפך ל-noindex" if became_noindex else ""),
    )


def _check_layout(html: str, baseline: Baseline) -> Check:
    """Catch a template that collapsed: the page loads, but most of it is gone.

    Content was added, so the text should grow. Losing a third of it means
    something other than our paragraph changed.
    """
    now = len(visible_text(html))
    ok = now >= baseline.text_length * 0.67
    return Check(
        "העיצוב לא נשבר",
        ok,
        f"{now} תווים גלויים (היו {baseline.text_length})"
        + ("" if ok else " — חלק ניכר מהדף נעלם"),
    )


def _check_headings(html: str) -> Check:
    """Heading order should not skip levels, and there should be exactly one H1."""
    levels = [int(m.group(1)) for m in _HEADINGS.finditer(html)]
    if not levels:
        return Check("היררכיית כותרות", True, "אין כותרות", breaking=False)

    h1_count = levels.count(1)
    skips = [
        (a, b) for a, b in zip(levels, levels[1:]) if b > a + 1
    ]
    ok = h1_count <= 1 and not skips
    detail = f"{h1_count} כותרות H1"
    if skips:
        detail += f" · דילוג רמה: H{skips[0][0]}→H{skips[0][1]}"
    return Check("היררכיית כותרות", ok, detail, breaking=False)


def _check_json_ld(html: str) -> Check:
    """Broken structured data costs rich results, but does not break the page."""
    blocks = _JSON_LD.findall(html)
    if not blocks:
        return Check("Schema תקין", True, "אין JSON-LD בדף", breaking=False)
    for block in blocks:
        try:
            json.loads(block)
        except json.JSONDecodeError as exc:
            return Check("Schema תקין", False, f"JSON-LD שבור: {exc}", breaking=False)
    return Check("Schema תקין", True, f"{len(blocks)} בלוקים תקינים", breaking=False)


def _check_link(url: str, fetch: Fetcher) -> Check:
    try:
        status, _ = fetch(url)
    except Exception as exc:
        return Check(f"קישור {url}", False, f"שגיאה: {exc}")
    return Check(f"קישור {url}", status == 200, f"סטטוס {status}")


def as_result(report: CheckReport) -> Result:
    """Fold the report into the decision the caller has to make."""
    if report.passed:
        return Result.success("checks_passed", report.summary(), report=report)
    if report.should_roll_back:
        return Result.failure(
            "checks_failed",
            "כשל טכני אחרי הפרסום: "
            + "; ".join(f"{c.name} — {c.detail}" for c in report.breaking_failures),
            recoverable=True,
            report=report,
        )
    return Result.success(
        "checks_passed_with_warnings", report.summary(), report=report
    )
