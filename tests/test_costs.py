"""What a run spent, with the measured part kept apart from the guessed part.

There is no cap and no per-call approval — that was decided deliberately. The
obligation that replaces them is that every run ends by saying what it spent,
including a run that fell over halfway.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from seo_core import costs                            # noqa: E402


@pytest.fixture
def run(tmp_path, monkeypatch):
    monkeypatch.setenv("SEO_HOME", str(tmp_path))
    return costs.RunCosts(client="example.com", skill="competitor-cta")


def test_a_reported_cost_is_used_over_our_guess(run):
    call = run.record("dataforseo", "serp/google/organic/live/advanced",
                      {"cost": 0.0049})
    assert call.measured and call.cost == 0.0049
    assert run.actual == 0.0049 and run.estimated == 0


def test_a_reply_without_a_cost_is_estimated_and_labelled(run):
    # This is what a call through the MCP connector looks like: it works, and
    # it never says what it cost.
    call = run.record("dataforseo", "serp/google/organic/live/advanced", {"tasks": []})
    assert not call.measured and call.cost == costs.ESTIMATES[
        "serp/google/organic/live/advanced"]
    assert run.actual == 0 and run.estimated > 0


def test_the_two_numbers_are_never_added_into_one(run):
    run.record("dataforseo", "backlinks/summary", {"cost": 0.02})
    run.record("dataforseo", "serp/google/organic/live/advanced", None)
    lines = "\n".join(run.summary_lines())
    assert "עלות בפועל: $0.0200" in lines
    assert "אומדן נוסף" in lines and "הערכה" in lines


def test_a_run_that_bought_nothing_says_so(run):
    assert run.summary_lines() == ["עלות: לא בוצעו קריאות בתשלום"]


def test_the_summary_survives_a_run_that_failed_halfway(run):
    run.record("dataforseo", "serp/google/organic/live/advanced", {"cost": 0.0049})
    # No "finish" step exists on purpose: the numbers are readable at any moment.
    assert "$0.0049" in "\n".join(run.summary_lines())


# ── loop guards, which are not budgets ────────────────────────────────────

def test_the_same_request_is_not_paid_for_twice_in_one_run(run):
    key = "serp:back facial sarasota:mobile"
    assert run.cached(key) is None
    run.remember(key, {"items": []})
    assert run.cached(key) == {"items": []}


def test_a_runaway_loop_is_stopped_and_the_spend_reported(run):
    for _ in range(costs.MAX_CALLS_PER_RUN):
        run.record("dataforseo", "serp/google/organic/live/advanced", {"cost": 0.001})
    stopped = run.may_call()
    assert not stopped and stopped.code == "call_guard_tripped"
    assert not stopped.recoverable
    assert run.actual > 0                       # and the bill is still legible


def test_normal_use_is_never_blocked(run):
    for _ in range(20):
        run.record("dataforseo", "serp/google/organic/live/advanced", {"cost": 0.001})
    assert run.may_call()


# ── the log on disk ───────────────────────────────────────────────────────

def test_a_run_is_appended_to_the_clients_cost_log(run, tmp_path):
    run.record("dataforseo", "backlinks/summary", {"cost": 0.02})
    path = run.save()
    stored = json.loads(path.read_text(encoding="utf-8"))["runs"]
    assert len(stored) == 1 and stored[0]["actual"] == 0.02
    assert stored[0]["skill"] == "competitor-cta"

    second = costs.RunCosts(client="example.com", skill="ai-visibility")
    second.record("dataforseo", "ai_optimization/llm_mentions", None)
    second.save()
    assert len(json.loads(path.read_text(encoding="utf-8"))["runs"]) == 2


def test_the_month_to_date_total_keeps_the_split(run):
    run.record("dataforseo", "backlinks/summary", {"cost": 0.02})
    run.record("dataforseo", "serp/google/organic/live/advanced", None)
    run.save()
    total = costs.month_to_date("example.com")
    assert total["actual"] == 0.02 and total["estimated"] > 0 and total["runs"] == 1


def test_an_unknown_endpoint_still_gets_a_price(run):
    assert costs.estimate_for("something/new") == costs.FALLBACK_ESTIMATE
