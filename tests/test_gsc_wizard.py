"""The GSC Wizard fetcher, against a scripted server — no network, no real key."""

from __future__ import annotations

import json
from datetime import date

import pytest

from seo_core.clients import Client
from seo_core.sources import gsc_source, gsc_wizard, queries, updates

PROPERTY = "sc-domain:example.com"
URL = "https://example.com/facial"
SETTLED = "2026-09-16"
CURRENT = {"start": "2026-08-20", "end": SETTLED}


def sse(payload: dict) -> str:
    return "event: message\ndata: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


def tool_reply(data: dict) -> dict:
    return {"jsonrpc": "2.0", "id": 1, "result": {
        "content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}],
        "structuredContent": data}}


def pages_of(rows):
    """The page pull of a fake that was given none: its query rows, summed per URL."""
    by_url: dict[str, dict] = {}
    for r in rows:
        if len(r["keys"]) != 2:
            continue
        agg = by_url.setdefault(r["keys"][1], {"keys": [r["keys"][1]], "clicks": 0,
                                               "impressions": 0, "ctr": 1.0,
                                               "position": r["position"]})
        agg["clicks"] += r["clicks"]
        agg["impressions"] += r["impressions"]
    return list(by_url.values())


class Server:
    """Answers like the real endpoint did when it was probed by hand.

    It holds two date windows. The current one is whatever is asked about up to
    the settled day; any other end date gets the prior one — empty unless a
    test fills it, which is what the real property answered for the same 28
    days a year earlier.
    """

    def __init__(self, rows, totals=(100, 1000), page_size=None, init_status=200,
                 report_pagination=True, pages=None, prior=(), prior_pages=None,
                 prior_totals=None, algo_updates=None):
        self.rows, self.totals = rows, totals
        self.page_size, self.init_status = page_size, init_status
        self.report_pagination = report_pagination
        self.pages = pages
        self.prior, self.prior_pages, self.prior_totals = list(prior), prior_pages, prior_totals
        self.algo_updates = algo_updates
        self.seen: list[dict] = []

    def window(self, args):
        """Which of the two date windows a call is asking about."""
        if args.get("endDate", SETTLED) == SETTLED:
            return self.rows, self.pages, self.totals
        return self.prior, self.prior_pages, self.prior_totals

    def __call__(self, payload, headers):
        # Never `assert a == b` on this header: a failing comparison prints both
        # sides, and on a developer's machine one side could be a real key.
        if headers["Authorization"] != "Bearer test-key":
            raise AssertionError("unexpected Authorization header (value withheld)")
        self.seen.append(payload)
        stream = {"content-type": "text/event-stream"}
        if payload["method"] == "initialize":
            return self.init_status, {**stream, "mcp-session-id": "s1"}, \
                sse({"result": {}}) if self.init_status == 200 else '{"error":"unauthorized"}'
        if payload["method"] == "notifications/initialized":
            return 202, {}, ""
        if payload["params"]["name"] == "list_algo_updates":
            return 200, stream, sse(tool_reply(self.algo_updates))
        args = payload["params"]["arguments"]
        rows, pages, totals = self.window(args)
        base = {"settledThrough": SETTLED, "dataSource": "api",
                "dataMaturity": {"settledThrough": SETTLED}}
        if not args.get("dimensions"):
            if totals is None:                     # a window with no data: no totals row
                return 200, stream, sse(tool_reply({**base, "rows": []}))
            clicks, impressions = totals
            return 200, stream, sse(tool_reply({**base, "rows": [
                {"keys": [], "clicks": clicks, "impressions": impressions,
                 "ctr": 10.0, "position": 9.9}]}))
        if args["dimensions"] == ["query"]:
            # Property-style aggregation: one row per query, however many URLs showed.
            by_query: dict[str, dict] = {}
            for r in rows:
                if len(r["keys"]) != 2:
                    continue
                agg = by_query.setdefault(r["keys"][0], {"keys": [r["keys"][0]],
                                                         "clicks": 0, "impressions": 0})
                agg["clicks"] += r["clicks"]
                agg["impressions"] += r["impressions"]
            return 200, stream, sse(tool_reply({**base, "rows": list(by_query.values()),
                                                "pagination": {"hasMore": False}}))
        if args["dimensions"] == ["page"]:
            rows = pages_of(rows) if pages is None else pages
        start = args.get("startRow", 0)
        size = self.page_size or len(rows) or 1
        chunk = rows[start:start + size]
        more = start + size < len(rows)
        reply = {**base, "rows": chunk}
        if self.report_pagination:
            reply["pagination"] = {"hasMore": more, "nextOffset": start + size}
        return 200, stream, sse(tool_reply(reply))


def row(query, url, clicks=1, impressions=100, position=8.0):
    # ctr arrives as a PERCENTAGE from the server; the export must not carry it.
    return {"keys": [query, url], "clicks": clicks, "impressions": impressions,
            "ctr": 100.0 * clicks / impressions, "position": position}


def page(url, clicks=1, impressions=100, position=8.0):
    return {"keys": [url], "clicks": clicks, "impressions": impressions,
            "ctr": 100.0 * clicks / impressions, "position": position}


def calls(server, dimensions=None):
    """The arguments of every tool call the server saw — optionally of one pull only."""
    found = [p["params"]["arguments"] for p in server.seen if p["method"] == "tools/call"]
    return [a for a in found if dimensions is None or a.get("dimensions") == dimensions]


def read(result):
    return json.loads(result.data["path"].read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def fake_key(monkeypatch):
    """A fake key, and no way to reach the real one.

    `load_env` is cut off as well: without that, a test that loses this fixture
    (one did, by naming a parameter after it) reads the developer's real .env
    and sends — and prints — a live credential.
    """
    monkeypatch.setattr(gsc_wizard.secrets, "load_env", lambda *a, **k: {})
    monkeypatch.setenv(gsc_wizard.KEY_NAME, "test-key")


def export(tmp_path, server):
    return gsc_wizard.export_queries(PROPERTY, tmp_path, transport=server)


def test_the_export_is_readable_by_the_loader_the_skills_already_use(tmp_path):
    server = Server([row("facial sarasota", "https://example.com/facial"),
                     row("facial sarasota", "https://example.com/spa")])
    result = export(tmp_path, server)
    assert result, result.detail
    loaded = queries.load_export(result.data["path"])
    assert loaded and len(loaded.data["rows"]) == 2
    # Two URLs on one query stay two rows — merging them would hide cannibalisation.
    assert {r.url for r in loaded.data["rows"]} == {
        "https://example.com/facial", "https://example.com/spa"}


def test_the_window_is_explicit_dates_ending_on_the_settled_day(tmp_path):
    result = export(tmp_path, Server([row("q", "https://example.com/")]))
    assert result.data["window"] == {"start": "2026-08-20", "end": "2026-09-16"}
    written = json.loads(result.data["path"].read_text(encoding="utf-8"))
    assert written["range"] == "2026-08-20..2026-09-16"
    assert written["freshness"]["settled_through"] == "2026-09-16"
    assert written["fetched_at"] and written["source"] == "gsc_wizard"


def test_percent_ctr_never_reaches_the_export(tmp_path):
    result = export(tmp_path, Server([row("q", "https://example.com/", clicks=5)]))
    written = json.loads(result.data["path"].read_text(encoding="utf-8"))
    assert all("ctr" not in r for r in written["rows"])
    loaded = queries.load_export(result.data["path"])
    assert all(0.0 <= r.ctr <= 1.0 for r in loaded.data["rows"])


def test_a_full_page_is_followed_not_assumed_to_be_the_end(tmp_path):
    rows = [row(f"q{i}", "https://example.com/") for i in range(5)]
    server = Server(rows, page_size=2)
    result = export(tmp_path, server)
    written = json.loads(result.data["path"].read_text(encoding="utf-8"))
    assert len(written["rows"]) == 5
    assert written["completeness"]["truncated"] is False
    offsets = [p["params"]["arguments"].get("startRow") for p in server.seen
               if p["method"] == "tools/call"
               and p["params"]["arguments"].get("dimensions") == ["query", "page"]]
    assert offsets == [0, 2, 4]          # paged with startRow, the parameter that works


def test_unknown_completeness_is_said_out_loud(tmp_path):
    server = Server([row("q", "https://example.com/")], report_pagination=False)
    written = json.loads(export(tmp_path, server).data["path"].read_text(encoding="utf-8"))
    assert written["completeness"]["truncated"] == "unknown"


def test_hidden_queries_are_measured_against_the_property_totals(tmp_path):
    server = Server([row("q", "https://example.com/", clicks=40, impressions=900)],
                    totals=(100, 1000))
    cover = export(tmp_path, server).data["completeness"]
    assert cover["anonymised_share"] == pytest.approx(0.1)
    assert cover["anonymised_click_share"] == pytest.approx(0.6)


def test_a_row_without_a_page_is_counted_not_silently_skipped(tmp_path):
    server = Server([row("q", "https://example.com/"),
                     {"keys": ["orphan"], "clicks": 1, "impressions": 5, "position": 3}])
    cover = export(tmp_path, server).data["completeness"]
    assert cover["rows_dropped"] == 1 and cover["rows_total"] == 2


def test_a_query_with_a_unicode_line_separator_survives_the_event_stream(tmp_path):
    # str.splitlines() would cut the JSON in half here; it happened on a real pull.
    server = Server([row("brow lamination", "https://example.com/")])
    result = export(tmp_path, server)
    assert result, result.detail


def test_a_revoked_key_says_so_instead_of_pretending_there_is_no_data(tmp_path):
    result = export(tmp_path, Server([], init_status=401))
    assert not result and result.code == "gsc_wizard_key_rejected"


def test_no_key_and_no_property_fail_before_any_request(tmp_path, monkeypatch):
    assert gsc_wizard.export_queries("", tmp_path).code == "gsc_wizard_no_property"
    monkeypatch.delenv(gsc_wizard.KEY_NAME)
    monkeypatch.setattr(gsc_wizard.secrets, "load_env", lambda: {})
    assert export(tmp_path, Server([])).code == "gsc_wizard_no_key"


# ── confidence is held to what the data can carry ─────────────────────────

def test_confidence_is_capped_when_most_clicks_are_hidden():
    level, why = queries.cap_confidence("high", "עקומה מהאתר", {
        "truncated": False, "anonymised_share": 0.05, "anonymised_click_share": 0.6})
    assert level == "medium" and "60%" in why


def test_confidence_is_capped_when_completeness_is_unknown_or_cut():
    assert queries.cap_confidence("high", "x", None)[0] == "medium"
    assert queries.cap_confidence("high", "x", {"truncated": "unknown"})[0] == "medium"
    assert queries.cap_confidence("high", "x", {"truncated": True})[0] == "low"


def test_complete_data_leaves_confidence_alone():
    full = {"truncated": False, "anonymised_share": 0.05, "anonymised_click_share": 0.1}
    assert queries.cap_confidence("high", "x", full) == ("high", "x")


def test_a_malformed_row_in_an_export_is_counted(tmp_path):
    path = tmp_path / "q.json"
    path.write_text(json.dumps({"rows": [
        {"query": "a", "url": "https://example.com/", "clicks": 1, "impressions": 9, "position": 2},
        {"query": "no url"}]}), encoding="utf-8")
    loaded = queries.load_export(path)
    assert loaded.data["dropped"] == 1 and "פגומות" in loaded.detail


# ── the page pull ─────────────────────────────────────────────────────────

def session_for(server):
    return gsc_wizard.open_session(server).data["session"]


def test_a_full_page_of_pages_is_followed_and_a_page_without_a_url_is_counted():
    listed = [page(f"https://example.com/{i}") for i in range(5)]
    listed.append({"keys": [""], "clicks": 1, "impressions": 5, "position": 3})
    server = Server([], pages=listed, page_size=2)
    pulled = gsc_wizard.fetch_pages(session_for(server), PROPERTY, **CURRENT)
    assert pulled, pulled.detail
    assert len(pulled.data["rows"]) == 5 and pulled.data["dropped"] == 1
    assert pulled.data["truncated"] is False
    assert set(pulled.data["rows"][0]) == {"url", "clicks", "impressions", "position"}
    assert [a["startRow"] for a in calls(server, ["page"])] == [0, 2, 4]


def test_a_page_pull_the_guard_stops_is_reported_as_cut(monkeypatch):
    monkeypatch.setattr(gsc_wizard, "MAX_PAGES", 2)
    server = Server([], pages=[page(f"https://example.com/{i}") for i in range(5)], page_size=1)
    pulled = gsc_wizard.fetch_pages(session_for(server), PROPERTY, **CURRENT)
    assert pulled.data["truncated"] is True and len(pulled.data["rows"]) == 2


def test_a_page_pull_with_no_paging_report_does_not_claim_to_be_whole():
    server = Server([], pages=[page(URL)], report_pagination=False)
    pulled = gsc_wizard.fetch_pages(session_for(server), PROPERTY, **CURRENT)
    assert pulled.data["truncated"] == "unknown"


# ── two windows, for the skills that compare ──────────────────────────────

def two_windows(**overrides):
    """A property with a year of history: one page, and the numbers deliberately disagree.

    The page says 25 clicks, its visible queries add up to 7, the property
    says 80 — so a test can tell which of the three a figure was taken from.
    """
    settings = dict(
        rows=[row("facial sarasota", URL, clicks=3, impressions=100),
              row("hydrafacial near me", URL, clicks=4, impressions=200)],
        pages=[page(URL, clicks=25, impressions=900, position=5.5)],
        totals=(80, 2000),
        prior=[row("facial sarasota", URL, clicks=9, impressions=300)],
        prior_pages=[page(URL, clicks=40, impressions=1100, position=3.2)],
        prior_totals=(100, 2400))
    settings.update(overrides)
    return Server(settings.pop("rows"), **settings)


def windows(tmp_path, server, **kwargs):
    return gsc_wizard.export_windows(PROPERTY, tmp_path, transport=server, **kwargs)


def span(window):
    return (date.fromisoformat(window["end"]) - date.fromisoformat(window["start"])).days + 1


def test_the_windows_export_is_readable_by_the_loader_content_decay_uses(tmp_path):
    result = windows(tmp_path, two_windows())
    assert result, result.detail
    loaded = gsc_source.load_export(result.data["path"])
    assert loaded, loaded.detail
    now, before = loaded.data["current"][URL], loaded.data["prior"][URL]
    assert (now.clicks, before.clicks) == (25, 40)
    assert [q.query for q in now.queries] == ["facial sarasota", "hydrafacial near me"]
    assert [q.query for q in before.queries] == ["facial sarasota"]
    assert loaded.data["property"] == PROPERTY and loaded.data["algorithm_updates"] == []


def test_the_year_comparison_ends_364_days_earlier_and_is_the_same_length(tmp_path):
    server = two_windows()
    result = windows(tmp_path, server)
    current, prior = result.data["window"], result.data["comparison_window"]
    assert current == CURRENT
    assert (prior["start"], prior["end"]) == ("2025-08-21", "2025-09-17")
    ends = date.fromisoformat(current["end"]), date.fromisoformat(prior["end"])
    assert (ends[0] - ends[1]).days == 364 and ends[0].weekday() == ends[1].weekday()
    assert span(current) == span(prior) == 28
    assert result.data["path"].name == \
        "windows_2026-08-20_2026-09-16_vs_2025-08-21_2025-09-17.json"
    # ...and those are the dates the server was actually asked about.
    assert ("2025-08-21", "2025-09-17") in {(a.get("startDate"), a.get("endDate"))
                                            for a in calls(server)}


def test_the_previous_comparison_is_the_same_length_immediately_before(tmp_path):
    prior = windows(tmp_path, two_windows(), compare="previous").data["comparison_window"]
    assert (prior["start"], prior["end"]) == ("2026-07-23", "2026-08-19")
    assert span(prior) == 28 and prior["compare"] == "previous"


def test_a_comparison_that_cannot_be_made_is_refused_before_any_request(tmp_path):
    server = two_windows()
    assert windows(tmp_path, server, compare="quarter").code == "gsc_wizard_bad_compare"
    # 400 days against "364 days back" would compare a window with part of itself.
    assert windows(tmp_path, server, days=400).code == "gsc_wizard_windows_overlap"
    assert server.seen == [] and list(tmp_path.iterdir()) == []


def test_page_totals_come_from_the_page_pull_not_from_summed_query_rows(tmp_path):
    written = read(windows(tmp_path, two_windows()))
    now, before = written["current"]["pages"][0], written["prior"]["pages"][0]
    assert (now["clicks"], now["impressions"], now["position"]) == (25, 900, 5.5)
    assert (before["clicks"], before["impressions"], before["position"]) == (40, 1100, 3.2)
    # The gap is the queries Search Console hides — summing would have lost 18 clicks.
    assert sum(q["clicks"] for q in now["queries"]) == 7


def test_an_empty_comparison_window_is_a_failure_not_a_clean_bill_of_health(tmp_path):
    server = Server([row("q", URL, clicks=5)])      # a year back: no rows, as on the real property
    result = windows(tmp_path, server)
    assert not result and result.code == "gsc_wizard_no_history" and result.recoverable
    assert "2025-08-21..2025-09-17" in result.detail
    assert result.data["window"] == CURRENT
    assert result.data["comparison_window"]["end"] == "2025-09-17"
    assert list(tmp_path.iterdir()) == []            # blocked is not passed: no file to misread
    # Nothing more is fetched for a comparison that cannot happen.
    assert calls(server, ["query", "page"]) == []


def test_the_site_trend_is_measured_on_property_totals_as_a_fraction(tmp_path):
    result = windows(tmp_path, two_windows(totals=(80, 2000), prior_totals=(100, 2400)))
    # 100 → 80 is -0.2. The page went 40 → 25; the trend must not be taken from there.
    assert read(result)["site"]["clicks_pct"] == pytest.approx(-0.2)
    assert result.data["clicks_pct"] == pytest.approx(-0.2)
    # A fraction, because classify() subtracts it from one and prints it with %.
    assert gsc_source.load_export(result.data["path"]).data["site_trend"] == pytest.approx(-0.2)


def test_a_site_trend_from_zero_prior_clicks_is_left_out_not_invented(tmp_path):
    result = windows(tmp_path, two_windows(prior_totals=(0, 2400)))
    assert result, result.detail
    assert result.data["clicks_pct"] is None
    site = read(result)["site"]
    assert "clicks_pct" not in site and site["clicks_prior"] == 0 and site["clicks_current"] == 80
    # A null would crash the loader's float(); a missing key it can read.
    assert gsc_source.load_export(result.data["path"])


def test_percent_ctr_never_reaches_the_windows_export(tmp_path):
    result = windows(tmp_path, two_windows())
    assert '"ctr"' not in result.data["path"].read_text(encoding="utf-8")
    loaded = gsc_source.load_export(result.data["path"])
    assert all(q.ctr == 0.0 for p in loaded.data["current"].values() for q in p.queries)


def test_the_windows_export_carries_the_same_contract_as_the_queries_export(tmp_path):
    written = read(windows(tmp_path, two_windows()))
    assert written["source"] == "gsc_wizard" and written["fetched_at"]
    assert written["data_sources"] == ["api"] and written["search_type"] == "web"
    assert written["window"] == CURRENT and written["comparison_window"]["compare"] == "year"
    assert written["freshness"]["settled_through"] == SETTLED
    assert written["calls"] == 8                     # what one comparison costs the quota
    cover = written["completeness"]                  # of the current window
    assert cover["truncated"] is False
    assert (cover["clicks_in_queries"], cover["clicks_total"]) == (7, 80)


def test_a_query_row_whose_page_was_not_listed_is_counted(tmp_path):
    server = two_windows(rows=[row("facial sarasota", URL, clicks=3),
                               row("stray", "https://example.com/unlisted", clicks=1)])
    block = read(windows(tmp_path, server))["current"]
    assert block["query_rows_without_page"] == 1 and len(block["pages"]) == 1


# ── confirmed ranking updates ─────────────────────────────────────────────

UPDATES = [
    {"name": "March 2026 core update", "startDate": "2026-03-10T14:00:00Z",
     "endDate": "2026-03-24T09:30:00Z"},
    {"title": "August 2026 spam update", "begin": "2026-08-26T00:00:00+00:00"},
    {"name": "Helpful content refresh", "date": "2025-12-05", "resolved": "2026-01-12"},
    {"name": "Site reputation abuse", "start": "2026-05-06", "end": "2026-05-07"},
]


def fetch_updates(tmp_path, reply, *dates):
    server = Server([], algo_updates=reply)
    return gsc_wizard.export_updates(tmp_path, *dates, transport=server), server


def test_updates_are_normalised_to_four_keys_with_plain_dates_and_a_kind(tmp_path):
    result, _ = fetch_updates(tmp_path, {"updates": UPDATES})
    assert result, result.detail
    assert result.data["path"].name == "algo_updates.json"
    assert (result.data["count"], result.data["skipped"]) == (4, 0)
    written = read(result)
    assert written["source"] == "gsc_wizard" and written["fetched_at"] and written["skipped"] == 0
    assert written["updates"] == [
        {"name": "Helpful content refresh", "start": "2025-12-05", "end": "2026-01-12",
         "kind": "helpful_content"},
        {"name": "March 2026 core update", "start": "2026-03-10", "end": "2026-03-24",
         "kind": "core"},
        {"name": "Site reputation abuse", "start": "2026-05-06", "end": "2026-05-07",
         "kind": "other"},
        # No end was given, so the update ends the day it started.
        {"name": "August 2026 spam update", "start": "2026-08-26", "end": "2026-08-26",
         "kind": "spam"},
    ]


def test_the_updates_file_is_readable_by_the_loader_algorithm_update_watch_uses(tmp_path):
    result, _ = fetch_updates(tmp_path, {"updates": UPDATES})
    loaded = updates.load_updates(result.data["path"])
    assert [u.name for u in loaded] == [u["name"] for u in read(result)["updates"]]
    assert updates.in_window(date(2026, 3, 12), date(2026, 3, 13), loaded)[0].kind == "core"


@pytest.mark.parametrize("list_key", ["updates", "rows", "items", "incidents"])
def test_the_update_list_is_found_under_any_name_it_might_go_by(tmp_path, list_key):
    result, _ = fetch_updates(tmp_path, {list_key: UPDATES})
    assert result and result.data["count"] == 4


def test_a_bare_list_of_updates_is_read_too():
    parsed = gsc_wizard.parse_updates(UPDATES)
    assert parsed and len(parsed.data["updates"]) == 4


def test_an_update_without_a_name_or_a_start_is_counted_not_quietly_dropped(tmp_path):
    entries = [UPDATES[0],
               {"startDate": "2026-04-01", "endDate": "2026-04-09"},      # no name
               {"name": "June 2026 core update"},                          # no start
               {"name": "Undated", "start": "sometime in May"}]            # not a date
    result, _ = fetch_updates(tmp_path, {"updates": entries})
    assert (result.data["count"], result.data["skipped"]) == (1, 3)
    assert read(result)["skipped"] == 3 and "3" in result.detail


def test_an_unknown_updates_reply_fails_and_names_the_keys_it_saw(tmp_path):
    result, _ = fetch_updates(tmp_path, {"changelog": UPDATES, "total": 4})
    assert not result and result.code == "gsc_wizard_updates_shape"
    assert "changelog" in result.detail and "total" in result.detail
    assert result.data["keys"] == ["changelog", "total"]
    assert list(tmp_path.iterdir()) == []


def test_a_list_nothing_can_be_read_from_is_a_wrong_mapping_not_an_empty_calendar(tmp_path):
    unreadable = [{"label": "March core", "from": "2026-03-10"}]
    result, _ = fetch_updates(tmp_path, {"updates": unreadable})
    assert not result and result.code == "gsc_wizard_updates_shape"
    assert "label" in result.detail and list(tmp_path.iterdir()) == []


def test_a_date_range_is_passed_on_only_when_one_is_asked_for(tmp_path):
    _, quiet = fetch_updates(tmp_path, {"updates": UPDATES})
    _, ranged = fetch_updates(tmp_path, {"updates": UPDATES}, "2026-01-01", SETTLED)
    assert calls(quiet) == [{}]
    assert calls(ranged) == [{"startDate": "2026-01-01", "endDate": SETTLED}]


# ── what a skill calls ────────────────────────────────────────────────────

def test_every_skill_gets_its_files_under_the_clients_own_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("SEO_HOME", str(tmp_path))
    server = two_windows(algo_updates={"updates": UPDATES})
    # Called the way a skill calls them: a client and nothing else.
    monkeypatch.setattr(gsc_wizard, "requests_transport", lambda: server)
    client = Client(domain="example.com", gsc_property=PROPERTY)
    home = tmp_path / "data" / "example.com" / "gsc"
    for result in (gsc_wizard.ensure_queries(client), gsc_wizard.ensure_windows(client),
                   gsc_wizard.ensure_updates(client)):
        assert result, result.detail
        assert result.data["path"].parent == home
    assert sorted(p.name.split("_")[0] for p in home.iterdir()) == ["algo", "queries", "windows"]


def test_a_client_without_a_property_is_told_so_by_the_helper(tmp_path, monkeypatch):
    monkeypatch.setenv("SEO_HOME", str(tmp_path))
    result = gsc_wizard.ensure_queries(Client(domain="example.com"))
    assert result.code == "gsc_wizard_no_property" and not (tmp_path / "data").exists()


def test_describe_says_the_missing_clicks_are_hidden_not_lost(tmp_path):
    server = Server([row("q", URL, clicks=40, impressions=900)], totals=(100, 1000))
    pairs = gsc_wizard.describe(export(tmp_path, server))
    said = " | ".join(value for _, value in pairs)
    assert SETTLED in said and "900 מתוך 1,000" in said
    assert "40 מתוך 100" in said and "מסתיר" in said
    assert len(pairs) == 3                           # a whole pull: nothing to warn about


def test_describe_flags_a_pull_whose_completeness_is_unknown(tmp_path):
    server = Server([row("q", URL)], report_pagination=False)
    pairs = gsc_wizard.describe(export(tmp_path, server))
    assert len(pairs) == 4 and "לא ידוע" in pairs[-1][1]


def test_describe_flags_a_pull_that_was_cut(tmp_path, monkeypatch):
    monkeypatch.setattr(gsc_wizard, "MAX_PAGES", 1)
    server = Server([row(f"q{i}", URL) for i in range(3)], page_size=1)
    pairs = gsc_wizard.describe(export(tmp_path, server))
    assert "חלקי" in pairs[-1][1]


def test_describe_of_a_failed_pull_says_why_instead_of_raising(tmp_path):
    pairs = gsc_wizard.describe(export(tmp_path, Server([], init_status=401)))
    assert len(pairs) == 1 and "מפתח" in pairs[0][1]
