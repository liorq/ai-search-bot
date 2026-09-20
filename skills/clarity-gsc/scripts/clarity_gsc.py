#!/usr/bin/env python3
"""
clarity-gsc — איך הגיעו, ומה עשו אחר כך.
=========================================

Search Console נגמר בקליק. Clarity מתחיל אחריו. אף אחד מהם לא יודע להבדיל בין
דף שמביא תנועה והמבקרים נתקעים בו לבין דף שפשוט לא מדורג — וזה ההבדל שקובע
במה לטפל קודם.

הסקיל לא מושך היסטוריה מ-Clarity, כי אין כזו: ה-API עונה שלושה ימים אחורה.
הוא קורא את הצילומים היומיים שהמשימה המתוזמנת שומרת.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# seo_core יושב ב-~/.claude/seo_core אחרי ההתקנה; בפיתוח — בשורש הריפו.
# הסדר חשוב: מההתקנה, parents[3] הוא ~/.claude, ושם יש תיקיית seo_core שהיא
# העטיפה ולא החבילה — התאמה אליה נותנת ImportError.
for candidate in (
    Path.home() / ".claude" / "seo_core",
    Path(__file__).resolve().parents[3],
):
    if (candidate / "seo_core").is_dir():
        sys.path.insert(0, str(candidate))
        break

from seo_core import clients, secrets                                    # noqa: E402
from seo_core.log import banner, kv, log, rule                           # noqa: E402
from seo_core.sources import clarity, gsc_wizard, queries                # noqa: E402

MAX_PER_QUADRANT = 6
ICONS = {"wasted": "🔥", "working": "✅", "hidden": "👻", "quiet": "🤏"}
TITLES = {"wasted": "מביא תנועה, והמבקרים נתקעים",
          "working": "מביא תנועה, והחוויה תקינה",
          "hidden": "החוויה תקינה, אין תנועה",
          "quiet": "מעט מדי נתונים"}


def self_check(domain: str) -> int:
    banner(f"🔍 בדיקת מוכנות — {domain}")
    client = clients.load(domain)
    kv("נכס Search Console", client.gsc_property or "לא מוגדר")
    days = clarity.stored_days(domain)
    kv("ימי Clarity שמורים", len(days) or "אין — הפעל את המשימה המתוזמנת")
    if days:
        kv("טווח", f"{min(days)} .. {max(days)}")
    failures = clarity.open_failures(domain)
    if failures:
        log(f"{len(failures)} ימים לא נשמרו — {failures[-1]['detail'][:80]}", "WARN")
    ready = bool(client.gsc_property and days)
    log("מוכן" if ready else "חסרים נתונים — ראו למעלה", "OK" if ready else "WARN")
    return 0 if ready else 1


def run(domain: str, queries_path: str | None) -> int:
    client = clients.load(domain)

    # אזהרה לפני הממצאים: יום שלא נשמר לא יוזכר בשום שורה, ובלי זה
    # "לא נמצאו בעיות" נקרא כמו תקינות במקום כמו חוסר נתונים.
    failures = clarity.open_failures(domain)
    if failures:
        log(f"{len(failures)} ימי Clarity חסרים — התמונה חלקית. "
            f"האחרון: {failures[-1]['detail'][:90]}", "WARN")

    behaviour_days = clarity.load_days(domain)
    if not behaviour_days:
        log("אין אף צילום Clarity שמור. הרץ את המשימה המתוזמנת "
            f"(scripts/install_clarity_timer.ps1 -Client {domain}) והמתן ליום אחד", "ERR")
        return 1

    fetched = gsc_wizard.rows_for(client, queries_path)
    if not fetched:
        log(fetched.detail, "ERR")
        return 1
    if fetched.data["fetched"]:
        log(fetched.detail, "OK")
        for label, value in fetched.data["lines"]:
            kv(label, value)
    loaded = queries.load_export(fetched.data["path"])
    if not loaded:
        log(loaded.detail, "ERR")
        return 1

    newest = max(behaviour_days)
    pages = clarity.matrix(loaded.data["rows"], behaviour_days[newest])

    banner(f"🔀 תנועה מול התנהגות — {domain}")
    kv("יום ההתנהגות", newest.isoformat())
    kv("ימים שמורים", len(behaviour_days))
    kv("דפים שנמצאו בשני המקורות", len(pages))
    if not pages:
        log("אף דף לא הופיע גם ב-Search Console וגם ב-Clarity. "
            "זו חוסר חפיפה בנתונים, לא סימן שהכול תקין", "WARN")
        return 0
    rule()

    by_quadrant: dict[str, list] = {}
    for page in pages:
        by_quadrant.setdefault(page.quadrant, []).append(page)

    for name in ("wasted", "working", "hidden", "quiet"):
        group = by_quadrant.get(name)
        if not group:
            continue
        print(f"\n  {ICONS[name]} {TITLES[name]} — {len(group)} דפים")
        print(f"     {clarity._ACTIONS[name]}")
        for page in group[:MAX_PER_QUADRANT]:
            print(f"\n     {page.url}")
            print(f"       {page.clicks} קליקים · מיקום {page.position} · "
                  f"{page.sessions} סשנים")
            print(f"       {page.friction} אותות חיכוך "
                  f"({page.friction_rate:.0%} לסשן)"
                  + (f" · גלילה {page.scroll_depth:.0f}%"
                     if page.scroll_depth is not None else ""))

    rule()
    wasted = by_quadrant.get("wasted") or []
    if wasted:
        top = wasted[0]
        print(f"\n  הצעד הבא: {top.url}")
        print(f"    {top.clicks} קליקים נכנסים ו-{top.friction} אותות חיכוך. "
              "פתח את ההקלטות ב-Clarity לדף הזה לפני שמשנים תוכן.\n")
    else:
        print("\n  אין דף שגם מביא תנועה וגם מתסכל — אין כאן פעולה דחופה.\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Clarity × Search Console")
    parser.add_argument("--client", required=True, help="דומיין הלקוח מ-clients.json")
    parser.add_argument("--self-check", action="store_true",
                        help="בודק שיש נכס, טוקן וצילומים לפני שמנתחים")
    parser.add_argument("--queries", help="ייצוא שאילתות שמור, לשחזור הרצה")
    args = parser.parse_args(argv)

    secrets.load_env()
    try:
        if args.self_check:
            return self_check(args.client)
        return run(args.client, args.queries)
    except clients.ClientConfigError as exc:
        log(str(exc), "ERR")
        return 1
    except KeyboardInterrupt:
        log("הופסק", "SKIP")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
