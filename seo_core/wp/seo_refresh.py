#!/usr/bin/env python3
"""
רענון תוספי ה-SEO אחרי עריכה דרך ה-API
================================================================
כתיבה דרך REST מפעילה את ה-hooks של וורדפרס, אז ה-indexable של Yoast נבנה
מחדש והסייטמאפ מתעדכן. מה שלא קורה הוא הניתוח עצמו: ציון ה-SEO והקריאוּת
מחושבים ב-JavaScript בתוך העורך, נשמרים כ-meta משם, ואף קריאת API לא מריצה
אותם. התוצאה היא ציון ישן שיושב על תוכן חדש.

לכן הסקילים לא מתיימרים לרענן אותו. הם רושמים אילו דפים נגעו, ומפיקים רשימת
עבודה אחת בסוף — עם קישור עריכה ישיר לכל דף — שאפשר לעבור עליה ברצף.

שימוש:
    python -m seo_core.wp.seo_refresh --client example.com
    python -m seo_core.wp.seo_refresh --client example.com --done chg_ab12,chg_cd34
    python -m seo_core.wp.seo_refresh --client example.com --file
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from ..schema import Result
from .seo_meta import FIELDS

Runner = Callable[[list[str]], tuple[int, str, str]]

#: Meta the plugin's own JavaScript writes from inside the editor. Nothing we
#: send over REST recomputes these, so after an API edit they describe the
#: previous version of the page.
BROWSER_SCORED = {
    "yoast": ("_yoast_wpseo_linkdex", "_yoast_wpseo_content_score"),
    "rankmath": ("rank_math_seo_score",),
    "seopress": ("_seopress_analysis_data",),
}

#: Marker written into the change log once a page has been refreshed by hand,
#: so the same page is not listed again on the next run.
REFRESH_NOTE = "yoast_refreshed"


@dataclass
class PendingPage:
    """One page that was edited through the API and still carries an old score."""

    change_id: str
    url: str
    post_id: int | None
    post_type: str
    skill: str
    applied_at: str

    def edit_url(self, base_url: str) -> str:
        if not self.post_id:
            return self.url
        return f"{base_url.rstrip('/')}/wp-admin/post.php?post={self.post_id}&action=edit"


def why_manual(plugin: str) -> str:
    """The reason this cannot be automated, stated once rather than hidden."""
    if plugin == "unknown":
        return "לא זוהה תוסף SEO — אין מה לרענן"
    scored = ", ".join(BROWSER_SCORED.get(plugin, ()))
    return (
        f"{plugin} מחשב את הציון ב-JavaScript בתוך העורך ושומר אותו ב-{scored}. "
        "שום קריאת API לא מריצה את החישוב, אז הציון שנשמר מתאר את הגרסה הקודמת "
        "של הדף. פתיחה ושמירה בעורך היא הדרך היחידה לחשב אותו מחדש."
    )


def elementor_caveat(builder: str, plugin: str) -> str | None:
    """On Elementor the score was never describing the page to begin with.

    Yoast reads `post_content`, and on an Elementor page that is a stale copy
    nobody renders — so the score is wrong before we touch anything.
    """
    if builder != "elementor" or plugin == "unknown":
        return None
    return (
        f"שים לב: בדף Elementor, {plugin} מנתח את post_content — עותק מת שלא "
        "מרונדר. הציון שם לא תיאר את הדף גם לפני השינוי."
    )


# ═══════════════════════════════════════════════════════
#  What is waiting
# ═══════════════════════════════════════════════════════

def pending(changes: list[dict[str, Any]]) -> list[PendingPage]:
    """Pages written through the API that nobody has re-saved yet.

    A rolled-back change is left out: the page is back to the content its score
    already described.
    """
    found: list[PendingPage] = []
    for change in changes:
        if change.get("status") in ("rolled_back", "planned"):
            continue
        if any(REFRESH_NOTE in str(note) for note in change.get("notes", [])):
            continue
        found.append(PendingPage(
            change_id=change.get("change_id", ""),
            url=change.get("url", ""),
            post_id=change.get("post_id"),
            post_type=change.get("post_type", "pages"),
            skill=change.get("skill", ""),
            applied_at=change.get("applied_at") or "",
        ))

    # One line per page, not per change: two edits to the same page are one
    # trip to the editor.
    seen: dict[str, PendingPage] = {}
    for page in found:
        seen.setdefault(page.url, page)
    return sorted(seen.values(), key=lambda p: p.applied_at)


def render(pages: list[PendingPage], base_url: str, plugin: str) -> str:
    """The work list, in the order the pages were changed."""
    if not pages:
        return "אין דפים שממתינים לרענון."

    count = "דף אחד" if len(pages) == 1 else f"{len(pages)} דפים"
    if plugin == "unknown":
        # No access to the site, or no plugin installed. Either way the pages
        # below were still edited, so the list stands — only the reason changes.
        heading = f"# דפים ששונו — {count}"
        reason = (
            "לא זוהה תוסף SEO. אם מותקן אחד, פתח ושמור את הדפים כדי שהניתוח "
            "שלו יחושב מחדש; אם לא — אין כאן מה לעשות."
        )
    else:
        heading = f"# רענון {plugin} — {count}"
        reason = why_manual(plugin)

    lines = [
        heading,
        "",
        reason,
        "",
        "לכל דף: לפתוח, לוודא שהניתוח רץ, לשמור. אין צורך לשנות שום דבר.",
        "",
    ]
    for index, page in enumerate(pages, 1):
        lines.append(f"{index}. {page.url}")
        lines.append(f"   {page.edit_url(base_url)}")
        lines.append(f"   {page.skill} · {page.change_id} · {page.applied_at[:10]}")
        lines.append("")

    lines.append("אחרי שסיימת:")
    lines.append(
        "    python -m seo_core.wp.seo_refresh --client <domain> --done "
        + ",".join(p.change_id for p in pages)
    )
    return "\n".join(lines)


def mark_done(change_ids: list[str], directory: Path) -> Result:
    """Record that these pages were re-saved, so they stop being listed."""
    from ..change_guard import ledger

    stamped = datetime.now(timezone.utc).isoformat()
    result = ledger.add_note(change_ids, f"{REFRESH_NOTE} {stamped}", directory)
    if not result:
        return result
    count = result.data["count"]
    noun = "שינוי אחד סומן" if count == 1 else f"{count} שינויים סומנו"
    return Result.success("marked", f"{noun} כרוענן", path=result.data["path"])


# ═══════════════════════════════════════════════════════
#  WP-CLI, where it exists
# ═══════════════════════════════════════════════════════

def reindex_command(plugin: str, run: Runner) -> Result:
    """Ask the installed plugin whether it can rebuild its index from the CLI.

    This rebuilds indexables and sitemaps — useful after a batch of edits. It
    still does not recompute the editor's analysis scores, and says so.
    """
    namespace = {"yoast": "yoast", "rankmath": "rankmath", "seopress": "seopress"}.get(plugin)
    if not namespace:
        return Result.failure("no_plugin", "לא זוהה תוסף SEO")

    code, out, err = run(["wp", "help", namespace])
    if code != 0:
        return Result.failure(
            "no_wpcli",
            f"אין פקודות WP-CLI ל-{plugin} בהתקנה הזו: {(err or out).strip()[:120]}",
            recoverable=True,
        )

    if "index" not in out:
        return Result.failure(
            "no_index_command",
            f"{plugin} חושף פקודות WP-CLI אבל לא פקודת index",
            recoverable=True,
        )

    return Result.success(
        "available",
        f"wp {namespace} index זמין — בונה מחדש indexables וסייטמאפ. "
        "את ציון הניתוח הוא עדיין לא מחשב.",
        command=f"wp {namespace} index",
    )


# ═══════════════════════════════════════════════════════
#  CLI
# ═══════════════════════════════════════════════════════

def main(argv: list[str] | None = None) -> int:
    from .. import clients, secrets
    from ..change_guard import ledger
    from ..log import banner, kv, log, rule
    from .client import WordPressClient
    from .seo_meta import detect_plugin

    parser = argparse.ArgumentParser(description="SEO plugin refresh list")
    parser.add_argument("--client", required=True, help="דומיין הלקוח מ-clients.json")
    parser.add_argument("--done", help="מזהי שינויים שרועננו, מופרדים בפסיק")
    parser.add_argument("--file", action="store_true",
                        help="שומר את הרשימה לקובץ במקום להדפיס")
    args = parser.parse_args(argv)

    secrets.load_env()
    try:
        client = clients.load(args.client)
    except clients.ClientConfigError as exc:
        log(str(exc), "ERR")
        return 1

    directory = client.data_dir

    if args.done:
        outcome = mark_done([c.strip() for c in args.done.split(",") if c.strip()],
                            directory)
        log(outcome.detail, "OK" if outcome else "ERR")
        return 0 if outcome else 1

    pages = pending(ledger.load(directory))

    plugin = "unknown"
    if client.cms.is_writable and pages:
        try:
            wp = WordPressClient(client.cms.base_url, client.cms.username,
                                 client.secret())
            wp.probe()
            first = pages[0]
            found = wp.get_post(first.post_id, post_type=first.post_type) \
                if first.post_id else None
            if found:
                plugin = detect_plugin(found.data["payload"])
        except Exception as exc:
            log(f"לא הצלחתי לזהות את תוסף ה-SEO: {exc}", "WARN")

    banner(f"🔄 רענון תוסף SEO — {args.client}")
    kv("תוסף", plugin)
    kv("דפים ממתינים", len(pages))
    rule()

    if not pages:
        log("אין דפים שממתינים לרענון", "OK")
        return 0

    text = render(pages, client.cms.base_url, plugin)
    if args.file:
        path = directory / "reports" / "seo_refresh.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        log(f"נשמר: {path}", "OK")
    else:
        print("\n" + text + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
