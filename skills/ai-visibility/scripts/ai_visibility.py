#!/usr/bin/env python3
"""
ai-visibility — מי מורשה להיכנס, ומה יש שם לצטט.
=================================================

שתי החלטות שונות שנוטות להתערבב: להיות מאומן עליך, ולהיות מצוטט על ידיך.
הן נוסעות תחת שמות סוכן שונים, והסקיל שומר אותן נפרדות במקום להמליץ תשובה
גורפת. מה נכון לעסק — זו החלטה של בעל האתר.
"""

from __future__ import annotations

import argparse
import sys
import time
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
from seo_core.sources import ai_surface                                  # noqa: E402
from seo_core.sources import crawl as crawler                            # noqa: E402

FETCH_DELAY = 1.0
MAX_LISTED = 6


def sample_pages(client, fetch, limit: int) -> dict[str, str]:
    """מדגם קטן של דפים, בקריאה מנומסת — לא סריקה של האתר כולו."""
    start = crawler.check_start(client.cms.base_url, fetch)
    if not start:
        return {}
    crawled = crawler.crawl(
        start.data["root"], fetch,
        robots_text=crawler.load_robots(client.cms.base_url, fetch),
        max_pages=limit)
    pages: dict[str, str] = {}
    targets = [u for u, p in crawled.pages.items() if p.is_ok][:limit]
    for index, url in enumerate(targets):
        try:
            _status, html, _final = fetch(url)
            pages[url] = html
        except Exception as exc:
            log(f"{url}: {exc}", "WARN")
        if index + 1 < len(targets):
            time.sleep(FETCH_DELAY)
    return pages


def run(domain: str, max_pages: int) -> int:
    client = clients.load(domain)
    fetch = crawler.requests_fetcher()

    log(f"קורא robots.txt, llms.txt ומדגם של עד {max_pages} דפים...", "WAIT")
    pages = sample_pages(client, fetch, max_pages)
    checked = ai_surface.inspect(client.cms.base_url, fetch, pages)
    if not checked:
        log(checked.detail, "ERR")
        return 1
    surface = checked.data["surface"]

    banner(f"🤖 נראות במנועי תשובות — {domain}")
    kv("robots.txt", "נקרא" if surface.robots_found else "לא נמצא — הכול פתוח כברירת מחדל")
    kv("llms.txt", f"קיים ({surface.llms_txt_bytes} בתים)" if surface.llms_txt else "אין")
    kv("דפים שנבדקו", len(surface.pages))
    rule()

    # ── מי מורשה ──────────────────────────────────────
    print("\n  מי מורשה להיכנס")
    for rule_ in surface.agents:
        mark = "✅" if rule_.allowed else "⛔"
        print(f"     {mark} {rule_.agent:22} {rule_.purpose}")

    blocked_citers = surface.blocked_citers
    if blocked_citers:
        print(f"\n  ⚠️  חסומים סוכנים שתפקידם להחזיר תנועה: {', '.join(blocked_citers)}")
        print("     חסימה שלהם מונעת ציטוט והפניות, ולא מונעת אימון —")
        print("     האימון עובר תחת שמות אחרים.")
    if surface.blocked_trainers:
        print(f"\n  ℹ️  חסומים סוכני אימון: {', '.join(surface.blocked_trainers)}")
        print("     זו החלטה לגיטימית של בעל האתר, ואין לה מחיר בתנועה.")
    if surface.agents and not blocked_citers and not surface.blocked_trainers:
        print("\n  ℹ️  הכול פתוח — כולל אימון. אם זה לא מה שרצית, זו נקודת ההחלטה.")

    # ── מה יש לצטט ────────────────────────────────────
    quotable = [p for p in surface.pages if p.quotable]
    with_questions = [p for p in surface.pages if p.question_headings]
    print(f"\n  מה יש לצטט — {len(quotable)} מתוך {len(surface.pages)} דפים במדגם")
    if not surface.pages:
        log("לא נסרק אף דף — אין מה לומר על מבנה התוכן", "WARN")
    for page in sorted(surface.pages, key=lambda p: -p.answered_directly)[:MAX_LISTED]:
        marks = []
        if page.schema_types:
            marks.append("סכמה: " + ", ".join(sorted(set(page.schema_types))[:3]))
        else:
            marks.append("בלי סכמה")
        if page.has_faq:
            marks.append("FAQPage")
        print(f"\n     {page.url}")
        print(f"       {page.question_headings} כותרות שאלה, "
              f"{page.answered_directly} מהן נענות ישירות · {' · '.join(marks)}")

    rule()
    print("\n  הצעדים שמשנים משהו:\n")
    if blocked_citers:
        print(f"    1. להחליט על {', '.join(blocked_citers)} — כרגע חסומים, "
              "וזה מונע ציטוט")
    if not surface.llms_txt:
        print("    2. llms.txt בשורש: מה העסק, למי, ואילו דפים מסבירים מה")
    if with_questions and len(quotable) < len(with_questions):
        print("    3. אחרי כותרת שאלה — פסקת תשובה ישירה של 40–60 מילים, "
              "ומתחתיה ההרחבה")
    if any(not p.schema_types for p in surface.pages):
        print("    4. סכמת FAQPage או Article לדפים שעונים על שאלות")
    print("\n  את העריכות עצמן מבצע onpage-optimizer, עם גיבוי ואישור.\n")
    return 0


def self_check(domain: str) -> int:
    banner(f"🔍 בדיקת מוכנות — {domain}")
    client = clients.load(domain)
    kv("אתר", client.cms.base_url or "לא מוגדר")
    reachable = bool(client.cms.base_url)
    log("מוכן — הסקיל קורא בלבד ולא צורך מפתחות" if reachable
        else "חסר base_url ב-clients.json", "OK" if reachable else "ERR")
    return 0 if reachable else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AI answer-engine visibility")
    parser.add_argument("--client", required=True, help="דומיין הלקוח מ-clients.json")
    parser.add_argument("--self-check", action="store_true", help="בדיקת הגדרות")
    parser.add_argument("--max-pages", type=int, default=15, help="גודל המדגם")
    args = parser.parse_args(argv)

    secrets.load_env()
    try:
        if args.self_check:
            return self_check(args.client)
        return run(args.client, args.max_pages)
    except clients.ClientConfigError as exc:
        log(str(exc), "ERR")
        return 1
    except KeyboardInterrupt:
        log("הופסק", "SKIP")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
