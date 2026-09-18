#!/usr/bin/env python3
"""
Topic Cluster — מיפוי הנושאים של האתר, ומפה שנשמרת
================================================================
לוקח את השאילתות ש-Search Console כבר מדווח ומארגן אותן לנושאים — לפי הדף
שגוגל בחר, לא לפי המילים. מזהה איזה דף אמור להוביל כל נושא, איזו תת-שאלה
גדלה מעבר לדף שהיא קבורה בו, ואיזה נושא מפוצל בין שני דפים שכל אחד עונה חצי.

המפה **נשמרת**. הרצה חוזרת לאותו לקוח לא מייצרת מפה חדשה בלי זיכרון — היא
אומרת מה חדש, מה זז, ומה נעלם.

שימוש:
    python topic_cluster.py --self-check --client example.com
    python topic_cluster.py --client example.com --queries queries.json
    python topic_cluster.py --client example.com --show
"""

from __future__ import annotations

import argparse
import sys
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
from seo_core.sources import clusters as cl                              # noqa: E402
from seo_core.sources import queries as gsc                              # noqa: E402

MAX_CLUSTERS = 12
MAX_QUERIES  = 5


def self_check(domain: str) -> int:
    banner("🔍 בדיקת תקינות")

    client = clients.load(domain)
    log(f"לקוח {domain} נטען מהרישום", "OK")
    kv("מיקום המפתחות", paths.home())
    for problem in paths.warnings_for():
        log(problem, "WARN")

    stored = cl.load_map(paths.data_dir(domain))
    if stored:
        kv("מפה קיימת", f"ריצה {stored.get('runs')} · "
                        f"{len(stored.get('clusters') or {})} נושאים")
        kv("עודכנה", stored.get("updated_at", "")[:10])
    else:
        log("אין עדיין מפה ללקוח הזה — ההרצה הראשונה תיצור אותה", "INFO")

    rule()
    log("מוכן", "OK")
    return 0


def show(domain: str) -> int:
    stored = cl.load_map(paths.data_dir(domain))
    if not stored:
        log(f"אין מפת נושאים ל-{domain}. הרץ עם --queries כדי לבנות אותה", "ERR")
        return 1

    banner(f"🗺️  מפת נושאים — {domain}")
    kv("ריצות", stored.get("runs"))
    kv("עודכנה", stored.get("updated_at", "")[:10])
    kv("נושאים", len(stored.get("clusters") or {}))
    rule()

    ordered = sorted((stored.get("clusters") or {}).items(),
                     key=lambda kv: kv[1].get("impressions", 0), reverse=True)
    for slug, cluster in ordered[:MAX_CLUSTERS]:
        print(f"\n  📂 {cluster.get('label')}  [{slug}]")
        print(f"       פילר: {cluster.get('pillar')}")
        print(f"       {cluster.get('impressions', 0):,} הופעות · "
              f"{cluster.get('clicks', 0):,} קליקים · "
              f"{count(len(cluster.get('queries') or {}), 'שאילתה אחת', 'שאילתות')}")
        if cluster.get("is_split"):
            print(f"       ⚠️  מפוצל על {len(cluster.get('pages') or {})} דפים")
    print()
    return 0


def run(domain: str, queries_path: Path) -> int:
    dirs = paths.data_dir(domain)

    loaded = gsc.load_export(queries_path)
    if not loaded:
        log(loaded.detail, "ERR")
        return 1
    rows = loaded.data["rows"]
    log(loaded.detail, "OK")

    built = cl.build(rows)
    if not built:
        log("לא נוצר אף נושא — צריך לפחות שלוש שאילתות שגוגל עונה עליהן "
            "באותו דף", "WARN")
        return 0

    curve = gsc.build_curve(rows)
    merged = cl.load_and_merge(dirs, built, domain)
    delta = merged.data["delta"]

    banner(f"🗺️  מפת נושאים — {domain}")
    kv("חלון", loaded.data["window"])
    kv("נושאים", len(built))
    kv("ריצה", merged.data["payload"]["runs"])
    kv("מאז הפעם הקודמת", delta.summary())
    kv("עקומת CTR", curve.describe())
    rule()

    for cluster in built[:MAX_CLUSTERS]:
        print(f"\n  📂 {cluster.label}")
        print(f"       {cluster.summary()}")
        print(f"       פילר: {cluster.pillar}")
        if cluster.is_split:
            for url, impressions in list(cluster.urls.items())[1:3]:
                print(f"       ⚠️  גם {url} — {impressions:,} הופעות")
        for subtopic in cluster.subtopics()[:MAX_QUERIES]:
            print(f"       🌱 “{subtopic.query}” — "
                  f"{subtopic.impressions:,} הופעות במיקום "
                  f"{subtopic.position:.1f}, בלי דף משלה")

    if not delta.first_run and not delta.is_empty:
        rule()
        print("\n  מה השתנה מאז המיפוי הקודם\n")
        for slug in delta.new_clusters:
            print(f"     🆕 נושא חדש: {slug}")
        for slug, queries in delta.new_queries.items():
            print(f"     ➕ {slug}: {', '.join(queries[:4])}")
        for slug, queries in delta.lost_queries.items():
            print(f"     ➖ {slug}: {', '.join(queries[:4])} — כבר לא מופיעות")
        for slug, was, now in delta.moved_pillars:
            print(f"     🔀 {slug}: הפילר עבר מ-{was} ל-{now}")

    findings = cl.to_findings(built, domain, curve)
    rule()
    print(f"\n  📄 המפה: {merged.data['path']}")
    if findings:
        path = save_findings(findings, dirs / "reports" / "cluster_findings.json")
        print(f"  📄 ממצאים: {path}")
        print(f"\n  הם ייכנסו לאודיט: python seo_audit_report.py --client {domain}")
    print()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Topic Cluster")
    parser.add_argument("--client", required=True, help="דומיין הלקוח מ-clients.json")
    parser.add_argument("--self-check", action="store_true",
                        help="בודק הגדרות ומראה אם כבר יש מפה")
    parser.add_argument("--queries", help="קובץ שאילתות מ-GSC")
    parser.add_argument("--show", action="store_true",
                        help="מציג את המפה השמורה בלי לבנות מחדש")
    args = parser.parse_args(argv)

    secrets.load_env()

    try:
        if args.self_check:
            return self_check(args.client)
        if args.show:
            return show(args.client)
        if not args.queries:
            log("צריך --queries כדי לבנות מפה, או --show כדי לראות קיימת", "ERR")
            return 1
        return run(args.client, Path(args.queries))
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
