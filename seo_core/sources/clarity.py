"""
Behaviour data, kept before Microsoft forgets it.
=================================================

Clarity's export API answers for the last three days and no further. Measured,
not assumed: `numOfDays` of 1, 2 and 3 return data and 4 returns HTTP 400. The
daily call budget is small — ten or so per project, after which every request
is 429 "Exceeded daily limit" with no Retry-After. Up to four dimensions are
accepted per call.

So history is something we keep, not something we can ask for. One call a day,
stored as one file per day, and a day already stored is never fetched again:
at roughly ten calls a day, a re-fetch of last Tuesday costs a day that this
Tuesday needed.

A day that is gone is gone. When the machine was off for a week, the next run
recovers the three days still in range and records the rest as a hole. It does
not interpolate, and it does not quietly shorten the window — a chart with an
invented Wednesday is worse than a chart with a gap in it.

A failed snapshot is never filed as a success: the day stays unclaimed, the
failure is written where every skill reads it, and the machine says so.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .. import paths, secrets
from ..schema import Result

API = "https://www.clarity.ms/export-data/api/v1/project-live-insights"

#: The NAME of the environment variable, never its value. Read from os.environ
#: at the moment of use, so a token cannot end up in a log or a report.
TOKEN_NAME = os.environ.get("CLARITY_TOKEN_VAR", "CLARITY_API_TOKEN")

#: Measured against the live API on 2026-09-20: 3 works, 4 returns 400.
MAX_DAYS_BACK = 3

#: Every metric is per-URL, because the join with Search Console is per page.
DIMENSION = "URL"

FAILURES_NAME = "clarity_failures.json"

#: (status, body). Injectable so tests never touch the network.
Fetcher = Callable[[dict[str, Any]], tuple[int, str]]


def requests_fetcher(token: str, timeout: int = 60) -> Fetcher:
    import requests

    def send(params: dict[str, Any]) -> tuple[int, str]:
        reply = requests.get(API, headers={"Authorization": f"Bearer {token}"},
                             params=params, timeout=timeout)
        return reply.status_code, reply.text

    return send


def snapshots_dir(client: str) -> Path:
    return paths.data_dir(client) / "clarity"


def day_file(client: str, day: date) -> Path:
    return snapshots_dir(client) / f"{day.isoformat()}.json"


def settled_days(client: str) -> set[date]:
    """Days there is nothing left to ask about — captured, or known lost.

    A day recorded as a permanent gap counts as settled. Without that, every
    run would spend a call re-asking for a day it had already declared
    unrecoverable, and the budget is about ten a day.
    """
    found: set[date] = set()
    for path in snapshots_dir(client).glob("*.json"):
        try:
            found.add(date.fromisoformat(path.stem))
        except ValueError:
            continue
    return found


def stored_days(client: str) -> set[date]:
    """Days whose behaviour data we actually hold."""
    found: set[date] = set()
    for path in snapshots_dir(client).glob("*.json"):
        try:
            day = date.fromisoformat(path.stem)
        except ValueError:
            continue
        try:
            if json.loads(path.read_text(encoding="utf-8")).get("metrics") is not None:
                found.add(day)
        except json.JSONDecodeError:
            continue
    return found


def reachable_days(today: date) -> list[date]:
    """The days the API can still answer for, newest first.

    Today is excluded: it is still filling up, and storing a partial day as
    final is how a Monday ends up looking like a collapse.
    """
    return [today - timedelta(days=n) for n in range(1, MAX_DAYS_BACK + 1)]


def missing_days(client: str, today: date) -> list[date]:
    settled = settled_days(client)
    return [day for day in reachable_days(today) if day not in settled]


# ═══════════════════════════════════════════════════════
#  Fetching
# ═══════════════════════════════════════════════════════

def fetch(fetcher: Fetcher, days: int = 1) -> Result:
    """One call. The only place a Clarity request is made."""
    if not 1 <= days <= MAX_DAYS_BACK:
        return Result.failure("clarity_window",
                              f"Clarity עונה עד {MAX_DAYS_BACK} ימים אחורה, התבקשו {days}")
    try:
        status, body = fetcher({"numOfDays": days, "dimension1": DIMENSION})
    except Exception as exc:
        return Result.failure("clarity_unreachable", f"Clarity לא נגיש: {exc}")
    if status == 429:
        return Result.failure(
            "clarity_quota",
            "נגמרה המכסה היומית של Clarity — הצילום יידחה למחר, והיום הזה "
            "עלול לרדת מהחלון של שלושת הימים")
    if status == 401:
        return Result.failure("clarity_unauthorized",
                              f"Clarity דחה את הטוקן — {TOKEN_NAME} שגוי או פג")
    if status != 200:
        return Result.failure("clarity_http", f"Clarity החזיר HTTP {status}")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        return Result.failure("clarity_bad_reply", f"תשובה לא קריאה מ-Clarity: {exc}")
    if not isinstance(payload, list):
        return Result.failure("clarity_bad_reply", "Clarity לא החזיר רשימת מדדים")
    return Result.success("fetched", f"{len(payload)} מדדים התקבלו", metrics=payload)


@dataclass
class PageBehaviour:
    """What visitors did on one page, as Clarity counts it."""

    url: str
    sessions: int = 0
    bot_sessions: int = 0
    dead_clicks: int = 0
    rage_clicks: int = 0
    quick_backs: int = 0
    excessive_scroll: int = 0
    script_errors: int = 0
    scroll_depth: float | None = None
    engagement_time: float | None = None

    @property
    def friction(self) -> int:
        """Signals that a visitor could not do what they came to do."""
        return self.dead_clicks + self.rage_clicks + self.quick_backs


_METRIC_FIELDS = {
    "DeadClickCount": "dead_clicks",
    "RageClickCount": "rage_clicks",
    "QuickbackClick": "quick_backs",
    "ExcessiveScroll": "excessive_scroll",
    "ScriptErrorCount": "script_errors",
    "ScrollDepth": "scroll_depth",
    "EngagementTime": "engagement_time",
}


def parse(metrics: list[dict[str, Any]]) -> dict[str, PageBehaviour]:
    """Clarity's metric-per-block shape, turned into one record per page."""
    pages: dict[str, PageBehaviour] = {}

    def page(url: str) -> PageBehaviour:
        return pages.setdefault(url, PageBehaviour(url=url))

    for block in metrics:
        name = block.get("metricName")
        for row in block.get("information") or []:
            url = row.get("Url") or row.get("URL")
            if not url:
                continue
            record = page(url)
            if name == "Traffic":
                record.sessions += int(row.get("totalSessionCount") or 0)
                record.bot_sessions += int(row.get("totalBotSessionCount") or 0)
            elif name in _METRIC_FIELDS:
                raw = row.get("subTotal", row.get("sessionsCount", 0))
                try:
                    value = float(raw)
                except (TypeError, ValueError):
                    continue
                field = _METRIC_FIELDS[name]
                if field in ("scroll_depth", "engagement_time"):
                    setattr(record, field, value)
                else:
                    setattr(record, field, getattr(record, field) + int(value))
    return pages


# ═══════════════════════════════════════════════════════
#  The daily snapshot
# ═══════════════════════════════════════════════════════

def record_failure(client: str, day: date, detail: str) -> Path:
    """Where a failed snapshot is written — and where every skill looks."""
    directory = paths.data_dir(client)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / FAILURES_NAME
    history = []
    if path.exists():
        try:
            history = json.loads(path.read_text(encoding="utf-8")).get("failures", [])
        except json.JSONDecodeError:
            history = []
    history.append({"day": day.isoformat(), "detail": detail,
                    "at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    path.write_text(json.dumps({"failures": history[-50:]}, ensure_ascii=False, indent=1),
                    encoding="utf-8")
    return path


def open_failures(client: str) -> list[dict[str, Any]]:
    """Failures for days that are still not captured. Shown at every run."""
    path = paths.data_dir(client) / FAILURES_NAME
    if not path.exists():
        return []
    try:
        history = json.loads(path.read_text(encoding="utf-8")).get("failures", [])
    except json.JSONDecodeError:
        return []
    have = stored_days(client)
    return [f for f in history if f.get("day") not in {d.isoformat() for d in have}]


def snapshot(client: str, fetcher: Fetcher | None = None,
             today: date | None = None) -> Result:
    """Capture whatever is missing and still in range. Safe to run repeatedly.

    Running twice in one day is a no-op rather than a second call: the budget
    is about ten calls, and a wasted one is a day that cannot be recovered.
    """
    now = today or datetime.now(timezone.utc).date()
    wanted = missing_days(client, now)
    if not wanted:
        return Result.success("up_to_date", "כל הימים בטווח כבר נשמרו",
                              captured=[], gaps=[], calls=0)

    if fetcher is None:
        secrets.load_env()
        token = os.environ.get(TOKEN_NAME, "")
        if not token:
            return Result.failure("clarity_no_token",
                                  f"חסר {TOKEN_NAME} ב-.env — אין צילום התנהגות")
        fetcher = requests_fetcher(token)

    # One call covers the whole window, so the oldest missing day sets its size.
    span = (now - min(wanted)).days
    got = fetch(fetcher, min(span, MAX_DAYS_BACK))
    if not got:
        for day in wanted:
            record_failure(client, day, got.detail)
        return Result.failure(got.code, got.detail, wanted=[d.isoformat() for d in wanted])

    directory = snapshots_dir(client)
    directory.mkdir(parents=True, exist_ok=True)
    # The API reports one window, not one day per call, so the window is filed
    # against its newest missing day and the rest are holes. Saying which is
    # which beats pretending each day was measured on its own.
    newest = max(wanted)
    day_file(client, newest).write_text(json.dumps({
        "day": newest.isoformat(), "covers_days": min(span, MAX_DAYS_BACK),
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "clarity", "metrics": got.data["metrics"],
    }, ensure_ascii=False), encoding="utf-8")

    # The older days of the window are filed as gaps, and filed on disk, so the
    # next run does not spend a call asking for a day already given up on.
    gaps = [d for d in wanted if d != newest]
    for day in gaps:
        day_file(client, day).write_text(json.dumps({
            "day": day.isoformat(), "metrics": None, "gap": True,
            "reason": "כלול בחלון של צילום אחר — אין נתון נפרד ליום הזה",
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }, ensure_ascii=False), encoding="utf-8")
        record_failure(client, day,
                       "היום הזה כבר לא בחלון של Clarity — לא נשמר ולא ישוחזר")
    return Result.success(
        "captured",
        f"נשמר {newest.isoformat()}" + (f", {len(gaps)} ימים אבדו מהחלון" if gaps else ""),
        captured=[newest.isoformat()], gaps=[d.isoformat() for d in gaps], calls=1)


@dataclass
class Quadrant:
    """One page, placed by how it is found against what happens next."""

    url: str
    clicks: int
    impressions: int
    position: float
    sessions: int
    friction: int
    scroll_depth: float | None
    quadrant: str                      # wasted / working / hidden / quiet
    action: str

    @property
    def friction_rate(self) -> float:
        return round(self.friction / self.sessions, 3) if self.sessions else 0.0


#: A page earning this many clicks or more is "found".
FOUND_CLICKS = 5

#: Friction events per session past which the page is failing its visitors.
BAD_EXPERIENCE = 0.25

_ACTIONS = {
    "wasted": "הדף מביא תנועה והמבקרים נתקעים — זו ההזדמנות הגדולה ביותר. "
              "בדוק את ההקלטות ואת הקליקים המתים לפני שנוגעים בתוכן",
    "working": "מביא תנועה והמבקרים מסתדרים — אל תיגע, זה המודל לשאר הדפים",
    "hidden": "החוויה תקינה אבל כמעט אף אחד לא מגיע — בעיית חשיפה, לא בעיית דף",
    "quiet": "מעט תנועה ומעט אותות — אין מספיק נתונים כדי להחליט",
}


def matrix(rows: Any, behaviour: dict[str, PageBehaviour]) -> list[Quadrant]:
    """Join Search Console with Clarity: how they arrived, and what they did.

    Search Console stops at the click. Clarity starts after it. A page that is
    found and frustrates is worth more attention than a page that neither
    ranks nor converts — and neither source can tell those apart alone.

    Pages Clarity never saw are left out rather than scored as friction-free:
    absence of data is not evidence of a good experience.
    """
    by_url: dict[str, list] = {}
    for row in rows:
        by_url.setdefault(row.url, []).append(row)

    out: list[Quadrant] = []
    for url, page_rows in by_url.items():
        seen = behaviour.get(url) or behaviour.get(url.rstrip("/"))
        if seen is None:
            continue
        clicks = sum(r.clicks for r in page_rows)
        impressions = sum(r.impressions for r in page_rows)
        position = (round(sum(r.position * r.impressions for r in page_rows) / impressions, 1)
                    if impressions else 0.0)
        real_sessions = max(0, seen.sessions - seen.bot_sessions)
        rate = seen.friction / real_sessions if real_sessions else 0.0
        found = clicks >= FOUND_CLICKS
        rough = rate >= BAD_EXPERIENCE

        if real_sessions < 5:
            quadrant = "quiet"
        else:
            quadrant = ("wasted" if found and rough else
                        "working" if found else
                        "hidden" if not rough else "quiet")
        out.append(Quadrant(url=url, clicks=clicks, impressions=impressions,
                            position=position, sessions=real_sessions,
                            friction=seen.friction, scroll_depth=seen.scroll_depth,
                            quadrant=quadrant, action=_ACTIONS[quadrant]))
    order = {"wasted": 0, "working": 1, "hidden": 2, "quiet": 3}
    return sorted(out, key=lambda q: (order[q.quadrant], -q.clicks))


def notify(title: str, body: str) -> bool:
    """Tell the person at the machine. Best effort — never fails a run.

    A snapshot that fails silently is the one case this module cannot recover
    from: the day drops out of the window while nobody knows.
    """
    if os.name != "nt":
        return False
    try:
        import subprocess

        script = (
            "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications,"
            " ContentType=WindowsRuntime] > $null;"
            "$t=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent(1);"
            f"$t.GetElementsByTagName('text').Item(0).AppendChild($t.CreateTextNode('{title}')) > $null;"
            f"$t.GetElementsByTagName('text').Item(1).AppendChild($t.CreateTextNode('{body}')) > $null;"
            "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('SEO')"
            ".Show([Windows.UI.Notifications.ToastNotification]::new($t))"
        )
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                       capture_output=True, timeout=30, check=False)
        return True
    except Exception:
        return False


def main(argv: list[str] | None = None) -> int:
    """`python -m seo_core.sources.clarity --client x` — what the timer runs."""
    import argparse

    parser = argparse.ArgumentParser(description="Clarity daily snapshot")
    parser.add_argument("--client", required=True, help="דומיין הלקוח מ-clients.json")
    parser.add_argument("--quiet", action="store_true", help="בלי התראה על כישלון")
    args = parser.parse_args(argv)

    result = snapshot(args.client)
    print(result.detail)
    if result and result.data.get("gaps"):
        print("ימים שאבדו מהחלון: " + ", ".join(result.data["gaps"]))
    if not result and not args.quiet:
        notify("צילום Clarity נכשל", f"{args.client}: {result.detail[:120]}")
    return 0 if result else 1


def load_days(client: str, since: date | None = None) -> dict[date, dict[str, PageBehaviour]]:
    """Every stored day, parsed. The history the API will not give us."""
    out: dict[date, dict[str, PageBehaviour]] = {}
    for path in sorted(snapshots_dir(client).glob("*.json")):
        try:
            day = date.fromisoformat(path.stem)
        except ValueError:
            continue
        if since and day < since:
            continue
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if stored.get("metrics") is None:
            continue                      # a recorded gap holds no numbers
        out[day] = parse(stored["metrics"])
    return out


if __name__ == "__main__":
    raise SystemExit(main())
