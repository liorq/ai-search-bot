"""The GSC Wizard fetcher, against a scripted server — no network, no real key."""

from __future__ import annotations

import json

import pytest

from seo_core.sources import gsc_wizard, queries

PROPERTY = "sc-domain:example.com"


def sse(payload: dict) -> str:
    return "event: message\ndata: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


def tool_reply(data: dict) -> dict:
    return {"jsonrpc": "2.0", "id": 1, "result": {
        "content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}],
        "structuredContent": data}}


class Server:
    """Answers like the real endpoint did when it was probed by hand."""

    def __init__(self, rows, totals=(100, 1000), page_size=None, init_status=200,
                 report_pagination=True):
        self.rows, self.totals = rows, totals
        self.page_size, self.init_status = page_size, init_status
        self.report_pagination = report_pagination
        self.seen: list[dict] = []

    def __call__(self, payload, headers):
        assert headers["Authorization"] == "Bearer test-key"
        self.seen.append(payload)
        stream = {"content-type": "text/event-stream"}
        if payload["method"] == "initialize":
            return self.init_status, {**stream, "mcp-session-id": "s1"}, \
                sse({"result": {}}) if self.init_status == 200 else '{"error":"unauthorized"}'
        if payload["method"] == "notifications/initialized":
            return 202, {}, ""
        args = payload["params"]["arguments"]
        base = {"settledThrough": "2026-09-16", "dataSource": "api",
                "dataMaturity": {"settledThrough": "2026-09-16"}}
        if not args.get("dimensions"):
            clicks, impressions = self.totals
            return 200, stream, sse(tool_reply({**base, "rows": [
                {"keys": [], "clicks": clicks, "impressions": impressions,
                 "ctr": 10.0, "position": 9.9}]}))
        start = args.get("startRow", 0)
        size = self.page_size or len(self.rows) or 1
        chunk = self.rows[start:start + size]
        more = start + size < len(self.rows)
        reply = {**base, "rows": chunk}
        if self.report_pagination:
            reply["pagination"] = {"hasMore": more, "nextOffset": start + size}
        return 200, stream, sse(tool_reply(reply))


def row(query, url, clicks=1, impressions=100, position=8.0):
    # ctr arrives as a PERCENTAGE from the server; the export must not carry it.
    return {"keys": [query, url], "clicks": clicks, "impressions": impressions,
            "ctr": 100.0 * clicks / impressions, "position": position}


@pytest.fixture(autouse=True)
def key(monkeypatch):
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
               if p["method"] == "tools/call" and p["params"]["arguments"].get("dimensions")]
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
