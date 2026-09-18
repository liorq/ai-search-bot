#!/usr/bin/env python3
"""
CTR Titles — הטייטל והתיאור של דפים שמדורגים טוב ולא מקבלים קליקים
================================================================
לוקח את טבלת השאילתות מ-Search Console, קורא את הטייטל והתיאור מהדף החי
כפי שגוגל מקבל אותם, ומחפש סיבה מבנית לפער בין הקליקים שהדף מקבל לקליקים
שהמיקום שלו אמור להביא.

הטייטל הוא גם אות דירוג, ולכן כל הצעה נבדקת מול המילים שהדף כבר מקבל עליהן
קליקים — הצעה שמורידה אחת מהן נדחית, לא מסומנת באזהרה.

שימוש:
    python ctr_titles.py --self-check --client example.com
    python ctr_titles.py --mode analyze --client example.com \\
        --queries queries.json [--snippets snippets.json] [--serp serp.json]
    python ctr_titles.py --mode plan --client example.com --queries queries.json \\
        --url https://example.com/springs \\
        --title "Torsion Spring Repair Denver" --description "..."
    python ctr_titles.py --mode publish --client example.com --plan plan_ab12
    python ctr_titles.py --mode verify --client example.com \\
        --queries before.json --after after.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
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
from seo_core.sources import queries as gsc                              # noqa: E402
from seo_core.text import pixels, snippets                               # noqa: E402
from seo_core.wp import backup as wp_backup                              # noqa: E402
from seo_core.wp import content as wp_content                            # noqa: E402
from seo_core.wp import seo_meta                                         # noqa: E402
from seo_core.wp.client import WordPressClient                           # noqa: E402

MAX_LISTED     = 12
FETCH_DELAY    = 0.4          # שניות בין בקשות — קריאה מנומסת לאתר של לקוח
FETCH_TIMEOUT  = 20
PROBLEM_ICONS  = {
    "head_term_missing":     "🔤",
    "title_duplicate":       "👯",
    "title_boilerplate":     "🏷️ ",
    "description_missing":   "📄",
    "title_truncated":       "✂️ ",
    "description_truncated": "✂️ ",
    "title_short":           "📏",
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
        log(f"CMS מסוג {client.cms.type} — ניתוח בלבד, בלי כתיבה", "WARN")
        return 0

    log("בודק גישה ל-WordPress...", "WAIT")
    caps = make_client(client).probe()
    log(caps.summary(), "OK" if caps.can_write_meta("page") else "WARN")
    if not caps.can_write_meta("page"):
        log("meta לא חשוף ב-REST — כתיבת טייטל תתקבל ותיזרק בשקט", "WARN")

    from seo_core.wp import rehearsal
    drilled = rehearsal.require(client.data_dir)
    log(drilled.detail, "OK" if drilled else "WARN")

    rule()
    if caps.can_write_meta("page") and drilled:
        log("הכל מוכן", "OK")
    else:
        log("אפשר לנתח; פרסום ייחסם", "WARN")
    return 0


# ═══════════════════════════════════════════════════════
#  שלב 1 — ניתוח
# ═══════════════════════════════════════════════════════

def load_snippets(path: str | None, urls: list[str]) -> list[snippets.PageSnippet]:
    """הטייטל והתיאור לכל דף — מקובץ, או מהדף החי עצמו."""
    if path:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        entries = raw.get("pages") if isinstance(raw, dict) else raw
        return [
            snippets.PageSnippet(
                url=str(entry["url"]),
                title=str(entry.get("title", "")),
                description=str(entry.get("description", "")),
                stored_title=str(entry.get("stored_title", "")),
                post_id=entry.get("post_id"),
            )
            for entry in entries
        ]

    collected: list[snippets.PageSnippet] = []
    log(f"קורא טייטלים מ-{len(urls)} דפים חיים...", "WAIT")
    for index, url in enumerate(urls):
        try:
            collected.append(snippets.from_html(url, fetch_html(url)))
        except Exception as exc:
            log(f"{url}: {exc}", "WARN")
        if index + 1 < len(urls):
            time.sleep(FETCH_DELAY)
    return collected


def candidate_urls(rows: list[gsc.QueryRow]) -> list[str]:
    """רק דפים שהסקיל הזה בכלל יכול לטפל בהם — אין טעם לסרוק את השאר."""
    shortlist = []
    for url, page in snippets.aggregate(rows).items():
        visible = page.visible()
        if visible.impressions >= gsc.MIN_IMPRESSIONS:
            shortlist.append((visible.impressions, url))
    return [url for _, url in sorted(shortlist, reverse=True)]


def analyze(domain: str, queries_path: Path, snippets_path: str | None,
            serp_path: str | None) -> int:
    clients.load(domain)          # מאמת שהלקוח מוגדר לפני שקוראים משהו
    dirs = client_dirs(domain)

    loaded = gsc.load_export(queries_path)
    if not loaded:
        log(loaded.detail, "ERR")
        return 1
    log(loaded.detail, "OK")
    rows = loaded.data["rows"]

    serp_features = None
    if serp_path:
        serp_features = json.loads(Path(serp_path).read_text(encoding="utf-8"))
        log(f"תכונות SERP ל-{len(serp_features)} שאילתות — "
            f"שאילתות עם AI Overview לא ייספרו בפער", "OK")
    else:
        log("לא סופקו תכונות SERP — שאילתה שיושבת מתחת ל-AI Overview "
            "תיראה כמו בעיית טייטל. הוסף --serp אם יש נתונים", "WARN")

    pages = load_snippets(snippets_path, candidate_urls(rows))
    if not pages:
        log("אין טייטלים לנתח", "ERR")
        return 1

    curve = gsc.build_curve(rows)
    brand = snippets.infer_site_name(pages)
    audits = snippets.analyse(rows, pages, curve, site_name=brand,
                              serp_features=serp_features)

    banner(f"✏️  טייטלים ותיאורים — {domain}")
    kv("חלון", loaded.data["window"])
    kv("דפים שנבדקו", len(pages))
    kv("שם המותג בטייטלים", brand or "לא זוהה")
    kv("עקומת CTR", curve.describe())

    if not audits:
        rule()
        log("אף דף לא מקבל פחות ממה שהמיקום שלו אמור להביא", "OK")
        return 0

    structural = [a for a in audits if a.has_structural_cause]
    kv("דפים מתחת לצפי", len(audits))
    kv("מתוכם עם סיבה מבנית", len(structural))
    kv("קליקים ברי-השגה", f"{sum(a.claimable_clicks for a in audits):,.0f}")
    rule()

    for audit in audits[:MAX_LISTED]:
        print(f"\n  +{audit.claimable_clicks:>5.0f} קליקים  {audit.url}")
        print(f"           {audit.summary()}")
        print(f"           טייטל: “{pixels.preview(audit.snippet.title, 'title')}”")
        for problem in audit.problems:
            print(f"           {PROBLEM_ICONS.get(problem.kind, '•')} {problem.detail}")
        if not audit.problems:
            print("           ❓ לא נמצאה סיבה מבנית — שווה ניסוי, לא אבחנה")
        if audit.suppressed_queries:
            print(f"           ⏭️  לא נספרו: {', '.join(audit.suppressed_queries[:3])}")

    findings = [snippets.to_finding(a, domain, curve) for a in audits]
    path = save_findings(findings, dirs["reports"] / "ctr_findings.json")

    rule()
    top = audits[0]
    print("\n  הצעד הבא:\n")
    print(f"    python ctr_titles.py --mode plan --client {domain} \\")
    print(f"        --queries {queries_path} --url {top.url} \\")
    print('        --title "<טייטל חדש>" --description "<תיאור חדש>"')
    must_keep = sorted(t for t in top.page.protected_terms()
                       if gsc.covers(t, top.snippet.terms()))
    if must_keep:
        print(f"\n    חייב להישאר בטייטל: {', '.join(must_keep)}")
    print(f"\n  📄 ממצאים מלאים: {path}\n")
    return 0


# ═══════════════════════════════════════════════════════
#  שלב 2 — תוכנית
# ═══════════════════════════════════════════════════════

def build_plan(domain, url, queries_path, new_title, new_description, dry_run) -> int:
    client = clients.load(domain)
    dirs = client_dirs(domain)

    loaded = gsc.load_export(queries_path)
    if not loaded:
        log(loaded.detail, "ERR")
        return 1
    rows = loaded.data["rows"]
    curve = gsc.build_curve(rows)

    wp = make_client(client)
    log("בודק יכולות באתר...", "WAIT")
    caps = wp.probe()
    if not caps.can_write_meta("page"):
        log(f"אי אפשר לכתוב meta: {caps.summary()}", "ERR")
        log("הטקסט מוכן להדבקה ידנית בעורך:", "INFO")
        print(f"\n  טייטל: {new_title}\n  תיאור: {new_description}\n")
        return 1

    found = wp.find_by_url(url)
    if not found:
        log(found.detail, "ERR")
        return 1
    post, post_type = found.data["payload"], found.data["post_type"]
    post_meta = dict(post.get("meta") or {})

    try:
        live_html = fetch_html(url)
    except Exception as exc:
        log(f"לא הצלחתי לקרוא את הדף החי: {exc}", "ERR")
        return 1

    plugin = seo_meta.detect_plugin(post_meta, live_html)
    if plugin == "unknown":
        log("לא זוהה תוסף SEO — כתיבה כאן תהיה ניחוש", "ERR")
        return 1
    log(f"תוסף SEO: {plugin}", "OK")

    current = seo_meta.read(post_meta, plugin).data["fields"]
    live = snippets.from_html(url, live_html, post_id=post.get("id"),
                              stored_title=current.title)

    # התבנית נלמדת מההפרש בין מה שהתוסף שומר למה שהדפדפן מקבל.
    prefix, suffix = pixels.infer_affixes(current.title, live.title)
    predicted = pixels.render_like(new_title, prefix, suffix)
    if prefix or suffix:
        log(f"התבנית באתר מוסיפה “{prefix}…{suffix}” — "
            f"הטייטל נמדד כ-“{predicted}”", "INFO")

    page_rows = [r for r in rows if r.url == url]
    if not page_rows:
        log(f"אין שורות שאילתה לדף {url} בקובץ — בלי זה אין מה להגן עליו", "ERR")
        return 1

    performance = snippets.PagePerformance(url=url, queries=page_rows)
    audit = snippets.audit_page(live, performance, curve)
    if audit is None:
        log("הדף לא מתחת לצפי לפי הנתונים — אין סיבה לגעת בטייטל שכבר עובד",
            "ERR")
        return 1

    verdict = snippets.validate(predicted, new_description, audit)
    rule()
    for warning in verdict.data.get("warnings", []):
        log(warning, "WARN")
    if not verdict:
        for reason in verdict.data["blocking"]:
            log(reason, "ERR")
        return 1
    log(verdict.detail, "OK")

    payload_built = seo_meta.write_payload(
        plugin, title=new_title, description=new_description or None)
    if not payload_built:
        log(payload_built.detail, "ERR")
        return 1
    touched = payload_built.data["touched_keys"]

    loaded_post = wp_content.load(post, post_type)
    if not loaded_post:
        log(loaded_post.detail, "ERR")
        return 1
    page = loaded_post.data["content"]

    # שער 1 — מילים שיורדות מהטייטל נבדקות מול השאילתות המוגנות של הדף.
    dropped = " ".join(
        w for w in live.title.split()
        if w.lower() not in predicted.lower()
    )
    report = risk.assess(dropped, risk.protected_queries(
        [{"query": r.query, "clicks": r.clicks,
          "impressions": r.impressions, "position": r.position} for r in page_rows]
    ))

    plan_id = f"plan_{uuid.uuid4().hex[:8]}"
    built = plan_mod.compose(
        plan_id=plan_id, client=domain, skill="ctr-titles", url=url,
        post_id=page.post_id, post_type=post_type, builder=page.builder,
        summary=f"שכתוב טייטל ותיאור ב-{plugin}",
        rationale=audit.problems[0].detail if audit.problems else audit.summary(),
        payload={"meta": payload_built.data["meta"]},
        inverse={"meta": seo_meta.inverse_payload(post_meta, touched)},
        before_text=f"{live.title}\n{live.description}",
        after_text=f"{predicted}\n{new_description}",
        before_hash=page.hash, risk=report,
        findings=[snippets.to_finding(audit, domain, curve)],
    )
    if not built:
        log(built.detail, "ERR")
        return 1

    change_plan = built.data["plan"]
    doc, _ = plan_mod.save(change_plan, dirs["plans"])

    banner(f"📝 תוכנית {plan_id}")
    kv("דף", url)
    kv("תוסף", plugin)
    kv("סיכון", report.summary())
    rule()
    print(f"\n  לפני:  “{pixels.preview(live.title, 'title')}”")
    print(f"  אחרי:  “{pixels.preview(predicted, 'title')}”")
    print(f"         {pixels.measure(predicted, 'title').describe()}")
    if new_description:
        print(f"\n  תיאור: “{pixels.preview(new_description, 'description')}”")
        print(f"         {pixels.measure(new_description, 'description').describe()}")
    rule()
    print(f"\n  ⚠️  {snippets.GOOGLE_REWRITE_NOTE}")

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
    from seo_core.wp import rehearsal

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
            change_id="n/a", plan_id=plan_id, skill="ctr-titles", client=domain,
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
    before_snippet = snippets.from_html(change_plan.url, baseline_html)

    log("כותב לאתר...", "WAIT")
    written = wp.update_post(
        change_plan.post_id, post_type=change_plan.post_type,
        meta=change_plan.payload.get("meta"),
    )
    if not written:
        log(f"הכתיבה נכשלה: {written.detail}", "ERR")
        return 1
    log("נכתב", "OK")

    record = ChangeRecord(
        change_id=change_id, plan_id=plan_id, skill="ctr-titles", client=domain,
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

    log("בודק שהדף החי מגיש את מה שנכתב...", "WAIT")
    try:
        after_html = fetch_html(change_plan.url)
    except Exception as exc:
        log(f"לא הצלחתי לקרוא את הדף אחרי הכתיבה: {exc}", "ERR")
        after_html = ""

    new_title, _, new_description = change_plan.after_text.partition("\n")
    live = snippets.confirm_rendered(after_html, new_title, new_description)
    log(live.detail, "OK" if live else "ERR")

    # גוף הדף לא משתנה בשכתוב טייטל, אז הבדיקות הרגילות מוודאות שלא נשבר כלום.
    report = checks.run(
        change_plan.url, checks.visible_text(baseline_html)[:120], baseline, fetch)
    rule()
    for check in report.checks:
        log(f"{check.name}: {check.detail}", "OK" if check.passed else "ERR")
    rule()

    if report.should_roll_back or not live:
        reasons = [c.name for c in report.breaking_failures]
        if not live:
            reasons.append("הטייטל לא עלה לדף החי")
        log("כשל — משחזר", "ERR")
        decision = rollback.on_technical_failure(record, reasons)
        outcome = rollback.execute(decision, record,
                                   lambda: wp_backup.restore(wp, snapshot))
        log(outcome.detail, "OK" if outcome else "ERR")
        record.status = "rolled_back" if outcome else "applied"
        record.notes.append(outcome.detail)
        ledger.record(record, dirs["base"])
        return 1

    record.notes.append(f"לפני: {before_snippet.title}")
    ledger.record(record, dirs["base"])

    banner("✅ פורסם")
    kv("שינוי", change_id)
    kv("מדידה חוזרת", "14 · 28 · 56 יום")
    print(f"\n  ⚠️  {snippets.GOOGLE_REWRITE_NOTE}")

    plugin = seo_meta.detect_plugin(post.get("meta") or {}, baseline_html)
    if plugin != "unknown":
        rule()
        log(f"הדף צריך פתיחה ושמירה ב-{plugin} כדי שהציון יחושב מחדש", "WARN")
        print(f"\n    {client.cms.base_url.rstrip('/')}"
              f"/wp-admin/post.php?post={change_plan.post_id}&action=edit")
        print("\n  רשימה מרוכזת של כל הדפים שממתינים:")
        print(f"    python -m seo_core.wp.seo_refresh --client {domain}")
    print()
    return 0


# ═══════════════════════════════════════════════════════
#  שלב 4 — מדידה, אחרי נטרול תזוזת המיקום
# ═══════════════════════════════════════════════════════

def verify(domain: str, before_path: str | None, after_path: str | None) -> int:
    dirs = client_dirs(domain)

    if not (before_path and after_path):
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
        rule()
        log("שלוף שאילתות לחלון שאחרי השינוי והרץ שוב עם "
            "--queries <לפני> --after <אחרי>", "INFO")
        return 0

    before = gsc.load_export(Path(before_path))
    after = gsc.load_export(Path(after_path))
    if not before or not after:
        log((before or after).detail, "ERR")
        return 1

    curve = gsc.build_curve(after.data["rows"])
    before_pages = snippets.aggregate(before.data["rows"])
    after_pages = snippets.aggregate(after.data["rows"])

    banner(f"📊 מה השכתוב עשה — {domain}")
    kv("עקומת CTR", curve.describe())
    rule()

    shared = [u for u in after_pages if u in before_pages]
    if not shared:
        log("אין דפים משותפים לשני החלונות", "ERR")
        return 1

    for url in shared:
        result = snippets.attribute(
            before_pages[url].visible(), after_pages[url].visible(), curve)
        icon = {"improved": "✅", "worse": "❌",
                "no_effect": "➖", "inconclusive": "❓"}[result.verdict]
        print(f"\n  {icon} {url}")
        print(f"       {result.describe()}")
        if snippets.ranking_regressed(result):
            log(f"הדף ירד {result.position_change:+.1f} מקומות מאז השכתוב — "
                "טייטל הוא אות דירוג, שווה לבדוק", "WARN")

    rule()
    print("\n  ירידה אחרי שכתוב לא מפעילה שחזור אוטומטי. לשחזור:")
    print(f"    python -m seo_core.change_guard.rollback --client {domain} "
          "--change <chg_id>\n")
    return 0


# ═══════════════════════════════════════════════════════
#  Main
# ═══════════════════════════════════════════════════════

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CTR Titles")
    parser.add_argument("--client", required=True, help="דומיין הלקוח מ-clients.json")
    parser.add_argument("--mode", choices=["analyze", "plan", "publish", "verify"],
                        default="analyze", help="שלב בתהליך (ברירת מחדל: analyze)")
    parser.add_argument("--self-check", action="store_true",
                        help="בודק הגדרות וגישה לפני שנוגעים במשהו")
    parser.add_argument("--queries", help="קובץ שאילתות מ-GSC")
    parser.add_argument("--snippets", help="קובץ טייטלים; בלעדיו נקראים מהדפים החיים")
    parser.add_argument("--serp", help="תכונות SERP לכל שאילתה — AI Overview וכדומה")
    parser.add_argument("--after", help="קובץ שאילתות לחלון שאחרי השינוי (למצב verify)")
    parser.add_argument("--url", help="כתובת הדף (למצב plan)")
    parser.add_argument("--title", help="הטייטל המוצע")
    parser.add_argument("--description", default="", help="התיאור המוצע")
    parser.add_argument("--plan", help="מזהה התוכנית (למצב publish)")
    parser.add_argument("--dry-run", action="store_true",
                        help="מייצר תוכנית בלי לכתוב כלום")
    args = parser.parse_args(argv)

    secrets.load_env()

    try:
        if args.self_check:
            return self_check(args.client)

        if args.mode == "analyze":
            if not args.queries:
                log("--mode analyze דורש --queries", "ERR")
                return 1
            return analyze(args.client, Path(args.queries), args.snippets, args.serp)

        if args.mode == "plan":
            if not (args.url and args.title and args.queries):
                log("--mode plan דורש --url, --title ו---queries", "ERR")
                return 1
            return build_plan(args.client, args.url, Path(args.queries),
                              args.title, args.description, args.dry_run)

        if args.mode == "publish":
            if not args.plan:
                log("--mode publish דורש --plan", "ERR")
                return 1
            if args.dry_run:
                log("--dry-run לא חל על publish — השתמש ב---mode plan", "ERR")
                return 1
            return publish(args.client, args.plan)

        return verify(args.client, args.queries, args.after)

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
