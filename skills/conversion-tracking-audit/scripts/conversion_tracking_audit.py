#!/usr/bin/env python3
"""
Conversion Tracking Audit — האם ההמרות בכלל נספרות
================================================================
סורק את האתר וקורא מה-HTML שהוא מגיש בפועל איך המדידה מותקנת: כמה מזהי GA4
יש, האם אותו תג נטען פעמיים, אילו דפים לא טוענים כלום, ואילו קישורי יצירת
קשר ממירים בלי לטעון דף.

המגבלה שמעצבת את הסקיל: **רוב מה שחשוב קורה בתוך GTM, וה-HTML לא רואה
לשם.** דף שטוען מכולה עשוי לירות אירוע על כל שליחת טופס, ועשוי לא לירות
כלום — ה-HTML זהה בשני המקרים. לכן מכולה מייצרת רשימת בדיקה מפורשת, לא
תעודת כשרות.

שימוש:
    python conversion_tracking_audit.py --self-check --client example.com
    python conversion_tracking_audit.py --client example.com \\
        [--queries queries.json --sessions ga4_sessions.json] [--max-pages 60]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
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
from seo_core.log import banner, count, kv, log, rule                    # noqa: E402
from seo_core.schema import save_findings                                # noqa: E402
from seo_core.sources import crawl as crawler                            # noqa: E402
from seo_core.sources import queries as gsc, tracking                    # noqa: E402

DEFAULT_PAGES = 60            # מדגם, לא סריקה מלאה — התגים חוזרים על עצמם
MAX_LISTED    = 6
FETCH_DELAY   = 0.4
SEVERITY_ICONS = {"blocker": "🛑", "high": "🔴", "medium": "🟡", "low": "⚪"}


def self_check(domain: str) -> int:
    banner("🔍 בדיקת תקינות")

    client = clients.load(domain)
    log(f"לקוח {domain} נטען מהרישום", "OK")
    kv("מיקום המפתחות", paths.home())
    kv("המרות מוגדרות", ", ".join(client.conversions) or "לא הוגדרו")
    if not client.conversions:
        log("ב-clients.json לא רשומה אף המרה — בלי זה אין מול מה להשוות "
            "את מה שנמצא באתר", "WARN")
    for problem in paths.warnings_for():
        log(problem, "WARN")

    fetch = crawler.requests_fetcher()
    reachable = crawler.check_start(client.cms.base_url, fetch)
    log(reachable.detail, "OK" if reachable else "ERR")
    rule()
    return 0 if reachable else 1


def load_sessions(path: str | None) -> dict[str, int]:
    """סשנים לכל דף נחיתה מ-GA4 — אופציונלי, ובלעדיו אין השוואה מול GSC."""
    if not path:
        return {}
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    entries = raw.get("rows") if isinstance(raw, dict) else raw
    if isinstance(entries, dict):
        return {str(url): int(value) for url, value in entries.items()}
    return {str(e["url"]): int(e.get("sessions") or 0) for e in entries}


def run(domain: str, queries_path: str | None, sessions_path: str | None,
        max_pages: int) -> int:
    client = clients.load(domain)
    dirs = paths.data_dir(domain)

    fetch = crawler.requests_fetcher()
    start = crawler.check_start(client.cms.base_url, fetch)
    if not start:
        log(start.detail, "ERR")
        return 1

    log(f"סורק מדגם של עד {max_pages} דפים...", "WAIT")
    crawled = crawler.crawl(
        start.data["root"], fetch,
        robots_text=crawler.load_robots(client.cms.base_url, fetch),
        max_pages=max_pages,
    )
    log(crawled.summary(), "OK")

    # הסריקה לא שומרת HTML, אז הדפים נקראים שוב — מדגם קטן, קריאה מנומסת.
    html_by_url: dict[str, str] = {}
    targets = [u for u, p in crawled.pages.items() if p.is_ok]
    log(f"קורא תגים מ-{len(targets)} דפים...", "WAIT")
    for index, url in enumerate(targets):
        try:
            _status, html, _final = fetch(url)
            html_by_url[url] = html
        except Exception as exc:
            log(f"{url}: {exc}", "WARN")
        if index + 1 < len(targets):
            time.sleep(FETCH_DELAY)

    rows = []
    if queries_path:
        loaded = gsc.load_export(Path(queries_path))
        if loaded:
            rows = loaded.data["rows"]
        else:
            log(loaded.detail, "WARN")

    sessions = load_sessions(sessions_path)
    if rows and not sessions:
        log("יש שאילתות אבל אין קובץ סשנים — ההשוואה בין קליקים לסשנים "
            "היא הבדיקה החזקה כאן, והיא מדלגת", "WARN")

    audit = tracking.analyse(crawled, html_by_url, rows, sessions)

    banner(f"📐 מדידת המרות — {domain}")
    kv("סריקה", audit.summary())
    kv("מזהי GA4", ", ".join(audit.measurement_ids) or "לא נמצאו")
    kv("מכולות GTM", ", ".join(audit.containers) or "אין")
    kv("המרות ב-clients.json", ", ".join(client.conversions) or "לא הוגדרו")
    rule()

    verifiable = [i for i in audit.issues if i.verifiable]
    unverifiable = [i for i in audit.issues if not i.verifiable]

    if verifiable:
        print(f"\n  ✅ מה שנקרא מהדף עצמו — "
              f"{count(len(verifiable), 'ממצא אחד', 'ממצאים')}")
        for issue in verifiable:
            icon = SEVERITY_ICONS.get(issue.severity, "•")
            print(f"     {icon} {issue.detail}")
            for url in issue.urls[:MAX_LISTED]:
                print(f"           {url}")

    if audit.gaps:
        print(f"\n  📉 קליקים בלי סשנים — "
              f"{count(len(audit.gaps), 'דף אחד', 'דפים')}")
        for gap in audit.gaps[:MAX_LISTED]:
            print(f"     {gap.url}\n           {gap.describe()}")

    if unverifiable:
        print(f"\n  ❓ מה שהדף לא יכול לענות עליו — "
              f"{count(len(unverifiable), 'ממצא אחד', 'ממצאים')}")
        for issue in unverifiable:
            print(f"     • {issue.detail}")
            for item in issue.evidence.get("checklist", []):
                print(f"           ☐ {item}")

    rule()
    if audit.trustworthy:
        log("המדידה נראית שלמה — אפשר לסמוך על המרות בדוחות של שאר הסקילים",
            "OK")
    else:
        log("המדידה לא אמינה. כל עוד זה המצב, `impact_conversions` בשאר "
            "הסקילים יטעה יותר משיעזור — עדיף לדרג על קליקים בלבד", "ERR")

    findings = tracking.to_findings(audit, domain)
    if findings:
        path = save_findings(findings, dirs / "reports" / "tracking_findings.json")
        print(f"\n  📄 ממצאים מלאים: {path}")
        print("  הם ייכנסו גם לאודיט: "
              f"python seo_audit_report.py --client {domain}")
    print()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Conversion Tracking Audit")
    parser.add_argument("--client", required=True, help="דומיין הלקוח מ-clients.json")
    parser.add_argument("--self-check", action="store_true",
                        help="בודק הגדרות וגישה לפני סריקה")
    parser.add_argument("--queries", help="קובץ שאילתות מ-GSC")
    parser.add_argument("--sessions", help="סשנים לכל דף נחיתה מ-GA4")
    parser.add_argument("--max-pages", type=int, default=DEFAULT_PAGES,
                        help="גודל המדגם לסריקה")
    args = parser.parse_args(argv)

    secrets.load_env()

    try:
        if args.self_check:
            return self_check(args.client)
        return run(args.client, args.queries, args.sessions, args.max_pages)
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
