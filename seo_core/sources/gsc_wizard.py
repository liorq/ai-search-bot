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
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .. import secrets
from ..schema import Result

ENDPOINT = "https://mcp.gscwizard.com/mcp"
KEY_NAME = "GSC_WIZARD_API_KEY"
PROTOCOL_VERSION = "2025-03-26"

#: The most rows one call may ask for. A property that fills a page exactly is
#: not assumed to be cut off — the next page is requested, and only a page that
#: is still full when the guard trips is reported as truncated.
PAGE_SIZE = 25000
MAX_PAGES = 20

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


# ═══════════════════════════════════════════════════════
#  Rows
# ═══════════════════════════════════════════════════════

def fetch_query_pages(session: Session, property_url: str, start: str, end: str,
                      search_type: str = "web") -> Result:
    """Every query+page pair in the window — both dimensions, never merged.

    Cannibalisation is two of our URLs on one query; a query-only pull would
    add them together and hide exactly the thing being looked for.
    """
    rows: list[dict[str, Any]] = []
    dropped = 0
    sources: set[str] = set()
    truncated: bool | str = "unknown"
    offset = 0
    for _ in range(MAX_PAGES):
        page = session.call("query_search_analytics", {
            "siteUrl": property_url, "startDate": start, "endDate": end,
            "dimensions": ["query", "page"], "searchType": search_type,
            "rowLimit": PAGE_SIZE, "startRow": offset})
        if not page:
            return page
        data = page.data["payload"]
        sources.add(str(data.get("dataSource", "unknown")))
        for entry in data.get("rows") or []:
            keys = entry.get("keys") or []
            if len(keys) != 2 or not keys[0] or not keys[1]:
                dropped += 1
                continue
            rows.append({"query": keys[0], "url": keys[1],
                         "clicks": int(entry.get("clicks") or 0),
                         "impressions": int(entry.get("impressions") or 0),
                         "position": float(entry.get("position") or 0)})
        pagination = data.get("pagination") or {}
        if "hasMore" not in pagination:
            break                                  # completeness stays "unknown"
        if not pagination["hasMore"]:
            truncated = False
            break
        truncated = True
        offset = int(pagination.get("nextOffset") or offset + PAGE_SIZE)
    return Result.success("rows", f"{len(rows)} שורות שאילתה+דף", rows=rows,
                          dropped=dropped, truncated=truncated, data_sources=sorted(sources))


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
                   search_type: str = "web", transport: Transport | None = None) -> Result:
    """Fetch the settled window and write it where `queries.load_export` reads."""
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
    start, end = window.data["start"], window.data["end"]

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
