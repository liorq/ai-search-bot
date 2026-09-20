#!/usr/bin/env python3
"""
Internal Anchors — קישורים פנימיים, אנקורים, ודפים שהאתר שוכח
================================================================
סורק את האתר, בונה את גרף הקישורים הפנימיים, ומפריד בין קישור שתפריט מייצר
לקישור שמישהו בחר לכתוב. מצליב מול Search Console כדי למצוא דפים עם ביקוש
אמיתי שהאתר עצמו לא תומך בהם.

מציע קישור שכמעט נכתב כבר: משפט קיים בדף אחר שמכיל בדיוק את הביטוי שדף
היעד מדורג עליו. הביטוי נעטף בקישור — שום טקסט לא מתווסף ושום דבר לא משוכתב.

שימוש:
    python internal_anchors.py --self-check --client example.com
    python internal_anchors.py --mode analyze --client example.com \\
        --queries queries.json [--known sitemap_urls.json] [--max-pages 400]
    python internal_anchors.py --mode plan --client example.com \\
        --from https://example.com/blog/post --to https://example.com/springs \\
        --phrase "torsion spring"
    python internal_anchors.py --mode publish --client example.com --plan plan_ab12
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

from seo_core import clients, paths, secrets                             # noqa: E402
from seo_core.change_guard import checks, ledger, plan as plan_mod       # noqa: E402
from seo_core.change_guard import risk, rollback                         # noqa: E402
from seo_core.log import banner, kv, log, rule                           # noqa: E402
from seo_core.schema import ChangeRecord, save_findings                  # noqa: E402
from seo_core.sources import crawl as crawler                            # noqa: E402
from seo_core.sources import gsc_wizard, linkgraph, queries as gsc       # noqa: E402
from seo_core.wp import backup as wp_backup                              # noqa: E402
from seo_core.wp import content as wp_content                            # noqa: E402
from seo_core.wp import rehearsal, seo_meta, seo_refresh                 # noqa: E402
from seo_core.wp.client import WordPressClient                           # noqa: E402

MAX_LISTED     = 10
MAX_PROPOSALS  = 4
CRAWL_FILE     = "crawl_cache.json"
FETCH_TIMEOUT  = 20


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
    robots = crawler.parse_robots(robots_text) if robots_text else crawler.Robots()
    if robots_text:
        log(f"robots.txt נקרא — {len(robots.disallow)} כללי Disallow", "OK")
        if robots.crawl_delay:
            log(f"האתר מבקש השהיה של {robots.crawl_delay} שניות — נכבד אותה", "INFO")
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

def load_known(path: str | None) -> list[str]:
    """כתובות שהאתר מכיר — מ-sitemap או מ-GSC. בלעדיהן אין איך לזהות יתומים."""
    if not path:
        return []
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        raw = raw.get("urls") or raw.get("rows") or []
    return [r if isinstance(r, str) else str(r.get("url", "")) for r in raw]


def analyze(domain: str, queries_path: str | None, known_path: str | None,
            max_pages: int) -> int:
    client = clients.load(domain)
    dirs = client_dirs(domain)

    fetched = gsc_wizard.rows_for(client, queries_path)
    if not fetched:
        log(fetched.detail, "ERR")
        return 1
    if fetched.data["fetched"]:
        log(fetched.detail, "OK")
        for label, value in fetched.data["lines"]:
            kv(label, value)

    loaded = gsc.load_export(fetched.data["path"])
    if not loaded:
        log(loaded.detail, "ERR")
        return 1
    rows = loaded.data["rows"]
    log(loaded.detail, "OK")

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
        log(f"הסריקה נעצרה בתקרה של {max_pages} דפים — הגרף חלקי, "
            f"ודפים שלא נסרקו לא ייחשבו יתומים", "WARN")
    if crawled.blocked:
        log(f"{len(crawled.blocked)} כתובות נחסמו ב-robots.txt ולא נסרקו", "INFO")

    graph = linkgraph.build(crawled)
    curve = gsc.build_curve(rows)

    known = load_known(known_path) or sorted({r.url for r in rows})
    orphans = graph.orphans(known)
    gaps = linkgraph.find_support_gaps(graph, rows, curve)
    page_terms = {
        crawler.normalise(url): {t for r in group for t in r.terms()}
        for url, group in _by_url(rows).items()
    }
    problems = linkgraph.anchor_problems(graph, page_terms)

    banner(f"🔗 קישורים פנימיים — {domain}")
    kv("חלון", loaded.data["window"])
    kv("גרף", graph.summary())
    kv("עקומת CTR", curve.describe())
    kv("דפים בלי קישור עריכתי", len(graph.unsupported()))
    kv("יתומים", len(orphans))
    rule()

    if orphans:
        source = "sitemap" if known_path else "Search Console"
        print(f"\n  🕳️  דפים שהאתר מכיר ולא מקשר אליהם ({source}) — {len(orphans)}")
        for url in orphans[:MAX_LISTED]:
            print(f"     {url}")
        print("     סריקה לבדה לא יכולה למצוא דף יתום — היא לא מגיעה אליו")

    if gaps:
        print(f"\n  📉 ביקוש בלי תמיכה — {len(gaps)} דפים")
        for gap in gaps[:MAX_LISTED]:
            phrases = linkgraph.phrases_for(rows, gap.url)
            proposals = linkgraph.propose(crawled, graph, gap.url, phrases,
                                          limit=MAX_PROPOSALS)
            print(f"     +{gap.potential_clicks:>5.0f} קליקים  {gap.url}")
            print(f"           {gap.basis}")
            for proposal in proposals:
                print(f"           ← {proposal.source}")
                print(f"             כבר כתוב שם: “{proposal.phrase}”")
            if not proposals:
                print("           אין דף שכבר מזכיר את הביטוי — "
                      "הקישור יצטרך משפט חדש")

    if problems:
        print(f"\n  🏷️  אנקורים — {len(problems)} בעיות")
        for problem in problems[:MAX_LISTED]:
            print(f"     {problem.url}\n           {problem.detail}")

    findings = []
    for gap in gaps:
        phrases = linkgraph.phrases_for(rows, gap.url)
        proposals = linkgraph.propose(crawled, graph, gap.url, phrases,
                                      limit=MAX_PROPOSALS)
        findings.append(linkgraph.gap_to_finding(gap, domain, curve, proposals))

    rule()
    if findings:
        path = save_findings(findings, dirs["reports"] / "internal_link_findings.json")
        print(f"\n  📄 ממצאים מלאים: {path}")

    if gaps:
        top = gaps[0]
        phrases = linkgraph.phrases_for(rows, top.url)
        proposals = linkgraph.propose(crawled, graph, top.url, phrases, limit=1)
        if proposals:
            print("\n  הצעד הבא:\n")
            print(f"    python internal_anchors.py --mode plan --client {domain} \\")
            print(f"        --from {proposals[0].source} \\")
            print(f"        --to {top.url} --phrase \"{proposals[0].phrase}\"")
    print()
    return 0


def _by_url(rows):
    grouped: dict[str, list] = {}
    for row in rows:
        grouped.setdefault(row.url, []).append(row)
    return grouped


# ═══════════════════════════════════════════════════════
#  שלב 2 — תוכנית
# ═══════════════════════════════════════════════════════

def build_plan(domain, source_url, target_url, phrase, dry_run) -> int:
    client = clients.load(domain)
    dirs = client_dirs(domain)

    wp = make_client(client)
    log("בודק יכולות באתר...", "WAIT")
    caps = wp.probe()
    if not caps.can_write("page"):
        log(f"אין אפשרות כתיבה: {caps.summary()}", "ERR")
        log(f'הוסף ידנית: עטוף את "{phrase}" ב-{source_url} '
            f"בקישור ל-{target_url}", "INFO")
        return 1

    found = wp.find_by_url(source_url)
    if not found:
        log(found.detail, "ERR")
        return 1
    post, post_type = found.data["payload"], found.data["post_type"]

    loaded = wp_content.load(post, post_type)
    if not loaded:
        log(loaded.detail, "ERR")
        return 1
    page = loaded.data["content"]
    log(f"דף המקור נטען כ-{page.builder}", "OK")

    composed = wp_content.link_phrase(page, phrase, target_url)
    if not composed:
        log(composed.detail, "ERR")
        if composed.code == "phrase_not_found":
            log("הביטוי נמצא בסריקה אבל לא בגוף הדף — כנראה בתפריט, "
                "בפוטר, או בתבנית", "INFO")
        return 1

    # שער 1 — עטיפה בקישור לא מסירה מילה מהדף, אז אין שאילתה בסיכון.
    report = risk.assess("", risk.protected_queries([]))

    plan_id = f"plan_{uuid.uuid4().hex[:8]}"
    built = plan_mod.compose(
        plan_id=plan_id, client=domain, skill="internal-anchors", url=source_url,
        post_id=page.post_id, post_type=post_type, builder=page.builder,
        summary=f"קישור {phrase!r} מ-{source_url} ל-{target_url}",
        rationale=composed.detail,
        payload=composed.data["payload"], inverse=composed.data["inverse"],
        before_text=page.plain_text,
        after_text=f"{page.plain_text}\n[{phrase} → {target_url}]",
        before_hash=page.hash, risk=report,
        context={"target": target_url, "phrase": phrase},
    )
    if not built:
        log(built.detail, "ERR")
        return 1

    change_plan = built.data["plan"]
    doc, _ = plan_mod.save(change_plan, dirs["plans"])

    banner(f"📝 תוכנית {plan_id}")
    kv("דף מקור", source_url)
    kv("דף יעד", target_url)
    kv("אנקור", phrase)
    kv("בונה", page.builder)
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
    post = found.data["payload"]
    page = wp_content.load(post, change_plan.post_type).data["content"]

    if page.hash != change_plan.before_hash:
        record = ChangeRecord(
            change_id="n/a", plan_id=plan_id, skill="internal-anchors",
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
        change_id=change_id, plan_id=plan_id, skill="internal-anchors",
        client=domain, url=change_plan.url, post_id=change_plan.post_id,
        before_hash=change_plan.before_hash, inverse=change_plan.inverse,
        backup_ref=str(backup_path), backup_verified=True, status="applied",
        applied_at=datetime.now(timezone.utc),
        checkpoints=ledger.schedule_checkpoints(datetime.now(timezone.utc)),
    )

    def fetch(target: str):
        import requests
        response = requests.get(target, timeout=FETCH_TIMEOUT)
        return response.status_code, response.text

    # יעד הקישור נבדק גם הוא: קישור פנימי ל-404 גרוע מאין קישור.
    target = change_plan.context.get("target", "")
    log("מריץ בדיקות אחרי הפרסום...", "WAIT")
    report = checks.run(
        change_plan.url, checks.visible_text(baseline_html)[:120], baseline,
        fetch, internal_links=[target] if target.startswith("http") else None,
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
    kv("מדידה חוזרת", "14 · 28 · 56 יום")

    plugin = seo_meta.detect_plugin(post.get("meta") or {}, baseline_html)
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
#  Main
# ═══════════════════════════════════════════════════════

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Internal Anchors")
    parser.add_argument("--client", required=True, help="דומיין הלקוח מ-clients.json")
    parser.add_argument("--mode", choices=["analyze", "plan", "publish"],
                        default="analyze", help="שלב בתהליך (ברירת מחדל: analyze)")
    parser.add_argument("--self-check", action="store_true",
                        help="בודק הגדרות, robots.txt וגישה לפני סריקה")
    parser.add_argument("--queries", help="קובץ שאילתות מ-GSC")
    parser.add_argument("--known", help="רשימת כתובות מה-sitemap — לזיהוי יתומים")
    parser.add_argument("--max-pages", type=int, default=crawler.DEFAULT_MAX_PAGES,
                        help="תקרת דפים לסריקה")
    parser.add_argument("--from", dest="source", help="דף המקור (למצב plan)")
    parser.add_argument("--to", dest="target", help="דף היעד (למצב plan)")
    parser.add_argument("--phrase", help="הביטוי שייעטף בקישור")
    parser.add_argument("--plan", help="מזהה התוכנית (למצב publish)")
    parser.add_argument("--dry-run", action="store_true",
                        help="מייצר תוכנית בלי לכתוב כלום")
    args = parser.parse_args(argv)

    secrets.load_env()

    try:
        if args.self_check:
            return self_check(args.client)

        if args.mode == "analyze":
            # בלי --queries הנתונים נמשכים לבד מ-GSC Wizard.
            return analyze(args.client, args.queries, args.known, args.max_pages)

        if args.mode == "plan":
            if not (args.source and args.target and args.phrase):
                log("--mode plan דורש --from, --to ו---phrase", "ERR")
                return 1
            return build_plan(args.client, args.source, args.target,
                              args.phrase, args.dry_run)

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
