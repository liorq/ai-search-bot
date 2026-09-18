#!/usr/bin/env python3
"""
Algorithm Update Watch — האם העדכון עשה את זה, או שאנחנו
================================================================
משווה שני חלונות של Search Console, מודד את התנודה הרגילה של האתר עצמו
מתוך פיזור השינויים בין הדפים, ורק אז מסתכל בלוח השנה.

הסדר הזה הוא כל העניין. דף נמדד קודם מול האתר שלו, ורק תזוזה שכל האתר עשה
מוצעת בכלל ללוח השנה — וזה מה שמונע מכל דף שלא התמזל מזלו בחודש שקט
להיתלות בעדכון ליבה.

שימוש:
    python algorithm_update_watch.py --client example.com \\
        --before before.json --after after.json \\
        --window 2025-03-01:2025-04-15 [--updates updates.json]
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

# seo_core יושב ב-~/.claude/seo_core אחרי install.sh; בפיתוח — בשורש הריפו.
for candidate in (
    Path.home() / ".claude" / "seo_core",
    Path(__file__).resolve().parents[3],
):
    if (candidate / "seo_core").is_dir():
        sys.path.insert(0, str(candidate))
        break

from seo_core import clients, paths, secrets                             # noqa: E402
from seo_core.change_guard import ledger                                 # noqa: E402
from seo_core.log import banner, count, kv, log, rule                    # noqa: E402
from seo_core.schema import save_findings                                # noqa: E402
from seo_core.sources import queries as gsc, updates as up               # noqa: E402

MAX_LISTED = 10
VERDICT_ICONS = {
    "site_wide": "🌐", "page_specific": "📄",
    "within_noise": "〰️ ", "unreadable": "🔇",
}


def parse_window(value: str) -> tuple:
    start, _, end = value.partition(":")
    return (datetime.strptime(start.strip(), "%Y-%m-%d").date(),
            datetime.strptime(end.strip(), "%Y-%m-%d").date())


def run(domain: str, before_path: Path, after_path: Path, window: str,
        updates_path: str | None) -> int:
    clients.load(domain)          # מאמת שהלקוח מוגדר לפני שקוראים משהו
    dirs = paths.data_dir(domain)

    try:
        start, end = parse_window(window)
    except ValueError:
        log("--window צריך להיות בפורמט 2025-03-01:2025-04-15", "ERR")
        return 1
    if end < start:
        log("סוף החלון לפני ההתחלה שלו", "ERR")
        return 1

    before = gsc.load_export(before_path)
    after = gsc.load_export(after_path)
    if not before:
        log(before.detail, "ERR")
        return 1
    if not after:
        log(after.detail, "ERR")
        return 1

    updates = up.load_updates(Path(updates_path) if updates_path else None)
    covered = up.coverage(end, updates)

    changes = up.compare(before.data["rows"], after.data["rows"])
    spread = up.spread_of(changes)
    matched = up.in_window(start, end, updates)
    diagnoses = up.diagnose(changes, start, end, updates)
    lost = up.losses(diagnoses)

    banner(f"📉 מה קרה — {domain}")
    kv("חלון", f"{start:%d/%m/%Y} – {end:%d/%m/%Y}")
    kv("דפים שהושוו", len(changes))
    kv("תנודה רגילה", spread.describe())
    kv("עדכונים בחלון", ", ".join(u.describe() for u in matched) or "אין ברשימה")
    rule()

    if not covered:
        log(covered.detail, "WARN")

    if not spread.measurable:
        log("אין מספיק דפים כדי למדוד את התנודה הרגילה של האתר. "
            "בלי זה אי אפשר להבדיל בין ירידה לרעש, והסקיל לא ינחש", "ERR")
        return 1

    total_before = sum(c.before for c in changes)
    total_after = sum(c.after for c in changes)
    kv("סך הקליקים", f"{total_before:,} → {total_after:,} "
                     f"({(total_after - total_before) / max(1, total_before):+.0%})")

    if not lost:
        rule()
        log("אף דף לא ירד מעבר לתנודה הרגילה של האתר", "OK")
        if matched:
            log(f"בחלון היה {matched[0].name}, והאתר לא הגיב אליו", "INFO")
        return 0

    site_wide = [d for d in lost if d.verdict == "site_wide"]
    page_specific = [d for d in lost if d.verdict == "page_specific"]

    if site_wide:
        print(f"\n  🌐 כל האתר זז יחד — "
              f"{count(len(site_wide), 'דף אחד', 'דפים')}")
        print(f"       {site_wide[0].describe()}")
        for diagnosis in site_wide[:MAX_LISTED]:
            print(f"     {diagnosis.page.delta:>6} קליקים  {diagnosis.page.url}")
            print(f"           {diagnosis.page.describe()}")

    if page_specific:
        print(f"\n  📄 דפים שירדו לבד — "
              f"{count(len(page_specific), 'דף אחד', 'דפים')}")
        print(f"       עדכון אלגוריתם לא מפיל דף אחד ומשאיר את השאר")
        for diagnosis in page_specific[:MAX_LISTED]:
            print(f"     {diagnosis.page.delta:>6} קליקים  {diagnosis.page.url}")
            print(f"           {diagnosis.page.describe()}")

    # הבקרה החשובה ביותר: מה *אנחנו* שינינו באותם שבועות.
    ours = ledger.applied_between(dirs, start, end)
    if ours:
        print(f"\n  ✍️  מה שאנחנו שינינו בחלון — "
              f"{count(len(ours), 'שינוי אחד', 'שינויים')}")
        dropped = {d.page.url for d in lost}
        for change in ours[:MAX_LISTED]:
            mark = "⚠️ " if change.get("url") in dropped else "  "
            print(f"     {mark} {change['applied_at'][:10]}  "
                  f"{change.get('skill')}  {change.get('url')}")
        overlap = [c for c in ours if c.get("url") in dropped]
        if overlap:
            print(f"\n       {count(len(overlap), 'דף אחד שירד', 'דפים שירדו')} "
                  "שונו על ידינו בתוך החלון. זה החשוד הראשון, לפני גוגל")
    else:
        print("\n  ✍️  לא נרשם שום שינוי שלנו בחלון הזה — "
              "מה שהשתנה, לא אנחנו שינינו אותו")

    findings = up.to_findings(diagnoses, domain)
    path = save_findings(findings, dirs / "reports" / "update_findings.json")

    rule()
    print("\n  ⚠️  חפיפה בין ירידה לחלון עדכון היא קורלציה. אין כאן מדידה של")
    print("      סיבתיות, וירידה בדירוג לא מפעילה שחזור אוטומטי בשום מצב.")
    print(f"\n  📄 ממצאים מלאים: {path}\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Algorithm Update Watch")
    parser.add_argument("--client", required=True, help="דומיין הלקוח מ-clients.json")
    parser.add_argument("--before", required=True, help="ייצוא GSC לחלון שלפני")
    parser.add_argument("--after", required=True, help="ייצוא GSC לחלון שאחרי")
    parser.add_argument("--window", required=True,
                        help="התאריכים שבין שני החלונות, 2025-03-01:2025-04-15")
    parser.add_argument("--updates", help="רשימת עדכונים עדכנית יותר מהמובנית")
    args = parser.parse_args(argv)

    secrets.load_env()

    try:
        return run(args.client, Path(args.before), Path(args.after),
                   args.window, args.updates)
    except clients.ClientConfigError as exc:
        log(str(exc), "ERR")
        return 1
    except FileNotFoundError as exc:
        log(f"קובץ לא נמצא: {exc}", "ERR")
        return 1
    except KeyboardInterrupt:
        log("הופסק", "SKIP")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
