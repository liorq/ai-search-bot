#!/usr/bin/env python3
"""
WordPress Migration — העברת אתר עם דרך חזרה
================================================================
מריץ את ההעברה כמכונת מצבים על הדיסק: כל שלב נרשם, ריצה שנקטעה נקראת אחר כך,
והרצה חוזרת אף פעם לא מבצעת שוב שלב שהושלם.

שלושה כללים שלא נשברים:
  · גיבוי מאומת לשני הצדדים לפני שנוגעים ביעד
  · 500MB גבול קשיח על קובץ ה-wpress הסופי, נמדד על הקובץ עצמו
  · מדיה ותוכן לא נמחקים כדי להיכנס לגבול — עוצרים ומראים מה גדול

שימוש:
    python wp_migration.py --self-check --client example.com
    python wp_migration.py --mode preflight --client example.com --target new.com
    python wp_migration.py --mode backup   --client example.com \\
        --source-backup src.wpress --target-backup dst.wpress
    python wp_migration.py --mode export   --client example.com --archive site.wpress
    python wp_migration.py --mode import   --client example.com --confirm
    python wp_migration.py --mode postcheck --client example.com --url https://new.com/
    python wp_migration.py --mode status   --client example.com
"""

from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
import uuid
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
from seo_core.log import banner, kv, log, rule                          # noqa: E402
from seo_core.wp import limits as wp_limits                             # noqa: E402
from seo_core.wp import migration                                       # noqa: E402

CLI_TIMEOUT  = 1800       # שניות — ייצוא של אתר גדול לוקח זמן

#: PHP שמחזיר את כל המגבלות בקריאה אחת, כולל דיסק פנוי.
LIMITS_SNIPPET = (
    "echo json_encode(["
    "'upload_max_filesize'=>ini_get('upload_max_filesize'),"
    "'post_max_size'=>ini_get('post_max_size'),"
    "'memory_limit'=>ini_get('memory_limit'),"
    "'max_execution_time'=>ini_get('max_execution_time'),"
    "'free_disk_bytes'=>disk_free_space(ABSPATH),"
    "'php_version'=>PHP_VERSION,"
    "'wp_version'=>get_bloginfo('version')]);"
)


def migrations_dir(domain: str) -> Path:
    return paths.data_dir(domain) / "migrations"


def runner(prefix: str):
    """מריץ פקודת WP-CLI דרך הקידומת שהוגדרה — מקומית או ב-SSH."""
    base = shlex.split(prefix)

    def run(argv: list[str]) -> tuple[int, str, str]:
        command = base + argv[1:] if argv and argv[0] == "wp" else base + argv
        try:
            done = subprocess.run(
                command, capture_output=True, text=True, timeout=CLI_TIMEOUT
            )
        except FileNotFoundError:
            return 127, "", f"לא נמצאה הפקודה: {command[0]}"
        except subprocess.TimeoutExpired:
            return 124, "", "הפקודה חרגה מזמן ההמתנה"
        return done.returncode, done.stdout, done.stderr

    return run


def collect_limits(run, site: str, plugin_max: str | None = None) -> wp_limits.ServerLimits:
    """מגבלות מהשרת. נכשל — מחזיר "לא ידוע", ולא "ללא הגבלה"."""
    import json

    code, out, _ = run(["wp", "eval", LIMITS_SNIPPET])
    if code != 0:
        return wp_limits.unknown(site, plugin_max)
    try:
        server = wp_limits.from_wp_cli(json.loads(out.strip()), site=site)
    except json.JSONDecodeError:
        return wp_limits.unknown(site, plugin_max)

    if plugin_max:
        server.plugin_max_upload = wp_limits.parse_size(plugin_max)
    return server


def open_state(domain: str, migration_id: str | None, target: str = "") -> tuple:
    directory = migrations_dir(domain)
    if migration_id:
        loaded = migration.load(migration_id, directory)
        if not loaded:
            log(loaded.detail, "ERR")
            return None, directory
        return loaded.data["state"], directory

    existing = sorted(directory.glob("mig_*.json")) if directory.exists() else []
    if existing:
        loaded = migration.load(existing[-1].stem, directory)
        if loaded:
            return loaded.data["state"], directory

    if not target:
        log("אין העברה פתוחה — הרץ --mode preflight עם --target", "ERR")
        return None, directory
    return migration.MigrationState.new(f"mig_{uuid.uuid4().hex[:8]}", domain, target), directory


# ═══════════════════════════════════════════════════════
#  בדיקת תקינות
# ═══════════════════════════════════════════════════════

def self_check(domain: str, source_cmd: str, target_cmd: str) -> int:
    banner("🔍 בדיקת תקינות")

    try:
        client = clients.load(domain)
    except clients.ClientConfigError as exc:
        log(str(exc), "ERR")
        return 1
    log(f"לקוח {domain} נטען מהרישום", "OK")
    kv("אתר", client.cms.base_url or "—")
    kv("מיקום המפתחות", paths.home())
    for problem in paths.warnings_for():
        log(problem, "WARN")

    for label, prefix in (("מקור", source_cmd), ("יעד", target_cmd)):
        report = migration.detect_tooling(runner(prefix))
        log(f"{label} ({prefix}): {report.detail}", "OK" if report.has_wpcli else "WARN")
        kv(f"שיטה ב{label}", report.method)

    rule()
    log("שיטה browser פירושה הפעלה ידנית דרך ממשק הניהול — "
        "הסקיל ידפיס בדיוק מה לעשות ויחכה לאישור", "INFO")
    return 0


# ═══════════════════════════════════════════════════════
#  שלב 1 — preflight
# ═══════════════════════════════════════════════════════

def preflight(domain, target, source_cmd, target_cmd, plugin_max) -> int:
    state, directory = open_state(domain, None, target)
    if state is None:
        return 1

    gate = migration.begin(state, "preflight")
    if not gate and gate.code != "already_done":
        log(gate.detail, "ERR")
        return 1

    banner(f"🚚 הכנה — {state.source} → {state.target}")
    kv("מזהה העברה", state.migration_id)

    source_tools = migration.detect_tooling(runner(source_cmd))
    target_tools = migration.detect_tooling(runner(target_cmd))
    state.method = target_tools.method

    for label, report in (("מקור", source_tools), ("יעד", target_tools)):
        log(f"{label}: {report.detail}", "OK" if report.has_plugin else "WARN")

    source_limits = collect_limits(runner(source_cmd), state.source)
    target_limits = collect_limits(runner(target_cmd), state.target, plugin_max)
    rule()
    kv("מגבלות מקור", source_limits.summary())
    kv("מגבלות יעד", target_limits.summary())
    rule()

    if not target_limits.collected:
        log(
            "לא ידועות המגבלות ביעד. פתח את All-in-One WP Migration > Import, "
            "קרא את המגבלה שהוא מציג, והרץ שוב עם --target-max-upload 256M",
            "WARN",
        )

    ceiling = target_limits.import_ceiling
    if ceiling is not None and ceiling < migration.HARD_ARCHIVE_LIMIT:
        log(
            f"תקרת הייבוא ביעד היא {wp_limits.human(ceiling)} "
            f"({target_limits.binding_constraint}) — נמוכה מהגבול הקשיח. "
            "זה המספר שקובע.",
            "WARN",
        )

    migration.complete(
        state, "preflight",
        f"מקור: {source_limits.summary()} · יעד: {target_limits.summary()}",
    )
    migration.save(state, directory)
    log(f"נשמר. השלב הבא: {state.next_stage}", "OK")
    return 0


# ═══════════════════════════════════════════════════════
#  שלב 2 — גיבויים, ואימות שלהם
# ═══════════════════════════════════════════════════════

def backup(domain, source_backup, target_backup, migration_id) -> int:
    state, directory = open_state(domain, migration_id)
    if state is None:
        return 1

    banner("💾 גיבויים")
    outcomes = {}
    for stage, path in (("backup_source", source_backup), ("backup_target", target_backup)):
        label = "מקור" if stage == "backup_source" else "יעד"
        gate = migration.begin(state, stage)
        if not gate and gate.code == "out_of_order":
            log(gate.detail, "ERR")
            return 1

        verdict = migration.verify_archive(Path(path))
        outcomes[stage] = verdict
        log(f"גיבוי {label}: {verdict.detail}", "OK" if verdict else "ERR")
        if verdict:
            migration.complete(state, stage, f"{path} — {verdict.detail}")
        else:
            migration.mark_failed(state, stage, verdict.detail)

    pair = migration.verify_backup_pair(outcomes["backup_source"], outcomes["backup_target"])
    rule()
    if not pair:
        log(pair.detail, "ERR")
        migration.save(state, directory)
        return 1

    migration.complete(state, "verify_backups", pair.detail)
    migration.save(state, directory)
    log(pair.detail, "OK")
    log("אימות מבני בלבד — הוכחה מלאה היא שחזור בפועל לסביבת בדיקה", "INFO")
    return 0


# ═══════════════════════════════════════════════════════
#  שלב 3 — ייצוא ושער הגודל
# ═══════════════════════════════════════════════════════

def export(domain, archive, target_cmd, migration_id, dry_run, plugin_max) -> int:
    state, directory = open_state(domain, migration_id)
    if state is None:
        return 1

    gate = migration.begin(state, "export")
    if not gate and gate.code == "out_of_order":
        log(gate.detail, "ERR")
        log("גיבוי שלא אומת הוא לא גיבוי — אין ייצוא לפניו", "ERR")
        return 1

    path = Path(archive)
    verdict = migration.verify_archive(path)
    banner("📦 ייצוא")
    kv("קובץ", path.name)
    log(verdict.detail, "OK" if verdict else "ERR")
    if not verdict:
        return 1

    size = verdict.data["size"]
    state.archive_path, state.archive_bytes = str(path), size
    migration.complete(state, "export", f"{wp_limits.human(size)}")

    target_limits = collect_limits(runner(target_cmd), state.target, plugin_max)
    decision = migration.size_gate(
        size, target_limits, largest=migration.largest_entries(path)
    )
    rule()
    if not decision:
        log(decision.detail, "ERR")
        # הפירוט המלא כבר הודפס; ביומן נשמרת הסיבה עצמה.
        migration.mark_failed(state, "size_gate", decision.detail.split("\n\n")[0])
        migration.save(state, directory)
        print_prune_options()
        return 1

    log(decision.detail, "OK")
    if dry_run:
        log("dry-run — המצב לא נשמר", "SKIP")
        return 0

    migration.complete(state, "size_gate", decision.detail)
    migration.save(state, directory)
    log(f"השלב הבא: {state.next_stage} — דורש --confirm", "OK")
    return 0


def print_prune_options() -> None:
    print("\n  מה כן מותר להסיר מהייצוא:")
    for name, description in migration.PRUNABLE.items():
        print(f"    · {description}")
    print("\n  מה לא נוגעים בו, בשום גודל:")
    for item in migration.NEVER_PRUNE:
        print(f"    · {item}")
    print()


# ═══════════════════════════════════════════════════════
#  שלב 4 — ייבוא, רק באישור מפורש
# ═══════════════════════════════════════════════════════

def run_import(domain, confirm, target_cmd, migration_id) -> int:
    state, directory = open_state(domain, migration_id)
    if state is None:
        return 1

    gate = migration.begin(state, "import")
    if not gate:
        log(gate.detail, "ERR" if gate.code == "out_of_order" else "SKIP")
        return 0 if gate.code == "already_done" else 1

    banner("⚠️  ייבוא")
    kv("קובץ", state.archive_path)
    kv("גודל", wp_limits.human(state.archive_bytes))
    kv("נדרס", state.target)
    rule()
    print("  אתר היעד ייכתב מחדש במלואו. גיבוי היעד אומת ונשמר.\n")

    if not confirm:
        log("הייבוא דורש --confirm בנוסף לאישור המוקלד", "ERR")
        return 1
    if input("  הקלד 'כן' כדי לייבא: ").strip() != "כן":
        log("בוטל — שום דבר לא נגע", "SKIP")
        return 1

    state.import_approved = True
    migration.save(state, directory)

    report = migration.detect_tooling(runner(target_cmd))
    if not report.can_import:
        log("אין פקודת ייבוא ב-WP-CLI ביעד — יש להריץ דרך ממשק הניהול", "WARN")
        print(f"\n  1. העלה את {state.archive_path} דרך All-in-One WP Migration > Import")
        print("  2. אשר את אזהרת הדריסה")
        print("  3. שמור קישורים קבועים פעמיים\n")
        print(f"  ואז: --mode postcheck --client {domain} --url https://{state.target}/\n")
        return 0

    log("מייבא דרך WP-CLI...", "WAIT")
    code, out, err = runner(target_cmd)(["wp", "ai1wm", "restore", state.archive_path, "--yes"])
    if code != 0:
        detail = (err or out).strip()[:300]
        migration.mark_failed(state, "import", detail)
        migration.save(state, directory)
        log(f"הייבוא נכשל: {detail}", "ERR")
        rule()
        print(migration.recovery_instructions(state))
        return 1

    migration.complete(state, "import", "הייבוא הושלם")
    migration.save(state, directory)
    log("הייבוא הושלם — הרץ --mode postcheck", "OK")
    return 0


# ═══════════════════════════════════════════════════════
#  שלב 5 — בדיקות אחרי
# ═══════════════════════════════════════════════════════

def postcheck(domain, urls, migration_id) -> int:
    state, directory = open_state(domain, migration_id)
    if state is None:
        return 1

    def fetch(url: str) -> tuple[int, str]:
        import requests
        response = requests.get(url, timeout=30)
        return response.status_code, response.text

    robots = None
    try:
        status, body = fetch(f"https://{state.target}/robots.txt")
        robots = body if status == 200 else None
    except Exception:
        pass

    banner(f"✅ בדיקות אחרי ההעברה — {state.target}")
    failures = 0

    for url in urls:
        print(f"\n  {url}")
        report = migration.postcheck(url, state.source, state.target, fetch,
                                     robots_txt=robots)
        for check in report.checks:
            log(f"{check.name}: {check.detail}", "OK" if check.passed else
                ("ERR" if check.breaking else "WARN"))
        failures += len(report.breaking_failures)

    rule()
    if failures:
        log(f"{failures} כשלים חוסמים — ההעברה לא הושלמה", "ERR")
        migration.mark_failed(state, "postcheck", f"{failures} כשלים")
        migration.save(state, directory)
        return 1

    migration.complete(state, "postcheck", f"{len(urls)} דפים נבדקו")
    migration.save(state, directory)
    log("הכל עבר", "OK")
    log("הגיבויים נשמרים עד אישור מפורש למחיקה", "INFO")
    return 0


# ═══════════════════════════════════════════════════════
#  מצב ושחזור
# ═══════════════════════════════════════════════════════

def status(domain, migration_id) -> int:
    state, _ = open_state(domain, migration_id)
    if state is None:
        return 1

    banner(f"📋 {state.migration_id} — {state.source} → {state.target}")
    for name in migration.STAGES:
        stage = state.stage(name)
        icon = {"done": "OK", "failed": "ERR", "skipped": "SKIP"}.get(stage.status, "INFO")
        log(f"{name}: {stage.status} {stage.detail}".strip(), icon)
    rule()
    print(migration.recovery_instructions(state))
    print()
    return 0


# ═══════════════════════════════════════════════════════
#  Main
# ═══════════════════════════════════════════════════════

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="WordPress Migration")
    parser.add_argument("--client", required=True, help="דומיין הלקוח מ-clients.json")
    parser.add_argument("--mode", default="status",
                        choices=["preflight", "backup", "export", "import",
                                 "postcheck", "status"],
                        help="שלב בתהליך (ברירת מחדל: status)")
    parser.add_argument("--self-check", action="store_true",
                        help="בודק הגדרות וזמינות WP-CLI בשני הצדדים")
    parser.add_argument("--target", help="דומיין היעד (למצב preflight)")
    parser.add_argument("--migration", help="מזהה העברה קיימת")
    parser.add_argument("--source-cmd", default="wp",
                        help="קידומת WP-CLI במקור, למשל 'ssh host wp'")
    parser.add_argument("--target-cmd", default="wp", help="קידומת WP-CLI ביעד")
    parser.add_argument("--target-max-upload",
                        help="המגבלה שהתוסף מציג ביעד, למשל 256M — כשאין WP-CLI")
    parser.add_argument("--source-backup", help="נתיב לגיבוי המקור")
    parser.add_argument("--target-backup", help="נתיב לגיבוי היעד")
    parser.add_argument("--archive", help="נתיב לקובץ ה-wpress לייבוא")
    parser.add_argument("--url", action="append", default=[],
                        help="כתובת לבדיקה אחרי ההעברה (אפשר לחזור)")
    parser.add_argument("--confirm", action="store_true",
                        help="נדרש לייבוא, בנוסף לאישור המוקלד")
    parser.add_argument("--dry-run", action="store_true",
                        help="מריץ את השערים בלי לשמור מצב")
    args = parser.parse_args(argv)

    secrets.load_env()

    try:
        if args.self_check:
            return self_check(args.client, args.source_cmd, args.target_cmd)

        if args.mode == "preflight":
            if not args.target:
                log("--mode preflight דורש --target", "ERR")
                return 1
            return preflight(args.client, args.target, args.source_cmd,
                             args.target_cmd, args.target_max_upload)

        if args.mode == "backup":
            if not (args.source_backup and args.target_backup):
                log("--mode backup דורש --source-backup ו---target-backup", "ERR")
                return 1
            return backup(args.client, args.source_backup, args.target_backup,
                          args.migration)

        if args.mode == "export":
            if not args.archive:
                log("--mode export דורש --archive", "ERR")
                return 1
            return export(args.client, args.archive, args.target_cmd,
                          args.migration, args.dry_run, args.target_max_upload)

        if args.mode == "import":
            if args.dry_run:
                log("--dry-run לא חל על import — השתמש ב---mode export", "ERR")
                return 1
            return run_import(args.client, args.confirm, args.target_cmd, args.migration)

        if args.mode == "postcheck":
            if not args.url:
                log("--mode postcheck דורש לפחות --url אחד", "ERR")
                return 1
            return postcheck(args.client, args.url, args.migration)

        return status(args.client, args.migration)

    except clients.ClientConfigError as exc:
        log(str(exc), "ERR")
        return 1
    except KeyboardInterrupt:
        log("הופסק — המצב נשמר, הרץ --mode status", "SKIP")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
