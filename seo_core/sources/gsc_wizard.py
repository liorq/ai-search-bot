"""
Search Console rows, fetched from GSC Wizard — so a skill needs only --client.
============================================================================

Until this module existed, six skills could not start: each asked for an export
file that nothing produced. This fetches the rows and writes them in the shape
`queries.load_export()` already reads, so nothing downstream changes.

It talks to GSC Wizard's MCP endpoint over plain HTTP with the read-only API
key from the .env. Three things were learned by running it, not by reading
about it, and each one is load-bearing:

    * The reply is an event stream with no charset. `resp.text` guesses Latin-1
      and `str.splitlines()` breaks on U+2028 inside a query string — so the
      body is decoded as UTF-8 and split on "\\n" only.
    * Paging is `startRow`. The server's own note says "offset", which it
      silently ignores and answers with page one again.
    * `ctr` comes back as a percentage. It is never passed on: every consumer
      derives CTR from clicks and impressions, as a fraction.

What the export cannot know, it says so. Search Console hides rare queries, so
the rows never add up to the property's totals; the share that is missing is
measured on every fetch and written next to the rows.

Two more exports obey the same rules: a pair of comparable windows in the
shape `gsc_source.load_export` reads, and Google's confirmed ranking updates in
the shape `updates.load_updates` reads. A comparison with nothing to compare
against is refused, not written — an empty earlier window would read
downstream as "nothing declined", which is a blocked check passing for a clean
one.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from .. import secrets
from ..schema import Result

if TYPE_CHECKING:                       # annotations only: a skill imports both
    from ..clients import Client        # modules, and neither should need the other

ENDPOINT = "https://mcp.gscwizard.com/mcp"
KEY_NAME = "GSC_WIZARD_API_KEY"
PROTOCOL_VERSION = "2025-03-26"

#: The most rows one call may ask for. A property that fills a page exactly is
#: not assumed to be cut off — the next page is requested, and only a page that
#: is still full when the guard trips is reported as truncated.
PAGE_SIZE = 25000
MAX_PAGES = 20

#: "The same window last year" ends this many days back. 364, not 365: fifty-two
#: whole weeks put every weekday under the same weekday, and a local business's
#: Saturday is not comparable with its Friday.
YEAR_BACK_DAYS = 364
COMPARISONS = ("year", "previous")

#: Seen answering on 2026-09-20: each entry is {id, name, beginISO, endISO,
#: severity, uri}. The other spellings stay accepted, and a reply that matches
#: none of them fails with what it did contain — so a renamed field is one look
#: away from fixed instead of a silent empty list.
UPDATE_LIST_KEYS = ("updates", "rows", "items", "incidents")
UPDATE_NAME_KEYS = ("name", "title")
UPDATE_START_KEYS = ("beginISO", "start", "startDate", "begin", "date")
UPDATE_END_KEYS = ("endISO", "end", "endDate", "resolved")

#: (status, headers, body-as-utf8). Injectable so tests never touch the network.
Transport = Callable[[dict[str, Any], dict[str, str]], tuple[int, dict[str, str], str]]


def requests_transport(timeout: int = 180) -> Transport:
    import requests

    session = requests.Session()

    def send(payload: dict[str, Any], headers: dict[str, str]) -> tuple[int, dict[str, str], str]:
        resp = session.post(ENDPOINT, headers=headers, json=payload, timeout=timeout)
        return resp.status_code, {k.lower(): v for k, v in resp.headers.items()}, \
            resp.content.decode("utf-8", errors="replace")

    return send


def _parse_body(headers: dict[str, str], body: str) -> dict[str, Any] | None:
    if "text/event-stream" in headers.get("content-type", ""):
        frames = [line[5:].strip() for line in body.split("\n") if line.startswith("data:")]
        return json.loads(frames[-1]) if frames else None
    return json.loads(body) if body.strip() else None


@dataclass
class Session:
    """One authenticated conversation with the server."""

    transport: Transport
    headers: dict[str, str]
    calls: int = 0

    def call(self, tool: str, arguments: dict[str, Any]) -> Result:
        self.calls += 1
        payload = {"jsonrpc": "2.0", "id": self.calls + 1, "method": "tools/call",
                   "params": {"name": tool, "arguments": arguments}}
        try:
            status, headers, body = self.transport(payload, self.headers)
        except Exception as exc:                      # network failure, not a bug
            return Result.failure("gsc_wizard_unreachable", f"GSC Wizard לא נגיש: {exc}")
        if status == 429:
            return Result.failure(
                "gsc_wizard_rate_limited",
                "GSC Wizard החזיר 429 — חריגה ממכסת הקריאות. נסה שוב בעוד כמה דקות",
                retry_after=headers.get("retry-after"))
        if status != 200:
            return Result.failure("gsc_wizard_http", f"GSC Wizard החזיר HTTP {status} בקריאה ל-{tool}")
        try:
            result = (_parse_body(headers, body) or {}).get("result") or {}
        except json.JSONDecodeError as exc:
            return Result.failure("gsc_wizard_bad_reply", f"תשובה לא קריאה מ-{tool}: {exc}")
        text = next((c.get("text", "") for c in result.get("content", [])
                     if c.get("type") == "text"), "")
        if result.get("isError"):
            return Result.failure("gsc_wizard_tool_error", f"{tool}: {text[:300]}")
        data = result.get("structuredContent")
        if data is None:
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                return Result.failure("gsc_wizard_bad_reply", f"{tool} לא החזיר JSON")
        return Result.success("called", tool, payload=data)


def open_session(transport: Transport | None = None) -> Result:
    """Authenticate. The key is read here and never logged or returned."""
    secrets.load_env()
    key = os.environ.get(KEY_NAME, "")
    if not key:
        return Result.failure(
            "gsc_wizard_no_key",
            f"חסר {KEY_NAME} ב-.env — צור מפתח לקריאה בלבד ב-tool.gscwizard.com/account/api-keys")
    send = transport or requests_transport()
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json",
               "Accept": "application/json, text/event-stream"}
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": PROTOCOL_VERSION, "capabilities": {},
        "clientInfo": {"name": "seo-core", "version": "1"}}}
    try:
        status, reply_headers, body = send(init, headers)
    except Exception as exc:
        return Result.failure("gsc_wizard_unreachable", f"GSC Wizard לא נגיש: {exc}")
    if status == 401:
        return Result.failure(
            "gsc_wizard_key_rejected",
            f"GSC Wizard דחה את המפתח ({body[:120].strip()}). "
            "מפתח שבוטל לא חוזר לעבוד — צריך מפתח חדש ב-.env")
    if status != 200:
        return Result.failure("gsc_wizard_http", f"GSC Wizard החזיר HTTP {status} בפתיחת החיבור")
    if reply_headers.get("mcp-session-id"):
        headers["Mcp-Session-Id"] = reply_headers["mcp-session-id"]
    send({"jsonrpc": "2.0", "method": "notifications/initialized"}, headers)
    return Result.success("connected", "מחובר ל-GSC Wizard", session=Session(send, headers))


# ═══════════════════════════════════════════════════════
#  Windows — explicit dates, so a run can be repeated
# ═══════════════════════════════════════════════════════

def settled_window(session: Session, property_url: str, days: int = 28) -> Result:
    """The last `days` days Search Console has finished counting.

    Asked of the server rather than computed from today's date: the data lags
    two or three days, and the lag is not constant.
    """
    probe = session.call("query_search_analytics",
                         {"siteUrl": property_url, "dimensions": [], "rowLimit": 1})
    if not probe:
        return probe
    data = probe.data["payload"]
    settled = data.get("settledThrough") or data.get("endDate")
    if not settled:
        return Result.failure("gsc_wizard_no_freshness", "השרת לא דיווח עד איזה יום הנתונים סגורים")
    end = date.fromisoformat(settled)
    start = end - timedelta(days=days - 1)
    return Result.success("window", f"{start}..{end}", start=start.isoformat(),
                          end=end.isoformat(), settled_through=settled,
                          maturity=data.get("dataMaturity"))


def comparison_window(start: str, end: str, compare: str = "year") -> tuple[str, str]:
    """The window the current one is held against — always the same length.

    "year" ends exactly `YEAR_BACK_DAYS` before the current end, so the two
    windows hold the same weekdays. "previous" is the stretch immediately
    before, for a property too young to have a last year. Anything else is
    refused by the caller before a request is spent on it.
    """
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    prior_end = (last - timedelta(days=YEAR_BACK_DAYS) if compare == "year"
                 else first - timedelta(days=1))
    return (prior_end - (last - first)).isoformat(), prior_end.isoformat()


# ═══════════════════════════════════════════════════════
#  Rows
# ═══════════════════════════════════════════════════════

def _metrics(entry: dict[str, Any]) -> dict[str, Any]:
    """The three numbers a row keeps. `ctr` is left behind on purpose."""
    return {"clicks": int(entry.get("clicks") or 0),
            "impressions": int(entry.get("impressions") or 0),
            "position": float(entry.get("position") or 0)}


def _paged(session: Session, arguments: dict[str, Any],
           shape: Callable[[dict[str, Any]], dict[str, Any] | None]) -> Result:
    """Follow `startRow` until the server says there is nothing more.

    One loop for every row pull, so `truncated` means the same thing wherever
    it is read: False only when the server said `hasMore: false`, "unknown"
    when it never said either way, True when the guard tripped with more still
    waiting. `shape` answers None for a row it cannot use, and those are
    counted rather than skipped.
    """
    rows: list[dict[str, Any]] = []
    dropped = 0
    sources: set[str] = set()
    truncated: bool | str = "unknown"
    offset = 0
    for _ in range(MAX_PAGES):
        page = session.call("query_search_analytics",
                            {**arguments, "rowLimit": PAGE_SIZE, "startRow": offset})
        if not page:
            return page
        data = page.data["payload"]
        sources.add(str(data.get("dataSource", "unknown")))
        for entry in data.get("rows") or []:
            row = shape(entry)
            if row is None:
                dropped += 1
            else:
                rows.append(row)
        pagination = data.get("pagination") or {}
        if "hasMore" not in pagination:
            break                                  # completeness stays "unknown"
        if not pagination["hasMore"]:
            truncated = False
            break
        truncated = True
        offset = int(pagination.get("nextOffset") or offset + PAGE_SIZE)
    return Result.success("paged", f"{len(rows)} שורות", rows=rows, dropped=dropped,
                          truncated=truncated, data_sources=sorted(sources))


def fetch_query_pages(session: Session, property_url: str, start: str, end: str,
                      search_type: str = "web") -> Result:
    """Every query+page pair in the window — both dimensions, never merged.

    Cannibalisation is two of our URLs on one query; a query-only pull would
    add them together and hide exactly the thing being looked for.
    """
    def shape(entry: dict[str, Any]) -> dict[str, Any] | None:
        keys = entry.get("keys") or []
        if len(keys) != 2 or not keys[0] or not keys[1]:
            return None
        return {"query": keys[0], "url": keys[1], **_metrics(entry)}

    pulled = _paged(session, {
        "siteUrl": property_url, "startDate": start, "endDate": end,
        "dimensions": ["query", "page"], "searchType": search_type}, shape)
    if not pulled:
        return pulled
    return Result.success("rows", f"{len(pulled.data['rows'])} שורות שאילתה+דף", **pulled.data)


def fetch_pages(session: Session, property_url: str, start: str, end: str,
                search_type: str = "web") -> Result:
    """Every page in the window, with the numbers Search Console gives the page itself.

    A page's query rows do not add up to the page: the rare queries Search
    Console hides are missing from the rows and present in the page's own
    total. A decline measured on summed rows is partly a change in how much was
    hidden, so two windows are compared on these numbers instead.
    """
    def shape(entry: dict[str, Any]) -> dict[str, Any] | None:
        keys = entry.get("keys") or []
        if len(keys) != 1 or not keys[0]:
            return None
        return {"url": keys[0], **_metrics(entry)}

    pulled = _paged(session, {
        "siteUrl": property_url, "startDate": start, "endDate": end,
        "dimensions": ["page"], "searchType": search_type}, shape)
    if not pulled:
        return pulled
    return Result.success("pages", f"{len(pulled.data['rows'])} דפים", **pulled.data)


def property_totals(session: Session, property_url: str, start: str, end: str,
                    search_type: str = "web") -> Result:
    """What the property earned in total — the yardstick the rows are held to."""
    reply = session.call("query_search_analytics", {
        "siteUrl": property_url, "startDate": start, "endDate": end,
        "dimensions": [], "searchType": search_type})
    if not reply:
        return reply
    rows = reply.data["payload"].get("rows") or []
    if not rows:
        return Result.success("totals", "אין נתונים לנכס בחלון", clicks=0, impressions=0)
    return Result.success("totals", "סך הנכס", clicks=int(rows[0].get("clicks") or 0),
                          impressions=int(rows[0].get("impressions") or 0))


def visible_totals(session: Session, property_url: str, start: str, end: str,
                   search_type: str = "web") -> Result:
    """What the *named* queries add up to, counted the way the property total is.

    The query+page rows cannot be held against the property total: they count
    an impression once per URL shown, the total counts it once. Measured on a
    real site, that made 24% hidden impressions look like 5%. Summing the
    query-only rows is the like-for-like comparison.
    """
    clicks = impressions = offset = 0
    for _ in range(MAX_PAGES):
        page = session.call("query_search_analytics", {
            "siteUrl": property_url, "startDate": start, "endDate": end,
            "dimensions": ["query"], "searchType": search_type,
            "rowLimit": PAGE_SIZE, "startRow": offset})
        if not page:
            return page
        data = page.data["payload"]
        for entry in data.get("rows") or []:
            clicks += int(entry.get("clicks") or 0)
            impressions += int(entry.get("impressions") or 0)
        pagination = data.get("pagination") or {}
        if not pagination.get("hasMore"):
            break
        offset = int(pagination.get("nextOffset") or offset + PAGE_SIZE)
    return Result.success("visible", "סך השאילתות הגלויות", clicks=clicks, impressions=impressions)


def completeness(rows: list[dict[str, Any]], totals: dict[str, int], visible: dict[str, int],
                 dropped: int, truncated: bool | str) -> dict[str, Any]:
    """How much of the property the rows actually cover.

    Search Console withholds rare queries — a privacy rule, not a paging limit:
    splitting the window by day returns exactly the same clicks. So some share
    is always missing, and two shares are reported because they tell different
    stories: a site can show most of its impressions and hide most of its clicks.
    """
    def hidden(seen: int, total: int) -> float | None:
        return round(max(0.0, 1 - seen / total), 4) if total else None

    return {
        "rows_total": len(rows) + dropped,
        "rows_dropped": dropped,
        "dropped_reasons": {"missing_query_or_page": dropped} if dropped else {},
        "impressions_total": totals.get("impressions"),
        "impressions_in_queries": visible.get("impressions"),
        "clicks_total": totals.get("clicks"),
        "clicks_in_queries": visible.get("clicks"),
        "anonymised_share": hidden(visible.get("impressions", 0), totals.get("impressions", 0)),
        "anonymised_click_share": hidden(visible.get("clicks", 0), totals.get("clicks", 0)),
        "hidden_because": "שאילתות נדירות ש-Search Console לא חושף (פרטיות) — לא מגבלת דפדוף",
        "truncated": truncated,
    }


# ═══════════════════════════════════════════════════════
#  The export a skill reads
# ═══════════════════════════════════════════════════════

def export_queries(property_url: str, out_dir: Path, days: int = 28,
                   search_type: str = "web", transport: Transport | None = None,
                   window_dates: tuple[str, str] | None = None) -> Result:
    """Fetch the settled window and write it where `queries.load_export` reads.

    `window_dates` overrides the settled window for a caller that needs a
    specific span — a before/after pair around a dated event, say — and the
    freshness of the data is still reported, because a window that runs past
    the settled day is short of counted days, not short of traffic.
    """
    if not property_url:
        return Result.failure("gsc_wizard_no_property",
                              "ללקוח לא מוגדר gsc_property ב-clients.json — אין מה למשוך")
    opened = open_session(transport)
    if not opened:
        return opened
    session: Session = opened.data["session"]

    window = settled_window(session, property_url, days)
    if not window:
        return window
    start, end = window_dates or (window.data["start"], window.data["end"])

    fetched = fetch_query_pages(session, property_url, start, end, search_type)
    if not fetched:
        return fetched
    totals = property_totals(session, property_url, start, end, search_type)
    if not totals:
        return totals
    visible = visible_totals(session, property_url, start, end, search_type)
    if not visible:
        return visible

    export = {
        "property": property_url,
        "range": f"{start}..{end}",
        "rows": fetched.data["rows"],
        "source": "gsc_wizard",
        "data_sources": fetched.data["data_sources"],
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "search_type": search_type,
        "window": {"start": start, "end": end},
        "comparison_window": None,
        "freshness": {"settled_through": window.data["settled_through"],
                      "maturity": window.data["maturity"]},
        "completeness": completeness(fetched.data["rows"], totals.data, visible.data,
                                     fetched.data["dropped"], fetched.data["truncated"]),
        "calls": session.calls,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"queries_{start}_{end}.json"
    path.write_text(json.dumps(export, ensure_ascii=False, indent=1), encoding="utf-8")
    return Result.success(
        "exported", f"{len(export['rows'])} שורות נמשכו מ-GSC Wizard ({start} עד {end})",
        path=path, completeness=export["completeness"], window=export["window"],
        freshness=export["freshness"])


# ═══════════════════════════════════════════════════════
#  Two windows — for the skills that compare
# ═══════════════════════════════════════════════════════

def _worst(*states: bool | str) -> bool | str:
    """The least reassuring of several `truncated` answers: cut, then unknown, then whole."""
    if any(state is True for state in states):
        return True
    return False if all(state is False for state in states) else "unknown"


def _window_block(start: str, end: str, pages: dict[str, Any],
                  pairs: dict[str, Any]) -> dict[str, Any]:
    """One window in the shape `gsc_source.PageWindow.from_dict` reads.

    The page's own numbers come from the page pull; its query rows hang
    underneath and are never summed into it. A query row whose URL the page
    pull did not list has nowhere to hang, so it is counted instead of lost.
    """
    by_url: dict[str, list[dict[str, Any]]] = {}
    for row in pairs["rows"]:
        by_url.setdefault(row["url"], []).append(
            {"query": row["query"], "clicks": row["clicks"],
             "impressions": row["impressions"], "position": row["position"]})
    listed = [{**page, "queries": by_url.pop(page["url"], [])} for page in pages["rows"]]
    return {"range": f"{start}..{end}", "pages": listed,
            "truncated": _worst(pages["truncated"], pairs["truncated"]),
            "rows_dropped": pages["dropped"] + pairs["dropped"],
            "query_rows_without_page": sum(len(left) for left in by_url.values())}


def site_trend(clicks_before: int, clicks_after: int) -> float | None:
    """How the whole property moved, as a FRACTION: -0.125 is a 12.5% loss.

    A fraction because that is what reads it: `gsc_source.classify` subtracts
    it from a page's own fractional change and prints it with `:+.0%`, so a
    "-12.5" here would be reported as the site losing 1250%. Three decimals is
    the percentage to one. None when there were no clicks to move from —
    a change from zero is not a number.
    """
    return round((clicks_after - clicks_before) / clicks_before, 3) if clicks_before else None


def export_windows(property_url: str, out_dir: Path, days: int = 28, compare: str = "year",
                   search_type: str = "web", transport: Transport | None = None) -> Result:
    """Fetch two comparable windows and write them where `gsc_source.load_export` reads.

    The comparison window is pulled first, and an empty one stops everything
    before a file exists. Downstream, a prior window with no pages reads as
    "nothing declined" — and a property with no history a year back is the
    ordinary case, not a rare one: the first live property tried had none.
    Failing there also costs two calls instead of eight.
    """
    if not property_url:
        return Result.failure("gsc_wizard_no_property",
                              "ללקוח לא מוגדר gsc_property ב-clients.json — אין מה למשוך")
    if compare not in COMPARISONS:
        return Result.failure(
            "gsc_wizard_bad_compare",
            f"compare לא מוכר: {compare!r} (נתמכים: {', '.join(COMPARISONS)})")
    if compare == "year" and days > YEAR_BACK_DAYS:
        return Result.failure(
            "gsc_wizard_windows_overlap",
            f"חלון של {days} ימים חופף לעצמו בהשוואה שנתית ({YEAR_BACK_DAYS} ימים אחורה) — "
            "קצר את החלון או השווה מול התקופה הקודמת")
    opened = open_session(transport)
    if not opened:
        return opened
    session: Session = opened.data["session"]

    window = settled_window(session, property_url, days)
    if not window:
        return window
    start, end = window.data["start"], window.data["end"]
    prior_start, prior_end = comparison_window(start, end, compare)
    spans = {"window": {"start": start, "end": end},
             "comparison_window": {"start": prior_start, "end": prior_end, "compare": compare}}

    prior_pages = fetch_pages(session, property_url, prior_start, prior_end, search_type)
    if not prior_pages:
        return prior_pages
    if not prior_pages.data["rows"]:
        hint = (". לנכס צעיר משנה אפשר להשוות מול התקופה הקודמת (compare=\"previous\")"
                if compare == "year" else "")
        return Result.failure(
            "gsc_wizard_no_history",
            f"אין נתוני Search Console לחלון ההשוואה {prior_start}..{prior_end} — "
            f"ההשוואה חסומה, וזה לא אומר \"אין ירידה\"{hint}",
            recoverable=True, **spans)

    current_pages = fetch_pages(session, property_url, start, end, search_type)
    if not current_pages:
        return current_pages
    current_pairs = fetch_query_pages(session, property_url, start, end, search_type)
    if not current_pairs:
        return current_pairs
    prior_pairs = fetch_query_pages(session, property_url, prior_start, prior_end, search_type)
    if not prior_pairs:
        return prior_pairs
    totals = property_totals(session, property_url, start, end, search_type)
    if not totals:
        return totals
    prior_totals = property_totals(session, property_url, prior_start, prior_end, search_type)
    if not prior_totals:
        return prior_totals
    visible = visible_totals(session, property_url, start, end, search_type)
    if not visible:
        return visible

    current = _window_block(start, end, current_pages.data, current_pairs.data)
    prior = _window_block(prior_start, prior_end, prior_pages.data, prior_pairs.data)

    # `load_export` reads site.clicks_pct through float() with a default of 0.0:
    # a null would raise there, so a trend that cannot be measured is left out,
    # and the two totals beside it say why.
    trend = site_trend(prior_totals.data["clicks"], totals.data["clicks"])
    site: dict[str, Any] = {"clicks_current": totals.data["clicks"],
                            "clicks_prior": prior_totals.data["clicks"]}
    if trend is not None:
        site["clicks_pct"] = trend

    export = {
        "property": property_url,
        "current": current,
        "prior": prior,
        "site": site,
        # Dates of ranking updates live in one place, `export_updates`. A second
        # copy here would be a second answer to the same question.
        "algorithm_updates": [],
        "source": "gsc_wizard",
        "data_sources": sorted({source for pull in (current_pages, current_pairs,
                                                    prior_pages, prior_pairs)
                                for source in pull.data["data_sources"]}),
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "search_type": search_type,
        **spans,
        "freshness": {"settled_through": window.data["settled_through"],
                      "maturity": window.data["maturity"]},
        "completeness": completeness(current_pairs.data["rows"], totals.data, visible.data,
                                     current_pairs.data["dropped"], current["truncated"]),
        "calls": session.calls,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"windows_{start}_{end}_vs_{prior_start}_{prior_end}.json"
    path.write_text(json.dumps(export, ensure_ascii=False, indent=1), encoding="utf-8")
    return Result.success(
        "exported",
        f"{len(current['pages'])} דפים בחלון {start} עד {end}, "
        f"{len(prior['pages'])} בחלון ההשוואה {prior_start} עד {prior_end}",
        path=path, completeness=export["completeness"], freshness=export["freshness"],
        clicks_pct=trend, **spans)


# ═══════════════════════════════════════════════════════
#  Confirmed ranking updates
# ═══════════════════════════════════════════════════════

def _first(entry: dict[str, Any], keys: tuple[str, ...]) -> Any:
    return next((entry[key] for key in keys if entry.get(key)), None)


def _day(value: Any) -> str | None:
    """`YYYY-MM-DD` out of a date or an ISO timestamp; None when it is neither."""
    try:
        return date.fromisoformat(str(value or "")[:10]).isoformat()
    except ValueError:
        return None


def _kind(name: str) -> str:
    lowered = name.lower()
    for needle, kind in (("core", "core"), ("spam", "spam"), ("helpful", "helpful_content")):
        if needle in lowered:
            return kind
    return "other"


def parse_updates(payload: Any) -> Result:
    """Turn whatever `list_algo_updates` sent into the four keys `load_updates` reads.

    `load_updates` drops an entry it cannot parse without a word, so the
    checking happens here, where a skipped entry can still be counted. And a
    list in which nothing could be read is not an empty calendar — it is a
    wrong mapping, and it fails as one.
    """
    if isinstance(payload, list):
        entries = payload
    else:
        found = payload if isinstance(payload, dict) else {}
        key = next((k for k in UPDATE_LIST_KEYS if isinstance(found.get(k), list)), None)
        if key is None:
            seen = sorted(map(str, found)) or [f"<{type(payload).__name__}>"]
            return Result.failure(
                "gsc_wizard_updates_shape",
                f"list_algo_updates החזיר מבנה לא מוכר — אין רשימה תחת "
                f"{' / '.join(UPDATE_LIST_KEYS)}. המפתחות שהגיעו: {', '.join(seen)}",
                keys=seen)
        entries = found[key]

    updates: list[dict[str, str]] = []
    skipped = 0
    for entry in entries:
        named = entry if isinstance(entry, dict) else {}
        name = str(_first(named, UPDATE_NAME_KEYS) or "").strip()
        start = _day(_first(named, UPDATE_START_KEYS))
        if not name or not start:
            skipped += 1
            continue
        updates.append({"name": name, "start": start,
                        "end": _day(_first(named, UPDATE_END_KEYS)) or start,
                        "kind": _kind(name)})
    if entries and not updates:
        seen = (sorted(map(str, entries[0])) if isinstance(entries[0], dict)
                else [f"<{type(entries[0]).__name__}>"])
        return Result.failure(
            "gsc_wizard_updates_shape",
            f"list_algo_updates החזיר {len(entries)} רשומות ובאף אחת לא נמצאו שם ותאריך "
            f"התחלה. השדות ברשומה הראשונה: {', '.join(seen)}",
            keys=seen, skipped=skipped)
    updates.sort(key=lambda update: (update["start"], update["name"]))
    return Result.success("parsed", f"{len(updates)} עדכונים", updates=updates, skipped=skipped)


def export_updates(out_dir: Path, start: str | None = None, end: str | None = None,
                   transport: Transport | None = None) -> Result:
    """Fetch Google's confirmed ranking updates and write them where `load_updates` reads.

    The list bundled in `updates.py` goes stale by design; this is the fresher
    one it asks for, taken from the Search Status Dashboard through the server.
    """
    opened = open_session(transport)
    if not opened:
        return opened
    session: Session = opened.data["session"]

    asked = {key: value for key, value in (("startDate", start), ("endDate", end)) if value}
    reply = session.call("list_algo_updates", asked)
    if not reply:
        return reply
    parsed = parse_updates(reply.data["payload"])
    if not parsed:
        return parsed

    updates, skipped = parsed.data["updates"], parsed.data["skipped"]
    export = {"updates": updates, "source": "gsc_wizard",
              "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "skipped": skipped}
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "algo_updates.json"
    path.write_text(json.dumps(export, ensure_ascii=False, indent=1), encoding="utf-8")
    left_out = f" ({skipped} רשומות דולגו — בלי שם או בלי תאריך התחלה)" if skipped else ""
    return Result.success("exported", f"{len(updates)} עדכוני דירוג מאושרים נשמרו{left_out}",
                          path=path, count=len(updates), skipped=skipped)


# ═══════════════════════════════════════════════════════
#  What a skill calls
# ═══════════════════════════════════════════════════════

def ensure_queries(client: Client, days: int = 28) -> Result:
    """The one call a skill makes for its rows — so no skill carries fetch code of its own."""
    return export_queries(client.gsc_property, client.data_dir / "gsc", days)


def ensure_windows(client: Client, days: int = 28, compare: str = "year") -> Result:
    """The same, for a skill that compares two windows.

    Blocked stays blocked: a `gsc_wizard_no_history` failure is the answer to
    report, not something to work around by comparing against nothing.
    """
    return export_windows(client.gsc_property, client.data_dir / "gsc", days, compare)


def ensure_updates(client: Client) -> Result:
    """The confirmed ranking updates, kept beside the client's other Search Console files."""
    return export_updates(client.data_dir / "gsc")


def ensure_query_window(client: Client, start: str, end: str) -> Result:
    """One named span of rows — for a before/after pair around a dated event."""
    return export_queries(client.gsc_property, client.data_dir / "gsc",
                          window_dates=(start, end))


def settled_through(client: Client, transport: Transport | None = None) -> Result:
    """The last day Search Console has finished counting for this property.

    Asked of the server, not computed from today: the lag is two or three days
    and it is not constant, so a window chosen from the calendar can silently
    include days that are still filling up.
    """
    opened = open_session(transport)
    if not opened:
        return opened
    return settled_window(opened.data["session"], client.gsc_property, 1)


def describe(result: Result) -> list[tuple[str, str]]:
    """What a skill should say about a pull before it shows a single finding.

    Returned as (label, value) pairs rather than printed: every skill has its
    own `kv()`, and the wording — above all that the missing clicks are hidden
    by Search Console, not lost by us — should not drift between them.
    """
    if not result:
        return [("משיכת הנתונים נכשלה", result.detail)]
    fresh, cover = result.data["freshness"], result.data["completeness"]
    pairs = [
        ("נתונים סגורים עד", str(fresh["settled_through"])),
        ("הופעות בשאילתות גלויות",
         f"{cover['impressions_in_queries']:,} מתוך {cover['impressions_total']:,}"),
        ("קליקים בשאילתות גלויות",
         f"{cover['clicks_in_queries']:,} מתוך {cover['clicks_total']:,} — השאר בשאילתות "
         "ש-Search Console מסתיר"),
    ]
    if cover["truncated"] is not False:
        pairs.append(("שלמות המשיכה",
                      "לא ידוע אם המשיכה מלאה" if cover["truncated"] == "unknown"
                      else "המשיכה נחתכה — הניתוח חלקי"))
    return pairs


def rows_for(client: Client, given: str | None = None, days: int = 28) -> Result:
    """The rows a skill is about to analyse — handed to it, or fetched.

    Every skill asked its user for an export file that nothing produced. They
    all resolve it the same way now, and through one function, so the wording
    of what was fetched cannot drift between them.

    A file the caller named is trusted as-is: replaying a saved export is how a
    past run is reproduced, and re-fetching would quietly change the data under
    a comparison.
    """
    if given:
        return Result.success("given", f"קובץ שאילתות שסופק: {given}",
                              path=Path(given), lines=[], fetched=False)
    fetched = ensure_queries(client, days)
    if not fetched:
        return fetched
    return Result.success("fetched", fetched.detail, path=fetched.data["path"],
                          lines=describe(fetched), fetched=True,
                          completeness=fetched.data["completeness"])


def optional_rows_for(client: Client, given: str | None = None, days: int = 28) -> Result:
    """The same, for a skill that has something to say without Search Console.

    Always succeeds, because these skills crawl the site and report on it
    regardless. What it will not do is let a missing source pass for a clean
    result: when the rows are unavailable, `path` is None and `blocked` holds
    the Hebrew sentence the report has to carry, so "no pages with demand"
    can never be read as "no problem found".
    """
    if given:
        return Result.success("given", f"קובץ שאילתות שסופק: {given}",
                              path=Path(given), lines=[], blocked=None)
    if not client.gsc_property:
        return Result.success(
            "no_property", "ללקוח אין נכס Search Console", path=None, lines=[],
            blocked="ללקוח לא מוגדר gsc_property — החלקים שתלויים בנתוני חיפוש לא נבדקו")
    fetched = ensure_queries(client, days)
    if not fetched:
        return Result.success(
            "fetch_failed", fetched.detail, path=None, lines=[],
            blocked=f"נתוני Search Console לא נמשכו ({fetched.detail}) — "
                    "החלקים שתלויים בהם חסומים, לא נקיים")
    return Result.success("fetched", fetched.detail, path=fetched.data["path"],
                          lines=describe(fetched), blocked=None,
                          completeness=fetched.data["completeness"])
