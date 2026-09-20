"""Clarity forgets after three days, so what is not captured is lost.

The limits here were measured against the live API on 2026-09-20, not read
off a page: numOfDays 1-3 return data and 4 returns HTTP 400; the daily call
budget runs out after about ten requests and answers 429 "Exceeded daily
limit" with no Retry-After.
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from seo_core.sources import clarity                  # noqa: E402

CLIENT = "example.com"
TODAY = date(2026, 9, 20)
PAGE = "https://example.com/facial"


def metrics(sessions=10, dead=2, rage=1):
    return [
        {"metricName": "Traffic", "information": [
            {"Url": PAGE, "totalSessionCount": sessions, "totalBotSessionCount": 2}]},
        {"metricName": "DeadClickCount", "information": [{"Url": PAGE, "subTotal": dead}]},
        {"metricName": "RageClickCount", "information": [{"Url": PAGE, "subTotal": rage}]},
        {"metricName": "ScrollDepth", "information": [{"Url": PAGE, "subTotal": 62.5}]},
    ]


class Api:
    """Answers the way the real endpoint did when it was probed by hand."""

    def __init__(self, status=200, body=None):
        self.status, self.body = status, body
        self.asked: list[dict] = []

    def __call__(self, params):
        self.asked.append(params)
        if self.status != 200:
            return self.status, self.body or ""
        return 200, json.dumps(self.body if self.body is not None else metrics())


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("SEO_HOME", str(tmp_path))
    return tmp_path


# ── the window ────────────────────────────────────────────────────────────

def test_the_window_is_three_days_and_today_is_not_in_it():
    # Today is still filling up; filing a partial day as final makes a normal
    # Monday look like a collapse.
    days = clarity.reachable_days(TODAY)
    assert days == [date(2026, 9, 19), date(2026, 9, 18), date(2026, 9, 17)]
    assert TODAY not in days


def test_asking_beyond_the_window_is_refused_before_a_call_is_made():
    api = Api()
    refused = clarity.fetch(api, days=4)
    assert not refused and refused.code == "clarity_window"
    assert api.asked == []


# ── no duplicate calls ────────────────────────────────────────────────────

def test_a_day_already_stored_is_never_fetched_again():
    api = Api()
    assert clarity.snapshot(CLIENT, api, today=TODAY)
    first = len(api.asked)
    again = clarity.snapshot(CLIENT, api, today=TODAY)
    assert again and again.data["calls"] == 0
    assert len(api.asked) == first          # the budget is about ten a day


def test_the_whole_window_is_covered_by_one_call():
    api = Api()
    clarity.snapshot(CLIENT, api, today=TODAY)
    assert len(api.asked) == 1
    assert api.asked[0]["numOfDays"] == clarity.MAX_DAYS_BACK


# ── catching up, and admitting what cannot be caught ──────────────────────

def test_days_the_machine_missed_are_recovered_while_they_are_in_range():
    api = Api()
    taken = clarity.snapshot(CLIENT, api, today=TODAY)
    assert taken and "2026-09-19" in taken.data["captured"]


def test_a_day_out_of_range_is_recorded_as_a_gap_not_invented():
    api = Api()
    result = clarity.snapshot(CLIENT, api, today=TODAY)
    # One call answers for a window, so only its newest day is claimed;
    # the older ones are holes, and they say so.
    assert result.data["gaps"] == ["2026-09-18", "2026-09-17"]
    assert {f["day"] for f in clarity.open_failures(CLIENT)} == {"2026-09-18", "2026-09-17"}


def test_a_gap_is_not_filled_in_by_guessing():
    api = Api()
    clarity.snapshot(CLIENT, api, today=TODAY)
    stored = clarity.load_days(CLIENT)
    assert set(stored) == {date(2026, 9, 19)}       # no invented Wednesday


# ── failure is never filed as success ─────────────────────────────────────

def test_a_spent_daily_budget_is_a_failure_with_the_reason():
    refused = clarity.snapshot(CLIENT, Api(429, "Exceeded daily limit"), today=TODAY)
    assert not refused and refused.code == "clarity_quota"
    assert "מכסה" in refused.detail


def test_a_failed_snapshot_claims_no_day():
    clarity.snapshot(CLIENT, Api(429, "Exceeded daily limit"), today=TODAY)
    assert clarity.stored_days(CLIENT) == set()
    assert len(clarity.open_failures(CLIENT)) == 3


def test_a_failure_stops_being_open_once_the_day_is_captured():
    clarity.snapshot(CLIENT, Api(429, ""), today=TODAY)
    assert clarity.open_failures(CLIENT)
    clarity.snapshot(CLIENT, Api(), today=TODAY)
    assert "2026-09-19" not in {f["day"] for f in clarity.open_failures(CLIENT)}


def test_a_rejected_token_says_which_variable_is_wrong():
    refused = clarity.snapshot(CLIENT, Api(401, ""), today=TODAY)
    assert refused.code == "clarity_unauthorized" and clarity.TOKEN_NAME in refused.detail


def test_an_unreadable_reply_is_not_treated_as_an_empty_day():
    api = Api()
    api.__dict__["body"] = None
    broken = clarity.fetch(lambda params: (200, "not json at all"))
    assert not broken and broken.code == "clarity_bad_reply"


# ── the numbers ───────────────────────────────────────────────────────────

def test_metrics_become_one_record_per_page():
    pages = clarity.parse(metrics(sessions=40, dead=5, rage=3))
    page = pages[PAGE]
    assert page.sessions == 40 and page.bot_sessions == 2
    assert page.dead_clicks == 5 and page.rage_clicks == 3
    assert page.scroll_depth == 62.5
    assert page.friction == 8                     # dead + rage + quick-backs


def test_a_row_without_a_url_is_skipped_not_guessed():
    assert clarity.parse([{"metricName": "Traffic",
                           "information": [{"totalSessionCount": 5}]}]) == {}
