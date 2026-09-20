#!/usr/bin/env python3
"""
On-Page Optimizer — מיפוי שאילתות לדפים וסגירת הפערים
================================================================
לוקח את טבלת השאילתות מ-Search Console ומפריד בין שלוש בעיות שנראות אותו
דבר: דף קרוב לעמוד הראשון, שני דפים שמתחרים על אותה שאילתה, ודף שמדורג על
שאילתה שהמילים שלה בכלל לא מופיעות בו.

האומדן נשען על עקומת CTR שנמדדת מהאתר עצמו, ונופל לעקומה ממוצעת רק כשאין
מספיק נתונים — והממצא אומר באיזו מהן השתמש.

שימוש:
    python onpage_optimizer.py --self-check --client example.com
    python onpage_optimizer.py --mode analyze --client example.com \\
        --queries queries.json [--crawl pages.json]
    python onpage_optimizer.py --mode plan --client example.com \\
        --url https://example.com/page --after "Pricing" \\
        --heading "How long does it take?" --text answer.txt
    python onpage_optimizer.py --mode publish --client example.com --plan plan_ab12
"""

from __future__ import annotations

import argparse
import json
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

from seo_core import clients, paths, secrets                            # noqa: E402
from seo_core.change_guard import checks, ledger, measure                # noqa: E402
from seo_core.change_guard import plan as plan_mod                       # noqa: E402
from seo_core.change_guard import risk, rollback                         # noqa: E402
from seo_core.log import banner, kv, log, rule                           # noqa: E402
from seo_core.schema import ChangeRecord, save_findings                  # noqa: E402
from seo_core.sources import gsc_wizard, queries                         # noqa: E402
from seo_core.wp import backup as wp_backup                              # noqa: E402
from seo_core.wp import content as wp_content                            # noqa: E402
from seo_core.wp import rehearsal                                        # noqa: E402
from seo_core.wp import seo_meta                                         # noqa: E402
from seo_core.wp import seo_refresh                                      # noqa: E402
from seo_core.wp.client import WordPressClient                           # noqa: E402

MAX_PER_KIND = 8
KIND_ICONS   = {
    "near_miss": "🎯", "cannibalised": "🔀", "coverage_gap": "🕳️ ", "low_ctr": "✏️ ",
}
KIND_TITLES  = {
    "near_miss":    "קרוב לעמוד הראשון",
    "cannibalised": "שני דפים על אותה שאילתה",
    "coverage_gap": "השאילתה לא נענית בדף",
    "low_ctr":      "בעיית טייטל — שייך ל-ctr-titles",
}


def client_dirs(domain: str) -> dict[str, Path]:
    base = paths.data_dir(domain)
    return {"base": base, "plans": base / "plans",
            "backups": base / "backups", "reports": base / "reports"}


def make_client(client: clients.Client) -> WordPressClient:
    return WordPressClient(
        base_url=client.cms.base_url, username=client.cms.username,
        app_password=client.secret(),
    )


# ═══════════════════════════════════════════════════════
#  בדיקת תקינות
# ═══════════════════════════════════════════════════════

def self_check(domain: str) -> int:
    banner("🔍 בדיקת תקינות")

    try:
        client = clients.load(domain)
    except clients.ClientConfigError as exc:
        log(str(exc), "ERR")
        return 1
    log(f"לקוח {domain} נטען מהרישום", "OK")
    kv("שפת התוכן", client.content_language)
    kv("מיקום המפתחות", paths.home())
    for problem in paths.warnings_for():
        log(problem, "WARN")

    for key, present in secrets.available().items():
        log(f"{key}: {'קיים' if present else 'חסר'}", "OK" if present else "WARN")

    if client.cms.type != "wordpress":
        log(f"CMS מסוג {client.cms.type} — טקסט להדבקה בלבד", "WARN")
        return 0

    log("בודק גישה ל-WordPress...", "WAIT")
    try:
        caps = make_client(client).probe()
    except clients.ClientConfigError as exc:
        log(str(exc), "ERR")
        return 1
    log(caps.summary(), "OK" if caps.can_write("page") else "WARN")

    drilled = rehearsal.require(client.data_dir)
    log(drilled.detail, "OK" if drilled else "WARN")

    rule()
    if caps.can_write("page") and drilled:
        log("הכל מוכן", "OK")
    elif caps.can_write("page"):
        log("יש הרשאת כתיבה, אבל חסרה חזרה גנרלית — פרסום ייחסם", "WARN")
    else:
        log("אפשר לנתח, אי אפשר לפרסם", "WARN")
    return 0


# ═══════════════════════════════════════════════════════
#  שלב 1 — ניתוח
# ═══════════════════════════════════════════════════════

def load_crawl(path: str | None) -> dict[str, str]:
    """טקסט גלוי לכל דף: {"url": "..."} — בלעדיו אין בדיקת כיסוי."""
    if not path:
        return {}
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return {url: str(text) for url, text in (raw.get("pages") or raw).items()}


def analyze(domain: str, queries_path: str | None, crawl_path: str | None) -> int:
    client = clients.load(domain)  # מאמת שהלקוח מוגדר לפני שקוראים משהו
    dirs = client_dirs(domain)

    rows = gsc_wizard.rows_for(client, queries_path)
    if not rows:
        log(rows.detail, "ERR")
        return 1
    if rows.data["fetched"]:
        log(rows.detail, "OK")
        for label, value in rows.data["lines"]:
            kv(label, value)

    loaded = queries.load_export(rows.data["path"])
    if not loaded:
        log(loaded.detail, "ERR")
        return 1
    log(loaded.detail, "OK")

    page_text = load_crawl(crawl_path)
    if not page_text:
        log("לא סופק טקסט הדפים — בדיקת הכיסוי מדלגת. "
            "הוסף --crawl כדי לזהות שאילתות שהדף לא עונה עליהן", "WARN")

    result = queries.analyse(loaded.data["rows"], page_text)
    curve, opportunities = result["curve"], result["opportunities"]

    if not opportunities:
        banner(f"🎯 מיפוי שאילתות — {domain}")
        log("לא נמצאו הזדמנויות מעל הסף", "OK")
        return 0

    conversion_rate = None      # ימולא מ-GA4 כשהמודול ייבנה
    findings = [queries.to_finding(o, domain, curve, conversion_rate,
                                   completeness=loaded.data["completeness"])
                for o in opportunities]
    path = save_findings(findings, dirs["reports"] / "onpage_findings.json")

    print_report(domain, opportunities, curve, path, loaded.data["window"])
    return 0


def print_report(domain, opportunities, curve, path, window) -> None:
    actionable = [o for o in opportunities if o.is_actionable]

    banner(f"🎯 מיפוי שאילתות — {domain}")
    kv("חלון", window)
    kv("הזדמנויות", len(opportunities))
    kv("ניתנות לטיפול בדף", len(actionable))
    # סכום של כל ההזדמנויות הוא לא תחזית — אף אחד לא סוגר את כולן.
    top_ten = sum(o.potential_clicks for o in actionable[:10])
    kv("פוטנציאל ב-10 המובילות", f"{top_ten:,.0f} קליקים")
    kv("פוטנציאל מצטבר", f"{sum(o.potential_clicks for o in actionable):,.0f} (הכל)")
    kv("עקומת CTR", curve.describe())
    rule()

    by_kind: dict[str, list] = {}
    for opportunity in opportunities:
        by_kind.setdefault(opportunity.kind, []).append(opportunity)

    for kind in ("cannibalised", "coverage_gap", "near_miss", "low_ctr"):
        group = by_kind.get(kind)
        if not group:
            continue
        icon = KIND_ICONS.get(kind, "•")
        count = "שאילתה אחת" if len(group) == 1 else f"{len(group)} שאילתות"
        print(f"\n  {icon} {KIND_TITLES[kind]} — {count}")
        for opportunity in group[:MAX_PER_KIND]:
            print(f"     +{opportunity.potential_clicks:>5.0f} קליקים  "
                  f"\"{opportunity.query}\"")
            print(f"           {opportunity.url}")
            print(f"           {opportunity.basis}")

    rule()
    if actionable:
        top = actionable[0]
        print("\n  הצעד הבא — ההזדמנות הגדולה ביותר:\n")
        if top.kind == "cannibalised":
            print(f"    הדף שצריך להחזיק בשאילתה: {top.detail['winner']}")
            for loser in top.detail["losers"]:
                print(f"      קישור פנימי והבדלת כותרות ב-{loser['url']} "
                      f"(מיקום {loser['position']})")
            print(f"\n    {top.detail['redirect']}.")
        else:
            print(f"    python onpage_optimizer.py --mode plan --client {domain} \\")
            print(f"        --url {top.url} \\")
            print('        --after "<כותרת קיימת>" --heading "<כותרת חדשה>" '
                  "--text <קובץ>")

    print(f"\n  📄 ממצאים מלאים: {path}\n")


# ═══════════════════════════════════════════════════════
#  שלב 2 — תוכנית
# ═══════════════════════════════════════════════════════

def build_plan(domain, url, after, heading, text_path, dry_run) -> int:
    client = clients.load(domain)
    dirs = client_dirs(domain)

    body = Path(text_path).read_text(encoding="utf-8").strip()
    if not body:
        log("קובץ הטקסט ריק", "ERR")
        return 1

    wp = make_client(client)
    log("בודק יכולות באתר...", "WAIT")
    caps = wp.probe()
    if not caps.can_write("page"):
        log(f"אין אפשרות כתיבה: {caps.summary()}", "ERR")
        log("הסעיף מוכן להדבקה ידנית:", "INFO")
        print(f"\n## {heading}\n\n{body}\n")
        return 1

    found = wp.find_by_url(url)
    if not found:
        log(found.detail, "ERR")
        return 1
    post_type = found.data["post_type"]

    loaded = wp_content.load(found.data["payload"], post_type)
    if not loaded:
        log(loaded.detail, "ERR")
        return 1
    page = loaded.data["content"]
    log(f"הדף נטען כ-{page.builder}", "OK")

    composed = wp_content.insert_section_after_heading(page, after, heading, body)
    if not composed:
        log(composed.detail, "ERR")
        log(f"כותרות בדף: {', '.join(wp_content.list_headings(page)) or '(אין)'}", "INFO")
        return 1

    # שער 1 — הוספה בלבד לא מסירה טקסט, ולכן לא מסכנת שאילתה קיימת.
    report = risk.assess("", risk.protected_queries([]))

    plan_id = f"plan_{uuid.uuid4().hex[:8]}"
    built = plan_mod.compose(
        plan_id=plan_id, client=domain, skill="onpage-optimizer", url=url,
        post_id=page.post_id, post_type=post_type, builder=page.builder,
        summary=f"הוספת סעיף {heading!r} אחרי {after!r}",
        rationale=composed.detail,
        payload=composed.data["payload"], inverse=composed.data["inverse"],
        before_text=page.plain_text,
        after_text=f"{page.plain_text}\n{heading}\n{body}",
        before_hash=page.hash, risk=report,
    )
    if not built:
        log(built.detail, "ERR")
        return 1

    change_plan = built.data["plan"]
    doc, _ = plan_mod.save(change_plan, dirs["plans"])

    banner(f"📝 תוכנית {plan_id}")
    kv("דף", url)
    kv("בונה", page.builder)
    kv("סעיף חדש", heading)
    rule()
    print(change_plan.diff)
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
    page = wp_content.load(found.data["payload"], change_plan.post_type).data["content"]

    if page.hash != change_plan.before_hash:
        record = ChangeRecord(
            change_id="n/a", plan_id=plan_id, skill="onpage-optimizer",
            client=domain, url=change_plan.url, post_id=change_plan.post_id,
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
        import requests
        baseline_html = requests.get(change_plan.url, timeout=20).text
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
        change_id=change_id, plan_id=plan_id, skill="onpage-optimizer", client=domain,
        url=change_plan.url, post_id=change_plan.post_id,
        before_hash=change_plan.before_hash, inverse=change_plan.inverse,
        backup_ref=str(backup_path), backup_verified=True, status="applied",
        applied_at=datetime.now(timezone.utc),
        checkpoints=ledger.schedule_checkpoints(datetime.now(timezone.utc)),
        # Why it was done and what the page was doing beforehand. Without
        # these two the measurement in 28 days has nothing to compare against
        # and no way to say what question it is answering.
        reason=_reason_for(change_plan),
        baseline=_baseline_for(domain, dirs, change_plan.url, _reason_for(change_plan)),
    )

    def fetch(target: str):
        import requests
        response = requests.get(target, timeout=20)
        return response.status_code, response.text

    log("מריץ בדיקות אחרי הפרסום...", "WAIT")
    report = checks.run(change_plan.url, change_plan.after_text[-200:], baseline, fetch)

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
    kv("מדידה חוזרת", "14 · 28 · 56 יום")

    # ציון ה-SEO בתוסף מחושב בדפדפן ולא בשרת, אז הוא נשאר על הגרסה הקודמת.
    plugin = seo_meta.detect_plugin(found.data["payload"])
    if plugin != "unknown":
        rule()
        log(f"הדף צריך פתיחה ושמירה ב-{plugin} כדי שהציון יחושב מחדש", "WARN")
        caveat = seo_refresh.elementor_caveat(page.builder, plugin)
        if caveat:
            log(caveat, "INFO")
        print(f"\n    {client.cms.base_url.rstrip('/')}"
              f"/wp-admin/post.php?post={change_plan.post_id}&action=edit")
        print("\n  רשימה מרוכזת של כל הדפים שממתינים:")
        print(f"    python -m seo_core.wp.seo_refresh --client {domain}")
    print()
    return 0


# ═══════════════════════════════════════════════════════
#  שלב 4 — מדידה חוזרת
# ═══════════════════════════════════════════════════════

def _reason_for(change_plan) -> dict:
    """The finding that justified the write, kept with the change."""
    finding = (change_plan.findings or [{}])[0]
    return {"type": finding.get("type"), "query": finding.get("query"),
            "instruction": (finding.get("action") or {}).get("instruction",
                                                             change_plan.summary),
            "impact_basis": finding.get("impact_basis", change_plan.rationale)}


def _baseline_for(domain: str, dirs: dict, url: str, reason: dict) -> dict:
    """The page's numbers as they stood, from the most recent export on disk.

    Read from the saved export rather than fetched again: the point is to keep
    the figures the decision was made on, not today's.
    """
    exports = sorted((dirs["base"] / "gsc").glob("queries_*.json"))
    if not exports:
        return {}
    loaded = queries.load_export(exports[-1])
    if not loaded:
        return {}
    rows = loaded.data["rows"]
    window = current_window(rows, url, reason.get("query"))
    if not window:
        return {}
    start, _, end = str(loaded.data["window"]).partition("..")
    return {**window, "site_clicks": sum(r.clicks for r in rows),
            "window": {"start": start, "end": end or start},
            "completeness": loaded.data.get("completeness")}


def current_window(rows, url: str, query: str | None) -> dict:
    """What the page, and the query that was worked on, are doing now."""
    for_page = [r for r in rows if r.url == url]
    if query:
        for_query = [r for r in for_page if r.query == query]
        if for_query:
            for_page = for_query
    if not for_page:
        return {}
    impressions = sum(r.impressions for r in for_page)
    weighted = sum(r.position * r.impressions for r in for_page)
    return {"clicks": sum(r.clicks for r in for_page), "impressions": impressions,
            "position": round(weighted / impressions, 2) if impressions else None}


def verify(domain: str) -> int:
    client = clients.load(domain)
    dirs = client_dirs(domain)
    due = ledger.due_checkpoints(dirs["base"])

    banner(f"📅 מדידות שהגיע זמנן — {domain}")
    if not due:
        log("אין מדידות שהגיע זמנן", "OK")
        return 0

    fetched = gsc_wizard.rows_for(client)
    if not fetched:
        log(f"{fetched.detail} — אי אפשר למדוד בלי נתונים עדכניים", "ERR")
        return 1
    loaded = queries.load_export(fetched.data["path"])
    if not loaded:
        log(loaded.detail, "ERR")
        return 1
    rows = loaded.data["rows"]

    # The control: the whole property over the same two windows. Without it no
    # rise can be told apart from a week when everything rose.
    site_now = sum(r.clicks for r in rows)

    for item in due:
        change, checkpoint = item["change"], item["checkpoint"]
        baseline = change.get("baseline") or {}
        current = current_window(rows, change["url"], (change.get("reason") or {}).get("query"))
        control = ({"clicks_before": baseline.get("site_clicks"), "clicks_after": site_now}
                   if baseline.get("site_clicks") else None)

        rule()
        if not baseline or not current:
            log(f"{change['change_id']}: אין בסיס השוואה שמור לשינוי הזה — "
                "נרשם כלא ניתן למדידה", "WARN")
            ledger.complete_checkpoint(change["change_id"], checkpoint["day"],
                                       "inconclusive", {"note": "no baseline"}, dirs["base"])
            continue

        assessment = measure.assess(baseline, current, control,
                                    loaded.data.get("completeness"))
        for line in measure.report_lines(change, assessment):
            print(f"  {line}")
        ledger.complete_checkpoint(
            change["change_id"], checkpoint["day"], assessment.verdict,
            {"current": current, "attributable": assessment.attributable,
             "reason": assessment.reason}, dirs["base"])
        if not assessment.conclusive:
            following = ledger.next_checkpoint(
                ledger.get(change["change_id"], dirs["base"]).data["change"])
            if following:
                log(f"נשאר פתוח — נמדוד שוב ביום {following['day']}", "INFO")

    rule()
    log(f"{len(due)} מדידות טופלו. ירידה בדירוג לא מפעילה שחזור אוטומטי", "INFO")
    print()
    return 0


# ═══════════════════════════════════════════════════════
#  Main
# ═══════════════════════════════════════════════════════

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="On-Page Optimizer")
    parser.add_argument("--client", required=True, help="דומיין הלקוח מ-clients.json")
    parser.add_argument("--mode", choices=["analyze", "plan", "publish", "verify"],
                        default="analyze", help="שלב בתהליך (ברירת מחדל: analyze)")
    parser.add_argument("--self-check", action="store_true",
                        help="בודק הגדרות וגישה לפני שנוגעים במשהו")
    parser.add_argument("--queries", help="קובץ שאילתות מ-GSC (למצב analyze)")
    parser.add_argument("--crawl", help="קובץ עם הטקסט הגלוי של כל דף")
    parser.add_argument("--url", help="כתובת הדף (למצב plan)")
    parser.add_argument("--after", help="הכותרת שאחרי הסעיף שלה ייכנס החדש")
    parser.add_argument("--heading", help="הכותרת של הסעיף החדש")
    parser.add_argument("--text", help="קובץ עם גוף הסעיף")
    parser.add_argument("--plan", help="מזהה התוכנית (למצב publish)")
    parser.add_argument("--dry-run", action="store_true",
                        help="מייצר תוכנית ו-diff בלי לכתוב כלום")
    args = parser.parse_args(argv)

    secrets.load_env()

    try:
        if args.self_check:
            return self_check(args.client)

        if args.mode == "analyze":
            # בלי --queries הנתונים נמשכים לבד מ-GSC Wizard.
            return analyze(args.client, args.queries, args.crawl)

        if args.mode == "plan":
            if not (args.url and args.after and args.heading and args.text):
                log("--mode plan דורש --url, --after, --heading ו---text", "ERR")
                return 1
            return build_plan(args.client, args.url, args.after, args.heading,
                              args.text, args.dry_run)

        if args.mode == "publish":
            if not args.plan:
                log("--mode publish דורש --plan", "ERR")
                return 1
            if args.dry_run:
                log("--dry-run לא חל על publish — השתמש ב---mode plan", "ERR")
                return 1
            return publish(args.client, args.plan)

        return verify(args.client)

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
