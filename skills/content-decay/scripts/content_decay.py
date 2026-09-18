#!/usr/bin/env python3
"""
Content Decay — איתור דפים שדועכים וטיפול בטוח
================================================================
מזהה דפים שאיבדו תנועה, מסווג *למה* (ירידת מיקום, עונתיות, קניבליזציה,
יציאה מהאינדקס), ומטפל רק במה שבאמת ניתן לטיפול — דרך change_guard,
עם תוכנית לאישור, גיבוי מאומת, בדיקות אחרי, ומדידה חוזרת.

שימוש:
    # בדיקת תקינות לפני הכל
    python content_decay.py --self-check --client example.com

    # שלב 1: ניתוח — מייצר דוח וממצאים
    python content_decay.py --mode analyze --client example.com --gsc-data gsc.json

    # שלב 2: תוכנית — בלי לכתוב כלום לאתר
    python content_decay.py --mode plan --client example.com \\
        --url https://example.com/page --heading "Spring cost" --text new.txt

    # שלב 3: פרסום — רק אחרי אישור התוכנית
    python content_decay.py --mode publish --client example.com --plan plan_001

    # שלב 4: מדידה חוזרת ב-14/28/56 יום
    python content_decay.py --mode verify --client example.com
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

from seo_core import clients, paths, secrets                                  # noqa: E402
from seo_core.change_guard import checks, ledger, plan as plan_mod      # noqa: E402
from seo_core.change_guard import risk, rollback                        # noqa: E402
from seo_core.log import banner, kv, log, rule                          # noqa: E402
from seo_core.schema import ChangeRecord, save_findings                 # noqa: E402
from seo_core.sources import gsc_source                                 # noqa: E402
from seo_core.wp import backup as wp_backup                             # noqa: E402
from seo_core.wp import content as wp_content                           # noqa: E402
from seo_core.wp import seo_meta
from seo_core.wp import rehearsal                                       # noqa: E402
from seo_core.wp import seo_refresh                                     # noqa: E402
from seo_core.wp.client import WordPressClient                          # noqa: E402

# ═══════════════════════════════════════════════════════
#  הגדרות
# ═══════════════════════════════════════════════════════

MAX_PER_RUN   = 10        # כמה ממצאים להציג בדוח
CAUSE_ICONS   = {
    "position_loss": "📉", "seasonality": "🗓️ ", "serp_takeover": "⚔️ ",
    "cannibalized": "🔀", "deindexed": "🚫", "unclear": "❓",
}


def client_dirs(domain: str) -> dict[str, Path]:
    base = paths.data_dir(domain)
    return {
        "base": base,
        "plans": base / "plans",
        "backups": base / "backups",
        "reports": base / "reports",
    }


def make_client(client: clients.Client) -> WordPressClient:
    return WordPressClient(
        base_url=client.cms.base_url,
        username=client.cms.username,
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
    kv("שוק", client.market)
    kv("שפת התוכן", client.content_language)
    kv("נתוני המרה", "כן" if client.has_conversion_data else "לא — נדרג לפי קליקים")

    kv("מיקום המפתחות", paths.home())
    for problem in paths.warnings_for():
        log(problem, "WARN")

    for key, present in secrets.available().items():
        log(f"{key}: {'קיים' if present else 'חסר'}", "OK" if present else "WARN")

    if client.cms.type != "wordpress":
        log(f"CMS מסוג {client.cms.type} — הסקיל יפיק טקסט להדבקה בלבד", "WARN")
        return 0

    log("בודק גישה ל-WordPress...", "WAIT")
    try:
        caps = make_client(client).probe()
    except clients.ClientConfigError as exc:
        log(str(exc), "ERR")
        return 1

    log(caps.summary(), "OK" if caps.can_write("page") else "WARN")
    for blocker in caps.blockers:
        log(blocker, "WARN")

    drilled = rehearsal.require(client.data_dir)
    log(drilled.detail, "OK" if drilled else "WARN")

    rule()
    if caps.can_write("page") and drilled:
        log("הכל מוכן", "OK")
        return 0
    if caps.can_write("page"):
        log("יש הרשאת כתיבה, אבל חסרה חזרה גנרלית — פרסום ייחסם", "WARN")
        return 0
    log("אין הרשאת כתיבה — אפשר לנתח, אי אפשר לפרסם", "WARN")
    return 0


# ═══════════════════════════════════════════════════════
#  שלב 1 — ניתוח
# ═══════════════════════════════════════════════════════

def analyze(domain: str, data_path: Path) -> int:
    client = clients.load(domain)
    dirs = client_dirs(domain)

    loaded = gsc_source.load_export(data_path)
    if not loaded:
        log(loaded.detail, "ERR")
        return 1
    log(loaded.detail, "OK")

    current = loaded.data["current"]
    prior = loaded.data["prior"]
    site_trend = loaded.data["site_trend"]

    verdicts = []
    for url, now in current.items():
        before = prior.get(url)
        if before is None:
            continue
        verdict = gsc_source.classify(now, before, site_trend=site_trend)
        if verdict:
            verdicts.append(verdict)

    if not verdicts:
        banner(f"📊 דעיכת תוכן — {domain}")
        log("לא נמצאו דפים דועכים מעל הסף", "OK")
        return 0

    conversion_rate = None
    if client.has_conversion_data and client.conversion_value:
        conversion_rate = None      # ימולא מ-GA4 כשהמודול ייבנה

    findings = [gsc_source.to_finding(v, domain, conversion_rate) for v in verdicts]
    findings.sort(key=lambda f: f.priority(), reverse=True)
    path = save_findings(findings, dirs["reports"] / "decay_findings.json")

    print_report(domain, verdicts, site_trend, path)
    return 0


def print_report(domain, verdicts, site_trend, path) -> None:
    actionable = [v for v in verdicts if v.is_actionable]

    banner(f"📊 דעיכת תוכן — {domain}")
    kv("דפים דועכים", len(verdicts))
    kv("ניתנים לטיפול בשכתוב", len(actionable))
    kv("מגמת האתר", f"{site_trend:+.0%}")
    kv("סה\"כ קליקים שאבדו", sum(v.clicks_lost for v in verdicts))
    rule()

    by_cause: dict[str, list] = {}
    for verdict in verdicts:
        by_cause.setdefault(verdict.cause, []).append(verdict)

    for cause, group in sorted(by_cause.items(), key=lambda kv_: -len(kv_[1])):
        icon = CAUSE_ICONS.get(cause, "•")
        count = "דף אחד" if len(group) == 1 else f"{len(group)} דפים"
        print(f"\n  {icon} {cause} — {count}")
        for verdict in sorted(group, key=lambda v: -v.clicks_lost)[:MAX_PER_RUN]:
            print(f"     {verdict.clicks_lost:>5} קליקים  {verdict.url}")
            print(f"           {verdict.explanation}")

    rule()
    if actionable:
        print("\n  הצעד הבא — לדף עם הפוטנציאל הגבוה ביותר:\n")
        top = max(actionable, key=lambda v: v.clicks_lost)
        print(f"    python content_decay.py --mode plan --client {domain} \\")
        print(f'        --url {top.url} --heading "<כותרת>" --text <קובץ>')
    else:
        log("אף דעיכה לא ניתנת לטיפול בשכתוב — ראה את ההסברים למעלה", "WARN")

    print(f"\n  📄 ממצאים מלאים: {path}\n")


# ═══════════════════════════════════════════════════════
#  שלב 2 — תוכנית
# ═══════════════════════════════════════════════════════

def build_plan(domain, url, heading, text_path, dry_run) -> int:
    client = clients.load(domain)
    dirs = client_dirs(domain)

    paragraph = Path(text_path).read_text(encoding="utf-8").strip()
    if not paragraph:
        log("קובץ הטקסט ריק", "ERR")
        return 1

    wp = make_client(client)
    log("בודק יכולות באתר...", "WAIT")
    caps = wp.probe()
    if not caps.can_write("page"):
        log(f"אין אפשרות כתיבה: {caps.summary()}", "ERR")
        log("הטקסט מוכן להדבקה ידנית:", "INFO")
        print(f"\n{paragraph}\n")
        return 1

    found = wp.find_by_url(url)
    if not found:
        log(found.detail, "ERR")
        return 1
    post = found.data["payload"]
    post_type = found.data["post_type"]

    loaded = wp_content.load(post, post_type)
    if not loaded:
        log(loaded.detail, "ERR")
        return 1
    page = loaded.data["content"]
    log(f"הדף נטען כ-{page.builder}", "OK")

    composed = wp_content.insert_paragraph_after_heading(page, heading, paragraph)
    if not composed:
        log(composed.detail, "ERR")
        return 1

    # שער 1 — רק טקסט שמוסר מסכן שאילתות קיימות. הוספה בלבד לא מסכנת.
    report = risk.assess("", risk.protected_queries([]))

    plan_id = f"plan_{uuid.uuid4().hex[:8]}"
    built = plan_mod.compose(
        plan_id=plan_id, client=domain, skill="content-decay", url=url,
        post_id=page.post_id, post_type=post_type, builder=page.builder,
        summary=f"הוספת פסקה אחרי הכותרת {heading!r}",
        rationale=composed.detail,
        payload=composed.data["payload"], inverse=composed.data["inverse"],
        before_text=page.plain_text, after_text=f"{page.plain_text}\n{paragraph}",
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
    kv("סיכון", report.level)
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

    # אין כתיבה ראשונה לאתר חי לפני שמישהו הוכיח שאפשר לבטל אותה.
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

    # מניעת דריסה — hash ולא modified_gmt
    if page.hash != change_plan.before_hash:
        decision = rollback.on_concurrent_edit(
            ChangeRecord(
                change_id="n/a", plan_id=plan_id, skill="content-decay",
                client=domain, url=change_plan.url, post_id=change_plan.post_id,
                before_hash=change_plan.before_hash, inverse=change_plan.inverse,
                backup_ref=None, backup_verified=False,
            ),
            page.hash,
        )
        log(decision.reason, "ERR")
        return 1

    # גיבוי, ואימות שהוא באמת שחזורי, לפני שנוגעים
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

    baseline_html = ""
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
        change_id=change_id, plan_id=plan_id, skill="content-decay", client=domain,
        url=change_plan.url, post_id=change_plan.post_id,
        before_hash=change_plan.before_hash, inverse=change_plan.inverse,
        backup_ref=str(backup_path), backup_verified=True, status="applied",
        applied_at=datetime.now(timezone.utc),
        checkpoints=ledger.schedule_checkpoints(datetime.now(timezone.utc)),
    )

    # שער 5 — בדיקות טכניות מיד אחרי
    log("מריץ בדיקות אחרי הפרסום...", "WAIT")

    def fetch(target: str):
        import requests
        response = requests.get(target, timeout=20)
        return response.status_code, response.text

    expected = change_plan.after_text[-200:]
    report = checks.run(change_plan.url, expected, baseline, fetch)

    rule()
    for check in report.checks:
        log(f"{check.name}: {check.detail}", "OK" if check.passed else "ERR")
    rule()

    if report.should_roll_back:
        log("כשל טכני — משחזר", "ERR")
        decision = rollback.on_technical_failure(
            record, [c.name for c in report.breaking_failures]
        )
        outcome = rollback.execute(decision, record, lambda: wp_backup.restore(wp, snapshot))
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

def verify(domain: str) -> int:
    dirs = client_dirs(domain)
    due = ledger.due_checkpoints(dirs["base"])

    banner(f"📅 מדידות שהגיע זמנן — {domain}")
    if not due:
        log("אין מדידות שהגיע זמנן", "OK")
        return 0

    for item in due:
        change, checkpoint = item["change"], item["checkpoint"]
        print(f"\n  {change['url']}")
        kv("שינוי", change["change_id"])
        kv("יום", checkpoint["day"])
        kv("בוצע", change.get("applied_at", "—"))

    rule()
    log(f"{len(due)} מדידות ממתינות", "INFO")
    log("שלוף נתוני GSC עדכניים והרץ את ההשוואה — "
        "ירידה בדירוג לא מפעילה שחזור אוטומטי", "INFO")
    print()
    return 0


# ═══════════════════════════════════════════════════════
#  Main
# ═══════════════════════════════════════════════════════

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Content Decay")
    parser.add_argument("--client", required=True, help="דומיין הלקוח כפי שמופיע ב-clients.json")
    parser.add_argument("--mode", choices=["analyze", "plan", "publish", "verify"],
                        default="analyze", help="שלב בתהליך (ברירת מחדל: analyze)")
    parser.add_argument("--self-check", action="store_true",
                        help="בודק הגדרות וגישה לפני שנוגעים במשהו")
    parser.add_argument("--gsc-data", help="נתיב לקובץ נתוני GSC (למצב analyze)")
    parser.add_argument("--url", help="כתובת הדף (למצב plan)")
    parser.add_argument("--heading", help="הכותרת שאחריה תיכנס הפסקה (למצב plan)")
    parser.add_argument("--text", help="קובץ עם הפסקה להוספה (למצב plan)")
    parser.add_argument("--plan", help="מזהה התוכנית (למצב publish)")
    parser.add_argument("--dry-run", action="store_true",
                        help="מייצר תוכנית ו-diff בלי לכתוב כלום")
    args = parser.parse_args(argv)

    secrets.load_env()

    try:
        if args.self_check:
            return self_check(args.client)

        if args.mode == "analyze":
            if not args.gsc_data:
                log("--mode analyze דורש --gsc-data", "ERR")
                return 1
            return analyze(args.client, Path(args.gsc_data))

        if args.mode == "plan":
            if not (args.url and args.heading and args.text):
                log("--mode plan דורש --url, --heading ו---text", "ERR")
                return 1
            return build_plan(args.client, args.url, args.heading, args.text, args.dry_run)

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
    except KeyboardInterrupt:
        log("הופסק", "SKIP")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
