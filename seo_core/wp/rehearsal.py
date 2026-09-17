#!/usr/bin/env python3
"""
חזרה גנרלית: להוכיח ששחזור עובד לפני שנוגעים בפרודקשן
========================================================
כותב פסקת סימון לדף ב-Staging, מוודא שהיא נכתבה ונראית, משחזר מהגיבוי,
ומוודא שהדף חזר בדיוק למצבו הקודם — לפי hash, לא לפי תחושה.

לקוח שלא עבר את החזרה הזו לא אמור לקבל כתיבה אוטומטית לאתר החי, כי אף אחד
עוד לא הוכיח שאפשר לבטל אותה.

שימוש:
    python -m seo_core.wp.rehearsal --client example.com
    python -m seo_core.wp.rehearsal --client example.com --url https://staging.example.com/page
    python -m seo_core.wp.rehearsal --client example.com --status
"""

from __future__ import annotations

import argparse
import json
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from ..schema import Result
from . import backup as wp_backup
from . import content as wp_content

REHEARSAL_FILE     = "rehearsal.json"
REHEARSAL_TTL_DAYS = 180
MARKER_PREFIX      = "seo-toolkit restore rehearsal"

Fetcher = Callable[[str], tuple[int, str]]


@dataclass
class Step:
    name: str
    ok: bool
    detail: str


@dataclass
class Rehearsal:
    """The record of one drill. Only a fully clean run counts as a pass."""

    client: str
    target_url: str
    builder: str
    marker: str
    steps: list[Step] = field(default_factory=list)
    passed: bool = False
    completed_at: str = ""

    @property
    def failed_step(self) -> Step | None:
        return next((s for s in self.steps if not s.ok), None)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def summary(self) -> str:
        if self.passed:
            return f"כתיבה ושחזור אומתו על {self.target_url}"
        failed = self.failed_step
        return f"נכשל בשלב {failed.name}: {failed.detail}" if failed else "לא הושלם"


def marker_text(marker_id: str) -> str:
    """Deliberately conspicuous, in English, and unique per run.

    If a rehearsal ever fails to clean up after itself, this is the string that
    makes the leftover findable in one search.
    """
    return (
        f"{MARKER_PREFIX} {marker_id} — this paragraph is written and removed "
        "automatically to prove that a restore works. If you are reading it on "
        "a live page, the restore failed and it is safe to delete."
    )


# ═══════════════════════════════════════════════════════
#  The drill
# ═══════════════════════════════════════════════════════

def run(
    wp: Any,
    url: str,
    client: str,
    directory: Path,
    heading: str | None = None,
    fetch: Fetcher | None = None,
) -> Result:
    """Write, verify, restore, verify again. Returns the recorded rehearsal."""
    marker_id = uuid.uuid4().hex[:8]
    drill = Rehearsal(client=client, target_url=url, builder="", marker=marker_id)

    def fail(name: str, detail: str, *, recoverable: bool = True) -> Result:
        drill.steps.append(Step(name, False, detail))
        drill.completed_at = datetime.now(timezone.utc).isoformat()
        record(drill, directory)
        return Result.failure(
            "rehearsal_failed", detail, recoverable=recoverable, rehearsal=drill
        )

    # ── 1. יכולות ─────────────────────────────────────
    caps = wp.probe()
    if not caps.can_write("page"):
        return fail("probe", f"אין הרשאת כתיבה: {caps.summary()}")
    drill.steps.append(Step("probe", True, caps.summary()))

    # ── 2. טעינת הדף ──────────────────────────────────
    located = _locate(wp, url)
    if not located:
        return fail("locate", located.detail)
    post_type = located.data["post_type"]
    drill.target_url = located.data.get("url", url)

    loaded = wp_content.load(located.data["payload"], post_type)
    if not loaded:
        return fail("load", loaded.detail)
    page = loaded.data["content"]
    drill.builder = page.builder
    original_hash = page.hash
    drill.steps.append(Step("load", True, f"{page.builder} · hash {original_hash}"))

    # ── 3. נקודת הכנסה ────────────────────────────────
    headings = wp_content.list_headings(page)
    target_heading = heading or (headings[0] if headings else None)
    if not target_heading:
        return fail(
            "heading",
            "לא נמצאה אף כותרת בדף — בחר דף אחר ל-Staging או ציין --heading",
        )
    drill.steps.append(Step("heading", True, f"{target_heading!r}"))

    composed = wp_content.insert_paragraph_after_heading(
        page, target_heading, marker_text(marker_id)
    )
    if not composed:
        return fail("compose", composed.detail)
    drill.steps.append(Step("compose", True, composed.detail))

    # ── 4. גיבוי מאומת ────────────────────────────────
    snapshot = wp_backup.create(
        page, url, f"rehearsal_{marker_id}",
        meta_keys=list(composed.data["payload"].get("meta", {})),
    )
    verified = wp_backup.verify(snapshot)
    if not verified:
        return fail("backup", verified.detail, recoverable=False)
    wp_backup.save(snapshot, directory / "backups")
    drill.steps.append(Step("backup", True, "הגיבוי אומת כשחזורי"))

    # ── 5. כתיבה ──────────────────────────────────────
    payload = composed.data["payload"]
    written = wp.update_post(
        page.post_id, post_type=post_type,
        content=payload.get("content"), meta=payload.get("meta"),
    )
    if not written:
        return fail("write", f"הכתיבה נכשלה: {written.detail}")
    drill.steps.append(Step("write", True, "נכתב"))

    # ── 6. האם הכתיבה באמת נחתה ───────────────────────
    landed = _reread(wp, page.post_id, post_type)
    if not landed:
        return fail("confirm_write", landed.detail)
    after = landed.data["content"]
    if after.hash == original_hash:
        return fail(
            "confirm_write",
            "הכתיבה הוחזרה כהצלחה אבל התוכן לא השתנה — "
            "זה בדיוק המקרה של meta שלא רשום ב-REST ונזרק בשקט",
        )
    drill.steps.append(Step("confirm_write", True, f"hash חדש {after.hash}"))

    if fetch is not None:
        rendered = _marker_visible(fetch, url, marker_id)
        drill.steps.append(rendered)
        if not rendered.ok:
            # לא עוצרים — עדיין חייבים לשחזר. הכישלון יירשם בסוף.
            pass

    # ── 7. שחזור ──────────────────────────────────────
    restored = wp_backup.restore(wp, snapshot)
    if not restored:
        return fail(
            "restore",
            f"{restored.detail}\n"
            f"הדף {url} נשאר עם פסקת הסימון {marker_id}. "
            "אל תריץ שוב — תקן ידנית ובדוק מה חסם את השחזור.",
            recoverable=False,
        )
    drill.steps.append(Step("restore", True, restored.detail))

    # ── 8. האם הדף חזר בדיוק ──────────────────────────
    final = _reread(wp, page.post_id, post_type)
    if not final:
        return fail("confirm_restore", final.detail, recoverable=False)
    if final.data["content"].hash != original_hash:
        return fail(
            "confirm_restore",
            f"אחרי השחזור ה-hash הוא {final.data['content'].hash} "
            f"ולא {original_hash} — הדף לא חזר למצבו המקורי. בדוק ידנית.",
            recoverable=False,
        )
    drill.steps.append(Step("confirm_restore", True, f"חזר ל-hash {original_hash}"))

    drill.passed = all(step.ok for step in drill.steps)
    drill.completed_at = datetime.now(timezone.utc).isoformat()
    record(drill, directory)

    if not drill.passed:
        return Result.failure(
            "rehearsal_incomplete", drill.summary(), recoverable=True, rehearsal=drill
        )
    return Result.success("rehearsal_passed", drill.summary(), rehearsal=drill)


def _locate(wp: Any, url: str) -> Result:
    """Find the page to rehearse on.

    A bare site root has no slug to resolve, and a WordPress front page may not
    be a page at all, so rather than guessing at the homepage the drill asks
    for any published page and takes the first one carrying a heading. Any page
    proves the same thing: that a write can be made and undone.
    """
    from urllib.parse import urlparse

    if urlparse(url).path.strip("/"):
        found = wp.find_by_url(url)
        if found:
            found.data.setdefault("url", url)
        return found

    listed = wp.list_posts("pages")
    if not listed:
        return Result.failure(
            "no_target",
            f"{listed.detail}. ציין דף מפורש עם --url, למשל "
            f"{url.rstrip('/')}/sample-page",
        )

    for item in listed.data["items"]:
        loaded = wp_content.load(item, "pages")
        if loaded and wp_content.list_headings(loaded.data["content"]):
            return Result.success(
                "found", "נבחר דף לחזרה",
                payload=item, post_type="pages",
                url=item.get("link") or url,
            )

    return Result.failure(
        "no_target",
        "אף דף באתר לא מכיל כותרת — צור דף עם כותרת H2, או ציין --url ו---heading",
    )


def _reread(wp: Any, post_id: int, post_type: str) -> Result:
    fresh = wp.get_post(post_id, post_type=post_type)
    if not fresh:
        return Result.failure("reread_failed", f"לא הצלחתי לקרוא את הדף שוב: {fresh.detail}")
    return wp_content.load(fresh.data["payload"], post_type)


def _marker_visible(fetch: Fetcher, url: str, marker_id: str) -> Step:
    """Did the paragraph reach the HTML a crawler receives?"""
    try:
        status, html = fetch(url)
    except Exception as exc:
        return Step("rendered", False, f"שגיאה בטעינת הדף: {exc}")
    if status != 200:
        return Step("rendered", False, f"הדף החזיר {status}")
    present = marker_id in html
    return Step(
        "rendered", present,
        "הפסקה נמצאה ב-HTML הגולמי" if present else
        "הפסקה לא נמצאה ב-HTML — ייתכן cache או שהעריכה לא נכנסה לתבנית",
    )


# ═══════════════════════════════════════════════════════
#  Persistence
# ═══════════════════════════════════════════════════════

def record(drill: Rehearsal, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / REHEARSAL_FILE
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(drill.to_dict(), fh, ensure_ascii=False, indent=2)
    return path


def status(directory: Path) -> Result:
    path = directory / REHEARSAL_FILE
    if not path.exists():
        return Result.failure(
            "never_rehearsed",
            "הלקוח הזה עוד לא עבר חזרה גנרלית של כתיבה ושחזור. "
            "הרץ: python -m seo_core.wp.rehearsal --client <domain>",
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return Result.failure("rehearsal_corrupt", f"קובץ החזרה פגום: {exc}")

    drill = Rehearsal(
        client=raw.get("client", ""), target_url=raw.get("target_url", ""),
        builder=raw.get("builder", ""), marker=raw.get("marker", ""),
        steps=[Step(**s) for s in raw.get("steps", [])],
        passed=bool(raw.get("passed")), completed_at=raw.get("completed_at", ""),
    )
    if not drill.passed:
        return Result.failure("rehearsal_failed", drill.summary(), rehearsal=drill)
    return Result.success("rehearsed", drill.summary(), rehearsal=drill)


def require(directory: Path, now: datetime | None = None) -> Result:
    """Gate a first production write on a passing, recent rehearsal."""
    current = status(directory)
    if not current:
        return current

    drill = current.data["rehearsal"]
    try:
        completed = datetime.fromisoformat(drill.completed_at)
    except (TypeError, ValueError):
        return Result.failure("rehearsal_undated", "לחזרה הגנרלית אין תאריך תקין")

    moment = now or datetime.now(timezone.utc)
    if completed.tzinfo is None:
        completed = completed.replace(tzinfo=timezone.utc)
    age = moment - completed
    if age > timedelta(days=REHEARSAL_TTL_DAYS):
        return Result.failure(
            "rehearsal_stale",
            f"החזרה הגנרלית בוצעה לפני {age.days} יום. "
            f"תוספים והרשאות משתנים — הרץ אותה שוב לפני כתיבה.",
            rehearsal=drill,
        )
    return Result.success("rehearsed", f"{drill.summary()} (לפני {age.days} יום)", rehearsal=drill)


# ═══════════════════════════════════════════════════════
#  CLI
# ═══════════════════════════════════════════════════════

def main(argv: list[str] | None = None) -> int:
    from .. import clients, paths, secrets
    from ..log import banner, kv, log, rule
    from .client import WordPressClient

    parser = argparse.ArgumentParser(description="Staging write/restore rehearsal")
    parser.add_argument("--client", required=True, help="דומיין הלקוח מ-clients.json")
    parser.add_argument("--url", help="כתובת הדף לחזרה (ברירת מחדל: דף כלשהו עם כותרת)")
    parser.add_argument("--heading", help="כותרת שאחריה תיכנס פסקת הסימון")
    parser.add_argument("--status", action="store_true", help="מציג את תוצאת החזרה האחרונה")
    parser.add_argument("--on-production", action="store_true",
                        help="מריץ על אתר הפרודקשן כשאין Staging — דורש אישור מוקלד")
    args = parser.parse_args(argv)

    secrets.load_env()
    try:
        client = clients.load(args.client)
    except clients.ClientConfigError as exc:
        log(str(exc), "ERR")
        return 1

    directory = client.data_dir

    if args.status:
        current = status(directory)
        banner(f"🧪 חזרה גנרלית — {args.client}")
        log(current.detail, "OK" if current else "WARN")
        return 0 if current else 1

    if client.cms.type != "wordpress":
        log(f"CMS מסוג {client.cms.type} — אין מה לחזור עליו", "ERR")
        return 1

    base = client.cms.staging_url
    if not base:
        if not args.on_production:
            log(
                "ללקוח הזה אין staging_url. חזרה גנרלית כותבת פסקה לדף חי ומוחקת "
                "אותה — הוסף staging_url ל-clients.json, או הרץ עם --on-production",
                "ERR",
            )
            return 1
        base = client.cms.base_url
        print(f"\n  החזרה תרוץ על הפרודקשן: {base}")
        if input("  הקלד 'כן' כדי להמשיך: ").strip() != "כן":
            log("בוטל", "SKIP")
            return 1

    url = args.url or base
    wp = WordPressClient(
        base_url=base, username=client.cms.username, app_password=client.secret()
    )

    def fetch(target: str) -> tuple[int, str]:
        import requests
        response = requests.get(target, timeout=20)
        return response.status_code, response.text

    banner(f"🧪 חזרה גנרלית — {args.client}")
    kv("אתר", base)
    kv("דף", args.url or "ייבחר אוטומטית")
    kv("מיקום המפתחות", paths.home())
    for problem in paths.warnings_for():
        log(problem, "WARN")
    rule()

    outcome = run(wp, url, args.client, directory, heading=args.heading, fetch=fetch)
    drill = outcome.data.get("rehearsal")

    if drill:
        for step in drill.steps:
            log(f"{step.name}: {step.detail}", "OK" if step.ok else "ERR")
    rule()

    if outcome:
        log(outcome.detail, "OK")
        log("הלקוח מאושר לכתיבה אוטומטית", "OK")
        return 0

    log(outcome.detail, "ERR")
    if not outcome.recoverable:
        log("עצור והתערב ידנית — אל תריץ שוב", "ERR")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
