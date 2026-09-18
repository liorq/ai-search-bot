#!/usr/bin/env python3
"""
SEO Audit Report — תור עבודה אחד מכל מה שהסקילים מצאו
================================================================
אוסף את קבצי הממצאים של כל הסקילים ללקוח אחד, מוריד ספירה כפולה, ומייצר
תוכנית עבודה מדורגת לצוות: מה השבוע, מה החודש, ומה רק לעקוב.

ההפרדה המרכזית: **תקלות ודאיות מעל אומדנים.** קישור שבור הוא עובדה; "הרחבת
המענה שווה 80 קליקים" הוא מודל. שניהם שווים עבודה, ורק אחד מהם בטוח.

שימוש:
    python seo_audit_report.py --client example.com
    python seo_audit_report.py --client example.com --ceiling-ctr 0.16
    python seo_audit_report.py --client example.com --out audit.md
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# seo_core יושב ב-~/.claude/seo_core אחרי install.sh; בפיתוח — בשורש הריפו.
for candidate in (
    Path.home() / ".claude" / "seo_core",
    Path(__file__).resolve().parents[3],
):
    if (candidate / "seo_core").is_dir():
        sys.path.insert(0, str(candidate))
        break

from seo_core import clients, paths, report, secrets                     # noqa: E402
from seo_core.log import banner, count, hours, kv, log, rule                           # noqa: E402
from seo_core.sources import queries as gsc                              # noqa: E402

MAX_LISTED = 8
SEVERITY_ICONS = {"blocker": "🛑", "high": "🔴", "medium": "🟡", "low": "⚪"}


def ceiling_from_queries(path: str | None) -> float | None:
    """עקומת CTR שנמדדה מהאתר, אם סופקה — עדיפה על ממוצע תעשייה."""
    if not path:
        return None
    loaded = gsc.load_export(Path(path))
    if not loaded:
        log(loaded.detail, "WARN")
        return None
    curve = gsc.build_curve(loaded.data["rows"])
    if curve.source != "site":
        log("אין מספיק נתונים לעקומה של האתר — נשארים בממוצע התעשייה", "WARN")
        return None
    log(curve.describe(), "OK")
    return curve.expected(gsc.BEST_CLAIMABLE_POSITION)


def run(domain: str, out: str | None, ceiling: float | None,
        queries_path: str | None) -> int:
    clients.load(domain)          # מאמת שהלקוח מוגדר לפני שקוראים משהו
    base = paths.data_dir(domain)
    reports_dir = base / "reports"

    measured = ceiling_from_queries(queries_path)
    ceiling_ctr = ceiling or measured or report.CEILING_CTR

    portfolio = report.build(domain, reports_dir, ceiling_ctr)
    if not (portfolio.faults or portfolio.estimates):
        banner(f"📋 אודיט — {domain}")
        log(f"אין ממצאים ב-{reports_dir}", "WARN")
        log("הרץ קודם את הסקילים עצמם — כל אחד כותב לשם את הממצאים שלו",
            "INFO")
        return 0

    buckets = report.triage(portfolio)

    banner(f"📋 אודיט — {domain}")
    kv("תקלות ודאיות", len(portfolio.faults))
    kv("אומדנים", len(portfolio.estimates))
    kv("דפים מעורבים", len(portfolio.pages))
    kv("קליקים בפוטנציאל", f"{portfolio.total_claim:,.0f}")
    if portfolio.double_counted > 0.5:
        kv("ספירה כפולה שהוסרה", f"{portfolio.double_counted:,.0f}")
    kv("תקרת CTR", f"{ceiling_ctr:.1%} "
       f"({'נמדדה מהאתר' if measured and not ceiling else 'ממוצע תעשייה'})")
    rule()

    counts = portfolio.by_skill()
    if counts:
        print("\n  לפי סקיל")
        for skill, found in counts.items():
            print(f"     {found:>3}  {skill}")

    for name in report.BUCKETS:
        records = buckets.get(name) or []
        if not records:
            continue
        print(f"\n  {report.BUCKET_TITLES[name]} — "
              f"{count(len(records), 'פריט אחד', 'פריטים')}, "
              f"{hours(report.planned_hours(records))}")
        for record in records[:MAX_LISTED]:
            icon = SEVERITY_ICONS.get(record.get("severity", "low"), "•")
            clicks = float(record.get("impact_clicks") or 0)
            claim = f"+{clicks:>5.0f} קליקים  " if clicks else "   תקלה     "
            print(f"     {icon} {claim}{record.get('type')}  "
                  f"[{record.get('skill')}]")
            if record.get("url"):
                print(f"             {record['url']}")
        if len(records) > MAX_LISTED:
            print(f"     … ועוד {count(len(records) - MAX_LISTED, 'פריט אחד', 'פריטים')} במסמך")

    capped = [p for p in portfolio.pages if p.was_capped]
    if capped:
        print(f"\n  ♻️  דפים שכמה סקילים תובעים עליהם — "
              f"{count(len(capped), 'דף אחד', 'דפים')}")
        for page in capped[:MAX_LISTED]:
            print(f"     {page.url}\n           {page.describe()}")

    unknown = [p for p in portfolio.pages if p.headroom is None]
    if unknown:
        log(f"{count(len(unknown), 'דף אחד', 'דפים')} בלי נתוני הופעות — האומדן לא מוגבל בתקרה",
            "WARN")

    document = report.render_markdown(portfolio, buckets)
    path = report.save(document,
                       Path(out) if out else reports_dir / "audit.md")

    rule()
    print(f"\n  📄 המסמך המלא: {path}")
    print("\n  זה אודיט לצוות. לדוח החודשי ללקוח — `seo-monthly-report`.\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SEO Audit Report")
    parser.add_argument("--client", required=True, help="דומיין הלקוח מ-clients.json")
    parser.add_argument("--out", help="נתיב למסמך (ברירת מחדל: reports/audit.md)")
    parser.add_argument("--queries",
                        help="קובץ שאילתות — כדי לגזור תקרת CTR מהאתר עצמו")
    parser.add_argument("--ceiling-ctr", type=float,
                        help="תקרת CTR ידנית, למשל 0.16")
    args = parser.parse_args(argv)

    secrets.load_env()

    try:
        return run(args.client, args.out, args.ceiling_ctr, args.queries)
    except clients.ClientConfigError as exc:
        log(str(exc), "ERR")
        return 1
    except KeyboardInterrupt:
        log("הופסק", "SKIP")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
