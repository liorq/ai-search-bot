#!/usr/bin/env python3
"""
Click Depth — מבנה האתר: מה נשבר, מה מפנה, ומה רחוק מדי
================================================================
סורק את האתר מדף הבית ומפריד בין שלוש עובדות לסימן אחד: קישורים פנימיים
ל-404, קישורים ליעד שמפנה, דפים חסומים שמקבלים הופעות — ודפים עמוקים
שיש להם ביקוש.

עומק לבדו הוא לא ממצא. sitemap דואג לגילוי, ודף במרחק חמישה קליקים שמדורג
טוב הוא לא בעיה. לכן דף עמוק עולה לדוח רק כשיש לו גם ביקוש, ותמיד מתחת
לעובדות.

שימוש:
    python click_depth.py --self-check --client example.com
    python click_depth.py --mode analyze --client example.com \\
        --queries queries.json [--max-pages 400] [--trace]
    python click_depth.py --mode plan --client example.com \\
        --page https://example.com/blog/post \\
        --old https://example.com/old --new https://example.com/new
    python click_depth.py --mode publish --client example.com --plan plan_ab12
"""

from __future__ import annotations

import argparse
import sys
import uuid
from datetime import datetime, timezone
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
from seo_core.change_guard import checks, ledger, plan as plan_mod       # noqa: E402
from seo_core.change_guard import risk, rollback                         # noqa: E402
from seo_core.log import banner, kv, log, rule                           # noqa: E402
from seo_core.schema import ChangeRecord, save_findings                  # noqa: E402
from seo_core.sources import crawl as crawler                            # noqa: E402
from seo_core.sources import queries as gsc, structure                   # noqa: E402
from seo_core.wp import backup as wp_backup                              # noqa: E402
from seo_core.wp import content as wp_content                            # noqa: E402
from seo_core.wp import rehearsal                                        # noqa: E402
from seo_core.wp.client import WordPressClient                           # noqa: E402

MAX_LISTED    = 10
FETCH_TIMEOUT = 20


def client_dirs(domain: str) -> dict[str, Path]:
    base = paths.data_dir(domain)
    return {"base": base, "plans": base / "plans",
            "backups": base / "backups", "reports": base / "reports"}


def make_client(client: clients.Client) -> WordPressClient:
    return WordPressClient(
        base_url=client.cms.base_url, username=client.cms.username,
        app_password=client.secret(),
    )


def fetch_html(url: str) -> str:
    import requests
    response = requests.get(url, timeout=FETCH_TIMEOUT)
    response.raise_for_status()
    return response.text


# ═══════════════════════════════════════════════════════
#  בדיקת תקינות
# ═══════════════════════════════════════════════════════

def self_check(domain: str) -> int:
    banner("🔍 בדיקת תקינות")

    client = clients.load(domain)
    log(f"לקוח {domain} נטען מהרישום", "OK")
    kv("מיקום המפתחות", paths.home())
    for problem in paths.warnings_for():
        log(problem, "WARN")

    fetch = crawler.requests_fetcher()
    reachable = crawler.check_start(client.cms.base_url, fetch)
    log(reachable.detail, "OK" if reachable else "ERR")
    if not reachable:
        return 1

    robots_text = crawler.load_robots(client.cms.base_url, fetch)
    if robots_text:
        robots = crawler.parse_robots(robots_text)
        log(f"robots.txt נקרא — {len(robots.disallow)} כללי Disallow", "OK")
    else:
        log("אין robots.txt קריא — נסרוק בקצב ברירת המחדל", "WARN")

    if client.cms.type == "wordpress":
        caps = make_client(client).probe()
        log(caps.summary(), "OK" if caps.can_write("page") else "WARN")
        drilled = rehearsal.require(client.data_dir)
        log(drilled.detail, "OK" if drilled else "WARN")

    rule()
    log("אפשר לסרוק", "OK")
    return 0


# ═══════════════════════════════════════════════════════
#  שלב 1 — סריקה וניתוח
# ═══════════════════════════════════════════════════════

def analyze(domain: str, queries_path: Path | None, max_pages: int,
            do_trace: bool) -> int:
    client = clients.load(domain)
    dirs = client_dirs(domain)

    rows = []
    if queries_path:
        loaded = gsc.load_export(queries_path)
        if not loaded:
            log(loaded.detail, "ERR")
            return 1
        rows = loaded.data["rows"]
        log(loaded.detail, "OK")
    else:
        log("בלי --queries: העובדות ידווחו, אבל דפים עמוקים ודפים חסומים "
            "דורשים נתוני ביקוש ולא ייבדקו", "WARN")

    fetch = crawler.requests_fetcher()
    start = crawler.check_start(client.cms.base_url, fetch)
    if not start:
        log(start.detail, "ERR")
        return 1

    log(f"סורק עד {max_pages} דפים...", "WAIT")
    crawled = crawler.crawl(
        start.data["root"], fetch,
        robots_text=crawler.load_robots(client.cms.base_url, fetch),
        max_pages=max_pages,
    )
    log(crawled.summary(), "OK")
    if crawled.stopped_at_cap:
        log(f"הסריקה נעצרה בתקרה של {max_pages} דפים — העומק המרבי שמדווח "
            f"הוא של מה שנסרק בלבד", "WARN")

    traces: dict[str, crawler.Trace] = {}
    if do_trace:
        redirecting = [p.url for p in crawled.pages.values() if p.redirected]
        if redirecting:
            log(f"עוקב אחרי {len(redirecting)} הפניות קפיצה-קפיצה...", "WAIT")
            single = crawler.requests_fetcher(follow=False)
            traces = {url: crawler.trace(url, single) for url in redirecting}

    curve = gsc.build_curve(rows)
    report = structure.analyse(crawled, rows, curve, traces)

    banner(f"🧭 מבנה האתר — {domain}")
    kv("סריקה", report.summary())
    kv("קישורים שלא מגיעים ליעד", report.wasted_links)
    if rows:
        kv("עקומת CTR", curve.describe())
    rule()

    if report.broken:
        print(f"\n  💥 יעדים שבורים — {len(report.broken)}")
        for broken in report.broken[:MAX_LISTED]:
            print(f"     {broken.url}")
            print(f"           {broken.describe()}")
            for source in broken.linked_from[:3]:
                print(f"           ← {source}")

    chains = [r for r in report.redirected if r.is_chain]
    singles = [r for r in report.redirected if not r.is_chain]
    if chains:
        print(f"\n  🔁 שרשראות הפניה — {len(chains)}")
        for redirect in chains[:MAX_LISTED]:
            print(f"     {redirect.url}\n           {redirect.describe()}")
    if singles:
        print(f"\n  ↪️  קישורים ליעד שמפנה — {len(singles)}")
        for redirect in singles[:MAX_LISTED]:
            print(f"     {redirect.url} → {redirect.trace.destination}")
    if report.redirected and not do_trace:
        print("     (--trace סופר את אורך השרשרת; בלעדיו נראית רק הקפיצה הראשונה)")

    if report.blocked:
        print(f"\n  🚫 חסום ומקבל הופעות — {len(report.blocked)}")
        for blocked in report.blocked[:MAX_LISTED]:
            print(f"     {blocked.url}\n           {blocked.describe()}")
        print("     בדוק אם החסימה מכוונת לפני שנוגעים בה")

    if report.deep:
        print(f"\n  🪜 עמוק ומבוקש — {len(report.deep)}")
        for deep in report.deep[:MAX_LISTED]:
            print(f"     +{deep.potential_clicks:>5.0f} קליקים  {deep.url}")
            print(f"           {deep.describe()}")
            if deep.path_hint:
                print(f"           הדף הרדוד הקרוב ביותר: {deep.path_hint}")

    findings = structure.to_findings(report, domain, curve)
    rule()
    if findings:
        path = save_findings(findings, dirs["reports"] / "structure_findings.json")
        print(f"\n  📄 ממצאים מלאים: {path}")
    else:
        log("לא נמצאה אף בעיית מבנה מעל הסף", "OK")

    if chains:
        top = chains[0]
        print("\n  הצעד הבא — לקצר את השרשרת הארוכה ביותר:\n")
        print(f"    python click_depth.py --mode plan --client {domain} \\")
        print(f"        --page {top.linked_from[0]} \\")
        print(f"        --old {top.url} --new {top.trace.destination}")
    print()
    return 0


# ═══════════════════════════════════════════════════════
#  שלב 2 — תוכנית
# ═══════════════════════════════════════════════════════

def build_plan(domain, page_url, old_url, new_url, dry_run) -> int:
    client = clients.load(domain)
    dirs = client_dirs(domain)

    wp = make_client(client)
    log("בודק יכולות באתר...", "WAIT")
    caps = wp.probe()
    if not caps.can_write("page"):
        log(f"אין אפשרות כתיבה: {caps.summary()}", "ERR")
        log(f"עדכן ידנית ב-{page_url}: {old_url} → {new_url}", "INFO")
        return 1

    found = wp.find_by_url(page_url)
    if not found:
        log(found.detail, "ERR")
        return 1
    post, post_type = found.data["payload"], found.data["post_type"]

    loaded = wp_content.load(post, post_type)
    if not loaded:
        log(loaded.detail, "ERR")
        return 1
    page = loaded.data["content"]
    log(f"הדף נטען כ-{page.builder}", "OK")

    # היעד החדש נבדק לפני שמפנים אליו — אין טעם להחליף 301 ב-404.
    try:
        import requests
        response = requests.get(new_url, timeout=FETCH_TIMEOUT, allow_redirects=False)
    except Exception as exc:
        log(f"לא הצלחתי לבדוק את היעד החדש: {exc}", "ERR")
        return 1
    if response.status_code != 200:
        log(f"היעד החדש מחזיר {response.status_code} — לא מפנים אליו קישורים",
            "ERR")
        return 1
    log("היעד החדש מחזיר 200", "OK")

    composed = wp_content.retarget_link(page, old_url, new_url)
    if not composed:
        log(composed.detail, "ERR")
        return 1

    # שער 1 — החלפת יעד לא נוגעת בטקסט, אז אין מילה שיורדת מהדף.
    report = risk.assess("", risk.protected_queries([]))

    plan_id = f"plan_{uuid.uuid4().hex[:8]}"
    built = plan_mod.compose(
        plan_id=plan_id, client=domain, skill="click-depth", url=page_url,
        post_id=page.post_id, post_type=post_type, builder=page.builder,
        summary=f"הפניית {composed.data['links_changed']} קישורים ליעד הסופי",
        rationale=composed.detail,
        payload=composed.data["payload"], inverse=composed.data["inverse"],
        before_text=f"{page.plain_text}\n[→ {old_url}]",
        after_text=f"{page.plain_text}\n[→ {new_url}]",
        before_hash=page.hash, risk=report,
        context={"target": new_url, "replaced": old_url,
                 "links_changed": composed.data["links_changed"]},
    )
    if not built:
        log(built.detail, "ERR")
        return 1

    change_plan = built.data["plan"]
    doc, _ = plan_mod.save(change_plan, dirs["plans"])

    banner(f"📝 תוכנית {plan_id}")
    kv("דף", page_url)
    kv("מ-", old_url)
    kv("אל", new_url)
    kv("קישורים", composed.data["links_changed"])
    rule()

    if dry_run:
        log("dry-run — לא נשמר אישור ולא נכתב כלום", "SKIP")
    print(f"\n  📄 {doc}")
    print(f"\n  לאישור:\n    python -m seo_core.change_guard.plan approve {plan_id} "
          f"--dir {dirs['plans']}\n")
    return 0


# ═══════════════════════════════════════════════════════
#  שלב 3 — פרסום
# ═══════════════════════════════════════════════════════

def publish(domain: str, plan_id: str) -> int:
    client = clients.load(domain)
    dirs = client_dirs(domain)

    loaded = plan_mod.load(plan_id, dirs["plans"])
    if not loaded:
        log(loaded.detail, "ERR")
        return 1
    change_plan = loaded.data["plan"]

    approved = plan_mod.is_approved(change_plan, dirs["plans"])
    if not approved:
        log(approved.detail, "ERR")
        return 1
    log("התוכנית מאושרת", "OK")

    drilled = rehearsal.require(dirs["base"])
    if not drilled:
        log(drilled.detail, "ERR")
        return 1
    log(drilled.detail, "OK")

    wp = make_client(client)
    wp.probe()

    found = wp.find_by_url(change_plan.url)
    if not found:
        log(found.detail, "ERR")
        return 1
    page = wp_content.load(found.data["payload"],
                           change_plan.post_type).data["content"]

    if page.hash != change_plan.before_hash:
        record = ChangeRecord(
            change_id="n/a", plan_id=plan_id, skill="click-depth", client=domain,
            url=change_plan.url, post_id=change_plan.post_id,
            before_hash=change_plan.before_hash, inverse=change_plan.inverse,
            backup_ref=None, backup_verified=False,
        )
        log(rollback.on_concurrent_edit(record, page.hash).reason, "ERR")
        return 1

    change_id = f"chg_{uuid.uuid4().hex[:8]}"
    snapshot = wp_backup.create(
        page, change_plan.url, change_id,
        meta_keys=list(change_plan.payload.get("meta", {})),
    )
    verified = wp_backup.verify(snapshot)
    if not verified:
        log(verified.detail, "ERR")
        return 1
    backup_path = wp_backup.save(snapshot, dirs["backups"])
    log(f"גיבוי אומת ונשמר: {backup_path.name}", "OK")

    try:
        baseline_html = fetch_html(change_plan.url)
    except Exception as exc:
        log(f"לא הצלחתי לצלם את הדף לפני הכתיבה: {exc}", "ERR")
        return 1
    baseline = checks.Baseline.capture(baseline_html)

    log("כותב לאתר...", "WAIT")
    written = wp.update_post(
        change_plan.post_id, post_type=change_plan.post_type,
        content=change_plan.payload.get("content"),
        meta=change_plan.payload.get("meta"),
    )
    if not written:
        log(f"הכתיבה נכשלה: {written.detail}", "ERR")
        return 1
    log("נכתב", "OK")

    record = ChangeRecord(
        change_id=change_id, plan_id=plan_id, skill="click-depth", client=domain,
        url=change_plan.url, post_id=change_plan.post_id,
        before_hash=change_plan.before_hash, inverse=change_plan.inverse,
        backup_ref=str(backup_path), backup_verified=True, status="applied",
        applied_at=datetime.now(timezone.utc),
        checkpoints=ledger.schedule_checkpoints(datetime.now(timezone.utc)),
    )

    def fetch(target: str):
        import requests
        response = requests.get(target, timeout=FETCH_TIMEOUT)
        return response.status_code, response.text

    # היעד החדש נבדק שוב אחרי הכתיבה: הפניית קישורים ל-404 גרועה מהשרשרת.
    target = change_plan.context.get("target", "")
    log("מריץ בדיקות אחרי הפרסום...", "WAIT")
    report = checks.run(
        change_plan.url, checks.visible_text(baseline_html)[:120], baseline, fetch,
        internal_links=[target] if target.startswith("http") else None,
    )

    rule()
    for check in report.checks:
        log(f"{check.name}: {check.detail}", "OK" if check.passed else "ERR")
    rule()

    if report.should_roll_back:
        log("כשל טכני — משחזר", "ERR")
        decision = rollback.on_technical_failure(
            record, [c.name for c in report.breaking_failures])
        outcome = rollback.execute(decision, record,
                                   lambda: wp_backup.restore(wp, snapshot))
        log(outcome.detail, "OK" if outcome else "ERR")
        record.status = "rolled_back" if outcome else "applied"
        record.notes.append(outcome.detail)
        ledger.record(record, dirs["base"])
        return 1

    ledger.record(record, dirs["base"])
    banner("✅ פורסם")
    kv("שינוי", change_id)
    kv("קישורים שהופנו", change_plan.context.get("links_changed", "?"))
    # החלפת יעד קישור לא נוגעת בטקסט הנראה, ולכן גם לא בציון של תוסף ה-SEO.
    print()
    return 0


# ═══════════════════════════════════════════════════════
#  Main
# ═══════════════════════════════════════════════════════

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Click Depth")
    parser.add_argument("--client", required=True, help="דומיין הלקוח מ-clients.json")
    parser.add_argument("--mode", choices=["analyze", "plan", "publish"],
                        default="analyze", help="שלב בתהליך (ברירת מחדל: analyze)")
    parser.add_argument("--self-check", action="store_true",
                        help="בודק הגדרות, robots.txt וגישה לפני סריקה")
    parser.add_argument("--queries", help="קובץ שאילתות מ-GSC")
    parser.add_argument("--max-pages", type=int, default=crawler.DEFAULT_MAX_PAGES,
                        help="תקרת דפים לסריקה")
    parser.add_argument("--trace", action="store_true",
                        help="עוקב אחרי כל הפניה קפיצה-קפיצה כדי לספור שרשראות")
    parser.add_argument("--page", help="הדף שבו מתקנים קישור (למצב plan)")
    parser.add_argument("--old", help="הכתובת הישנה שהקישור מצביע אליה")
    parser.add_argument("--new", help="היעד הסופי")
    parser.add_argument("--plan", help="מזהה התוכנית (למצב publish)")
    parser.add_argument("--dry-run", action="store_true",
                        help="מייצר תוכנית בלי לכתוב כלום")
    args = parser.parse_args(argv)

    secrets.load_env()

    try:
        if args.self_check:
            return self_check(args.client)

        if args.mode == "analyze":
            return analyze(args.client,
                           Path(args.queries) if args.queries else None,
                           args.max_pages, args.trace)

        if args.mode == "plan":
            if not (args.page and args.old and args.new):
                log("--mode plan דורש --page, --old ו---new", "ERR")
                return 1
            return build_plan(args.client, args.page, args.old, args.new,
                              args.dry_run)

        if not args.plan:
            log("--mode publish דורש --plan", "ERR")
            return 1
        if args.dry_run:
            log("--dry-run לא חל על publish — השתמש ב---mode plan", "ERR")
            return 1
        return publish(args.client, args.plan)

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
