#!/usr/bin/env python3
"""
WordPress Speed Optimizer — אבחון, תעדוף, ואימות אחרי
================================================================
מודד מובייל ודסקטופ בנפרד, מפריד בין נתוני שדה (CrUX) לנתוני מעבדה
(Lighthouse), ומתעדף לפי השפעה צפויה על LCP/INP/CLS חלקי מאמץ — לא לפי
רשימת ההזדמנויות של PageSpeed כמו שהיא.

הסקיל לא כותב לאתר. כמעט כל תיקון מהירות הוא הגדרה בתוסף cache, בשרת או
במדיה — דברים שאין להם REST endpoint בטוח. מה שכן אוטומטי: האבחון, התעדוף,
פקודת העבודה המדויקת, והאימות שאחריה — כולל בדיקה שהטפסים והעיצוב שרדו.

שימוש:
    python speed_optimizer.py --self-check --client example.com
    python speed_optimizer.py --mode analyze --client example.com \\
        --url https://example.com/services --live
    python speed_optimizer.py --mode analyze --client example.com --psi-data psi.json
    python speed_optimizer.py --mode verify  --client example.com \\
        --url https://example.com/services --live
"""

from __future__ import annotations

import argparse
import json
import os
import sys
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

from seo_core import clients, secrets                                  # noqa: E402
from seo_core.change_guard import checks                                # noqa: E402
from seo_core.log import banner, kv, log, rule                          # noqa: E402
from seo_core.schema import save_findings                              # noqa: E402
from seo_core.sources import pagespeed                                 # noqa: E402

SEO_HOME     = Path.home() / ".claude" / "seo"
MAX_STEPS    = 6          # כמה פריטים בפקודת העבודה
METRIC_ICONS = {"lcp": "🖼️ ", "inp": "⚡", "cls": "📐"}
EFFORT_LABEL = {"s": "קל", "m": "בינוני", "l": "כבד"}


def client_dirs(domain: str) -> dict[str, Path]:
    base = SEO_HOME / "data" / domain
    return {"base": base, "reports": base / "reports", "speed": base / "speed"}


def fetch_page(url: str) -> tuple[int, str]:
    import requests
    response = requests.get(url, timeout=30)
    return response.status_code, response.text


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
    kv("נתוני המרה", "כן" if client.has_conversion_data else "לא")

    has_key = bool(os.environ.get("PAGESPEED_API_KEY"))
    log(
        "PAGESPEED_API_KEY קיים" if has_key else
        "אין PAGESPEED_API_KEY — --live יעבוד אבל ייחסם מהר",
        "OK" if has_key else "WARN",
    )

    rule()
    log("הסקיל לא כותב לאתר — אבחון, תעדוף ואימות בלבד", "INFO")
    return 0


# ═══════════════════════════════════════════════════════
#  שליפת נתונים
# ═══════════════════════════════════════════════════════

def gather(url: str, data_path: Path | None, live: bool, directory: Path) -> dict | None:
    """מחזיר {strategy: Diagnosis} — מקובץ שמור או מ-PSI חי."""
    if data_path:
        loaded = pagespeed.load_export(data_path)
        if not loaded:
            log(loaded.detail, "ERR")
            return None
        log(loaded.detail, "OK")
        return loaded.data["diagnoses"]

    if not live:
        log("צריך --psi-data או --live", "ERR")
        return None

    key = os.environ.get("PAGESPEED_API_KEY", "")
    raw: dict = {"url": url}
    diagnoses = {}
    for strategy in pagespeed.STRATEGIES:
        log(f"מודד {strategy}...", "WAIT")
        fetched = pagespeed.fetch(url, strategy, key)
        if not fetched:
            log(fetched.detail, "ERR" if strategy == "mobile" else "WARN")
            if strategy == "mobile":
                return None
            continue
        raw[strategy] = fetched.data["payload"]
        parsed = pagespeed.parse(raw[strategy], url=url, strategy=strategy)
        if not parsed:
            log(parsed.detail, "ERR")
            return None
        diagnoses[strategy] = parsed.data["diagnosis"]

    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    snapshot = directory / f"psi-{stamp}.json"
    with open(snapshot, "w", encoding="utf-8") as fh:
        json.dump(raw, fh, ensure_ascii=False, indent=2)
    log(f"נשמר: {snapshot.name}", "OK")
    return diagnoses


def load_traffic(path: str | None, url: str) -> dict | None:
    """נתוני תנועה לדף — בלעדיהם אין תרגום לקליקים, ולא נמציא אחד."""
    if not path:
        return None
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    pages = raw.get("pages") or {}
    return pages.get(url) or pages.get(url.rstrip("/")) or raw.get("page")


# ═══════════════════════════════════════════════════════
#  מצב analyze
# ═══════════════════════════════════════════════════════

def analyze(domain, url, data_path, live, traffic_path, dry_run) -> int:
    client = clients.load(domain)
    dirs = client_dirs(domain)

    diagnoses = gather(url, data_path, live, dirs["speed"])
    if diagnoses is None:
        return 1

    mobile = diagnoses["mobile"]
    traffic = load_traffic(traffic_path, url)
    finding = pagespeed.to_finding(mobile, client.domain, traffic)

    print_report(domain, diagnoses, finding, traffic)

    if dry_run:
        log("dry-run — לא נשמרו ממצאים", "SKIP")
        return 0

    path = save_findings([finding], dirs["reports"] / "speed_findings.json")
    print(f"\n  📄 ממצאים: {path}\n")
    return 0


def print_report(domain, diagnoses, finding, traffic) -> None:
    mobile = diagnoses["mobile"]
    desktop = diagnoses.get("desktop")

    banner(f"⚡ מהירות — {domain}")
    kv("דף", mobile.url)
    kv("שדה (מובייל)", mobile.field.summary())
    kv("מעבדה (מובייל)", mobile.lab.summary())
    if desktop:
        kv("מעבדה (דסקטופ)", desktop.lab.summary())
    kv("חיסכון צפוי", f"{mobile.total_expected_gain_ms:.0f}ms")
    rule()

    if not mobile.field.has_data:
        log("אין נתוני שדה — כל מה שלמטה הוא סימולציה, לא מדידה", "WARN")
    elif not mobile.field.failing:
        log("הדף עובר Core Web Vitals בשדה — אין כאן רווח בדירוג", "INFO")
    else:
        failing = ", ".join(m.upper() for m in mobile.field.failing)
        log(f"נכשל בשדה ב-{failing} — זה מה שגוגל רואה", "WARN")

    print("\n  פקודת עבודה, לפי סדר:\n")
    for index, opportunity in enumerate(mobile.ranked()[:MAX_STEPS], 1):
        icon = METRIC_ICONS.get(opportunity.metric, "•")
        tag = "" if opportunity.addresses_failure else "  (מטריקה תקינה בשדה)"
        print(f"  {index}. {icon} {opportunity.title}{tag}")
        print(f"       {opportunity.expected_gain_ms:>5.0f}ms צפוי · "
              f"מאמץ {EFFORT_LABEL[opportunity.effort]}")
        print(f"       {opportunity.hint}")

    rule()
    kv("קליקים באומדן", finding.impact_clicks)
    if finding.impact_conversions is not None:
        kv("המרות באומדן", f"{finding.impact_conversions:.1f}")
    print(f"\n  {finding.impact_basis}")
    if not traffic:
        print("\n  להוספת אומדן קליקים: --traffic עם clicks_28d לדף מ-GSC.")
    print("\n  אחרי הביצוע: --mode verify — מודד שוב ובודק שהטפסים והעיצוב שרדו.")


# ═══════════════════════════════════════════════════════
#  מצב verify
# ═══════════════════════════════════════════════════════

def verify(domain, url, data_path, live, baseline_path) -> int:
    dirs = client_dirs(domain)

    if not baseline_path:
        # הבסיס הוא המדידה הישנה ביותר — זו שלפני העבודה, לא זו שאחריה.
        candidates = sorted(dirs["speed"].glob("psi-*.json"))
        if not candidates:
            log("אין מדידת בסיס לדף הזה — הרץ קודם --mode analyze", "ERR")
            return 1
        baseline_path = str(candidates[0])

    before = pagespeed.load_export(Path(baseline_path))
    if not before:
        log(before.detail, "ERR")
        return 1

    after = gather(url, data_path, live, dirs["speed"])
    if after is None:
        return 1

    comparison = pagespeed.compare(before.data["diagnoses"]["mobile"], after["mobile"])

    banner(f"📊 לפני ואחרי — {domain}")
    kv("בסיס", Path(baseline_path).name)
    rule()

    for movement in comparison["field"]:
        if not movement.readable:
            print(f"  {movement.metric.upper():<5} — אין נתוני שדה")
            continue
        delta = movement.delta or 0
        arrow = "=" if delta == 0 else ("↓" if delta < 0 else "↑")
        print(f"  {movement.metric.upper():<5} {movement.before:>8.2f} → "
              f"{movement.after:>8.2f}  {arrow}")

    lab = comparison["lab_score"]
    if lab.readable:
        print(f"  ציון  {lab.before:>8.0f} → {lab.after:>8.0f}")

    rule()
    log(f"מסקנה: {comparison['verdict']}", "OK")
    print(f"  {comparison['note']}")

    # אופטימיזציה ששוברת טופס היא נזק, לא שיפור.
    log("בודק שהדף עצמו עדיין שלם...", "WAIT")
    try:
        status, html = fetch_page(url)
    except Exception as exc:
        log(f"לא הצלחתי לטעון את הדף: {exc}", "ERR")
        return 1

    baseline = checks.Baseline.capture(html)
    report = checks.run(url, "", baseline, fetch_page)
    rule()
    for check in report.checks:
        if check.name == "הטקסט קיים ב-HTML הגולמי":
            continue                       # לא הוספנו טקסט — לא רלוונטי כאן
        log(f"{check.name}: {check.detail}", "OK" if check.passed else "ERR")

    print()
    return 1 if report.should_roll_back else 0


# ═══════════════════════════════════════════════════════
#  Main
# ═══════════════════════════════════════════════════════

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="WordPress Speed Optimizer")
    parser.add_argument("--client", required=True, help="דומיין הלקוח מ-clients.json")
    parser.add_argument("--mode", choices=["analyze", "verify"], default="analyze",
                        help="שלב בתהליך (ברירת מחדל: analyze)")
    parser.add_argument("--self-check", action="store_true",
                        help="בודק הגדרות ומפתחות לפני הרצה")
    parser.add_argument("--url", help="כתובת הדף לבדיקה")
    parser.add_argument("--live", action="store_true", help="מריץ PageSpeed עכשיו")
    parser.add_argument("--psi-data", help="קובץ תשובת PSI שמור במקום הרצה חיה")
    parser.add_argument("--traffic", help="קובץ עם clicks_28d ו-sessions_28d לדף")
    parser.add_argument("--baseline", help="קובץ המדידה שלפני (למצב verify)")
    parser.add_argument("--dry-run", action="store_true",
                        help="מציג את הדוח בלי לשמור ממצאים")
    args = parser.parse_args(argv)

    secrets.load_env()

    try:
        if args.self_check:
            return self_check(args.client)
        if not args.url:
            log("צריך --url", "ERR")
            return 1
        if args.mode == "analyze":
            return analyze(args.client, args.url, Path(args.psi_data) if args.psi_data
                           else None, args.live, args.traffic, args.dry_run)
        return verify(args.client, args.url,
                      Path(args.psi_data) if args.psi_data else None,
                      args.live, args.baseline)

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
