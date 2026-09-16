"""Tests for the six gates, backups, and the rollback decision."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from seo_core.change_guard import checks, ledger, plan as plan_mod, risk, rollback  # noqa: E402
from seo_core.schema import ChangeRecord, Result  # noqa: E402
from seo_core.wp import backup as wp_backup  # noqa: E402
from seo_core.wp.content import ELEMENTOR_DATA_KEY, PostContent  # noqa: E402

NOW = datetime(2026, 9, 16, tzinfo=timezone.utc)


# ═══════════════════════════════════════════════════════
#  Gate 1 — risk
# ═══════════════════════════════════════════════════════

ROWS = [
    {"query": "garage door spring repair", "clicks": 120, "impressions": 3000, "position": 3.2},
    {"query": "emergency garage door denver", "clicks": 0, "impressions": 800, "position": 12.0},
    {"query": "garage door brands", "clicks": 0, "impressions": 40, "position": 68.0},
]


def test_protected_queries_pick_up_clicks_and_near_misses():
    protected = risk.protected_queries(ROWS)
    assert {q.query for q in protected} == {
        "garage door spring repair", "emergency garage door denver"
    }


def test_protected_queries_are_ordered_by_what_is_at_stake():
    assert risk.protected_queries(ROWS)[0].clicks == 120


def test_pure_addition_is_never_risky():
    """Adding a paragraph cannot take a phrase away from the page."""
    report = risk.assess("", risk.protected_queries(ROWS))
    assert report.level == "green"
    assert report.needs_double_approval is False


def test_removing_a_ranking_phrase_is_red():
    report = risk.assess(
        "Our garage door spring repair service", risk.protected_queries(ROWS)
    )
    assert report.level == "red"
    assert report.needs_double_approval is True
    assert any("garage door spring repair" in r for r in report.reasons)


def test_removing_a_zero_click_phrase_is_only_amber():
    report = risk.assess(
        "emergency garage door denver", risk.protected_queries(ROWS)
    )
    assert report.level == "amber"


def test_the_query_being_targeted_is_not_counted_against_itself():
    """Rewriting a heading to serve a query is not a threat to that query."""
    report = risk.assess(
        "Our garage door spring repair service",
        risk.protected_queries(ROWS),
        target_query="garage door spring repair",
    )
    assert report.level == "green"


def test_partial_overlap_does_not_raise_an_alarm():
    """Touching one word of a phrase is not removing the phrase."""
    report = risk.assess("our repair team", risk.protected_queries(ROWS))
    assert report.level == "green"


def test_stopwords_alone_do_not_create_risk():
    assert risk.assess("the and for to of", risk.protected_queries(ROWS)).level == "green"


# ═══════════════════════════════════════════════════════
#  Gates 2 and 3 — plan and approval
# ═══════════════════════════════════════════════════════

def make_plan(payload=None, after="After text here") -> plan_mod.ChangePlan:
    result = plan_mod.compose(
        plan_id="plan_001", client="x.com", skill="content-decay",
        url="https://x.com/page", post_id=7, post_type="pages", builder="gutenberg",
        summary="הוספת פסקה", rationale="הדף איבד 140 קליקים",
        payload=payload or {"content": "<p>after</p>"},
        inverse={"content": "<p>before</p>"},
        before_text="Before text here", after_text=after, before_hash="abc123",
        risk=risk.assess("", risk.protected_queries(ROWS)),
    )
    assert result, result.detail
    return result.data["plan"]


def test_plan_without_an_inverse_is_refused():
    """No way back means no way forward."""
    result = plan_mod.compose(
        plan_id="p", client="x", skill="s", url="u", post_id=1, post_type="pages",
        builder="gutenberg", summary="s", rationale="r",
        payload={"content": "x"}, inverse={},
        before_text="a", after_text="b", before_hash="h",
        risk=risk.RiskReport("green"),
    )
    assert not result
    assert result.code == "no_inverse"
    assert result.recoverable is False


def test_plan_that_changes_nothing_is_refused():
    result = plan_mod.compose(
        plan_id="p", client="x", skill="s", url="u", post_id=1, post_type="pages",
        builder="gutenberg", summary="s", rationale="r",
        payload={"content": "x"}, inverse={"content": "y"},
        before_text="same", after_text="same", before_hash="h",
        risk=risk.RiskReport("green"),
    )
    assert not result
    assert result.code == "no_change"


def test_publishing_without_approval_is_refused(tmp_path):
    plan = make_plan()
    result = plan_mod.is_approved(plan, tmp_path)
    assert not result
    assert result.code == "not_approved"


def test_approval_permits_publishing(tmp_path):
    plan = make_plan()
    plan_mod.approve(plan, tmp_path)
    assert plan_mod.is_approved(plan, tmp_path)


def test_approval_does_not_carry_over_to_different_copy(tmp_path):
    """What he approved must be what gets written."""
    original = make_plan()
    plan_mod.approve(original, tmp_path)

    revised = make_plan(payload={"content": "<p>completely different</p>"})
    result = plan_mod.is_approved(revised, tmp_path)
    assert not result
    assert result.code == "plan_changed"


def test_plan_document_names_the_risk_and_the_diff(tmp_path):
    plan = make_plan()
    doc, raw = plan_mod.save(plan, tmp_path)
    text = doc.read_text(encoding="utf-8")
    assert "## ה-diff המלא" in text
    assert "Before text here" in text
    assert json.loads(raw.read_text(encoding="utf-8"))["plan_id"] == "plan_001"


def test_red_risk_plan_demands_double_approval_in_writing(tmp_path):
    result = plan_mod.compose(
        plan_id="plan_red", client="x", skill="s", url="u", post_id=1,
        post_type="pages", builder="gutenberg", summary="s", rationale="r",
        payload={"content": "x"}, inverse={"content": "y"},
        before_text="a", after_text="b", before_hash="h",
        risk=risk.assess("Our garage door spring repair service", risk.protected_queries(ROWS)),
    )
    doc, _ = plan_mod.save(result.data["plan"], tmp_path)
    assert "אישור כפול" in doc.read_text(encoding="utf-8")


# ═══════════════════════════════════════════════════════
#  Backups
# ═══════════════════════════════════════════════════════

def elementor_content(with_data=True) -> PostContent:
    meta = {ELEMENTOR_DATA_KEY: json.dumps([{"id": "a", "elType": "section"}])} if with_data else {}
    return PostContent(
        post_id=5, post_type="pages", builder="elementor",
        raw_content="<p>stale</p>", elementor_tree=[{"id": "a"}], meta=meta,
    )


def test_elementor_backup_keeps_the_widget_tree():
    made = wp_backup.create(elementor_content(), "https://x.com/p", "bk1")
    assert ELEMENTOR_DATA_KEY in made.meta
    assert wp_backup.verify(made)


def test_elementor_backup_without_the_tree_fails_verification():
    """It looks complete and restores nothing anyone can see."""
    made = wp_backup.create(elementor_content(with_data=False), "https://x.com/p", "bk2")
    result = wp_backup.verify(made)
    assert not result
    assert result.recoverable is False
    assert any("_elementor_data" in p for p in result.data["problems"])


def test_elementor_restore_targets_meta_not_post_content():
    made = wp_backup.create(elementor_content(), "https://x.com/p", "bk3")
    assert "content" not in made.restore_payload()
    assert "meta" in made.restore_payload()


def test_classic_backup_restores_post_content():
    content = PostContent(post_id=6, post_type="posts", builder="classic",
                          raw_content="<p>original</p>", meta={})
    made = wp_backup.create(content, "https://x.com/p", "bk4")
    assert wp_backup.verify(made)
    assert made.restore_payload()["content"] == "<p>original</p>"


def test_empty_backup_fails_verification():
    content = PostContent(post_id=7, post_type="posts", builder="classic",
                          raw_content="   ", meta={})
    assert not wp_backup.verify(wp_backup.create(content, "u", "bk5"))


def test_backup_round_trips_through_disk(tmp_path):
    made = wp_backup.create(elementor_content(), "https://x.com/p", "bk6")
    path = wp_backup.save(made, tmp_path)
    loaded = wp_backup.load(path)
    assert loaded
    assert loaded.data["backup"].meta == made.meta


def test_corrupt_backup_file_is_unrecoverable(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{ broken", encoding="utf-8")
    result = wp_backup.load(path)
    assert not result
    assert result.recoverable is False


def test_missing_backup_file_is_unrecoverable(tmp_path):
    assert wp_backup.load(tmp_path / "nope.json").recoverable is False


class FakeClient:
    def __init__(self, result): self.result = result; self.calls = []
    def update_post(self, post_id, **kw): self.calls.append((post_id, kw)); return self.result


def test_failed_restore_says_stop_rather_than_retry():
    """A failed restore is the worst state there is; retrying blindly is worse."""
    made = wp_backup.create(elementor_content(), "u", "bk7")
    client = FakeClient(Result.failure("forbidden", "אין הרשאה"))
    result = wp_backup.restore(client, made)
    assert not result
    assert result.recoverable is False
    assert "ידנית" in result.detail


# ═══════════════════════════════════════════════════════
#  Gate 5 — post-publish checks
# ═══════════════════════════════════════════════════════

PAGE = """<html><head>
<link rel="canonical" href="https://x.com/page"/>
<meta name="robots" content="index, follow"/>
<script type="application/ld+json">{"@type":"Article"}</script>
</head><body>
<h1>Title</h1><h2>Spring replacement cost</h2>
<p>Most Denver springs run $180-$350 including labour and a short warranty.</p>
<form action="/contact"><input name="email"/></form>
</body></html>"""


def fetcher(pages: dict[str, tuple[int, str]]):
    def fetch(url: str):
        if url not in pages:
            raise AssertionError(f"unexpected fetch: {url}")
        return pages[url]
    return fetch


def test_a_healthy_page_passes_everything():
    base = checks.Baseline.capture(PAGE)
    report = checks.run("u", "Most Denver springs run $180-$350",
                        base, fetcher({"u": (200, PAGE)}))
    assert report.passed, [c.detail for c in report.checks if not c.passed]
    assert report.should_roll_back is False


def test_a_500_stops_everything_else():
    base = checks.Baseline.capture(PAGE)
    report = checks.run("u", "anything", base, fetcher({"u": (500, "")}))
    assert report.should_roll_back is True
    assert len(report.checks) == 1


def test_copy_that_never_rendered_is_a_breaking_failure():
    base = checks.Baseline.capture(PAGE)
    report = checks.run("u", "Text that is not on the page at all",
                        base, fetcher({"u": (200, PAGE)}))
    assert report.should_roll_back is True


def test_a_vanished_form_is_a_breaking_failure():
    base = checks.Baseline.capture(PAGE)
    broken = PAGE.replace('<form action="/contact"><input name="email"/></form>', "")
    report = checks.run("u", "Most Denver springs", base, fetcher({"u": (200, broken)}))
    assert report.should_roll_back is True
    assert any("טפסים" in c.name for c in report.breaking_failures)


def test_a_collapsed_layout_is_a_breaking_failure():
    base = checks.Baseline.capture(PAGE)
    stub = "<html><body><h1>Title</h1></body></html>"
    report = checks.run("u", "Title", base, fetcher({"u": (200, stub)}))
    assert report.should_roll_back is True


def test_becoming_noindex_is_a_breaking_failure():
    base = checks.Baseline.capture(PAGE)
    hidden = PAGE.replace('content="index, follow"', 'content="noindex, follow"')
    report = checks.run("u", "Most Denver springs", base, fetcher({"u": (200, hidden)}))
    assert report.should_roll_back is True


def test_a_moved_canonical_is_a_breaking_failure():
    base = checks.Baseline.capture(PAGE)
    moved = PAGE.replace("https://x.com/page", "https://x.com/other")
    report = checks.run("u", "Most Denver springs", base, fetcher({"u": (200, moved)}))
    assert report.should_roll_back is True


def test_broken_schema_warns_but_does_not_roll_back():
    """A lost rich result is not worth reverting a page over."""
    base = checks.Baseline.capture(PAGE)
    bad = PAGE.replace('{"@type":"Article"}', "{not json")
    report = checks.run("u", "Most Denver springs", base, fetcher({"u": (200, bad)}))
    assert report.passed is False
    assert report.should_roll_back is False
    assert report.warnings


def test_heading_level_skip_warns_but_does_not_roll_back():
    base = checks.Baseline.capture(PAGE)
    skipped = PAGE.replace("<h2>Spring replacement cost</h2>", "<h4>Spring replacement cost</h4>")
    report = checks.run("u", "Most Denver springs", base, fetcher({"u": (200, skipped)}))
    assert report.should_roll_back is False


def test_a_broken_internal_link_is_a_breaking_failure():
    base = checks.Baseline.capture(PAGE)
    report = checks.run("u", "Most Denver springs", base,
                        fetcher({"u": (200, PAGE), "/dead": (404, "")}),
                        internal_links=["/dead"])
    assert report.should_roll_back is True


def test_as_result_folds_into_a_rollback_signal():
    base = checks.Baseline.capture(PAGE)
    ok = checks.as_result(checks.run("u", "Most Denver springs", base, fetcher({"u": (200, PAGE)})))
    assert ok
    bad = checks.as_result(checks.run("u", "nope", base, fetcher({"u": (200, PAGE)})))
    assert not bad and bad.recoverable is True


# ═══════════════════════════════════════════════════════
#  Ledger
# ═══════════════════════════════════════════════════════

def make_change(**over) -> ChangeRecord:
    base = dict(
        change_id="chg1", plan_id="plan_001", skill="content-decay", client="x.com",
        url="https://x.com/p", post_id=7, before_hash="abc",
        inverse={"content": "<p>before</p>"}, backup_ref="bk1.json",
        backup_verified=True, status="applied",
        checkpoints=ledger.schedule_checkpoints(NOW),
    )
    base.update(over)
    return ChangeRecord(**base)


def test_recording_is_idempotent(tmp_path):
    """Re-running a skill must not create a second entry for the same change."""
    ledger.record(make_change(), tmp_path)
    ledger.record(make_change(), tmp_path)
    assert len(ledger.load(tmp_path)) == 1


def test_checkpoints_land_at_14_28_and_56_days():
    assert [c["day"] for c in ledger.schedule_checkpoints(NOW)] == [14, 28, 56]


def test_checkpoints_are_not_due_before_their_time(tmp_path):
    ledger.record(make_change(), tmp_path)
    assert ledger.due_checkpoints(tmp_path, NOW + timedelta(days=13)) == []


def test_checkpoint_comes_due(tmp_path):
    ledger.record(make_change(), tmp_path)
    due = ledger.due_checkpoints(tmp_path, NOW + timedelta(days=15))
    assert len(due) == 1
    assert due[0]["checkpoint"]["day"] == 14


def test_rolled_back_changes_are_not_measured(tmp_path):
    ledger.record(make_change(status="rolled_back"), tmp_path)
    assert ledger.due_checkpoints(tmp_path, NOW + timedelta(days=60)) == []


def test_completed_checkpoint_does_not_come_due_again(tmp_path):
    ledger.record(make_change(), tmp_path)
    ledger.complete_checkpoint("chg1", 14, "inconclusive", {"clicks": 5}, tmp_path)
    due = ledger.due_checkpoints(tmp_path, NOW + timedelta(days=15))
    assert due == []


def test_status_change_is_noted(tmp_path):
    ledger.record(make_change(), tmp_path)
    ledger.set_status("chg1", "verified", tmp_path, note="עלה 40 קליקים")
    entry = ledger.get("chg1", tmp_path).data["change"]
    assert entry["status"] == "verified"
    assert any("40 קליקים" in n for n in entry["notes"])


def test_corrupt_ledger_does_not_crash_the_run(tmp_path):
    (tmp_path / ledger.LEDGER_NAME).write_text("{ broken", encoding="utf-8")
    assert ledger.load(tmp_path) == []


# ═══════════════════════════════════════════════════════
#  The rollback decision
# ═══════════════════════════════════════════════════════

def test_technical_failure_with_a_verified_backup_rolls_back():
    decision = rollback.on_technical_failure(make_change(), ["הטופס נעלם"])
    assert decision.action == "auto_rollback"


def test_technical_failure_without_a_verified_backup_halts():
    decision = rollback.on_technical_failure(make_change(backup_verified=False), ["500"])
    assert decision.action == "halt"
    assert decision.needs_human


def test_technical_failure_without_an_inverse_halts():
    assert rollback.on_technical_failure(make_change(inverse={}), ["500"]).action == "halt"


def test_a_concurrent_edit_always_halts():
    """Their work is not ours to discard."""
    decision = rollback.on_concurrent_edit(make_change(), "def456")
    assert decision.action == "halt"
    assert "מישהו ערך" in decision.reason


def test_a_ranking_drop_never_rolls_back_automatically():
    decision = rollback.on_performance_change(
        protected_deltas={"garage door repair": {"position_delta": 4.0, "clicks_pct": -0.55}},
        site_trend_pct=0.01, control_trend_pct=0.0,
        algorithm_update_in_window=False, days_elapsed=28,
    )
    assert decision.action == "report_only"
    assert decision.is_automatic is False


def test_a_site_wide_decline_is_not_blamed_on_this_change():
    decision = rollback.on_performance_change(
        protected_deltas={"q": {"position_delta": 3.0, "clicks_pct": -0.40}},
        site_trend_pct=-0.45, control_trend_pct=-0.42,
        algorithm_update_in_window=False, days_elapsed=28,
    )
    assert decision.action == "keep_watching"


def test_a_page_that_fell_much_further_than_the_site_is_still_attributed():
    """Excess loss is what separates 'the site slipped' from 'we broke this page'."""
    decision = rollback.on_performance_change(
        protected_deltas={"q": {"position_delta": 6.0, "clicks_pct": -0.85}},
        site_trend_pct=-0.25, control_trend_pct=-0.22,
        algorithm_update_in_window=False, days_elapsed=28,
    )
    assert decision.action == "report_only"


def test_an_algorithm_update_lowers_confidence_to_low():
    decision = rollback.on_performance_change(
        protected_deltas={"q": {"position_delta": 5.0, "clicks_pct": -0.60}},
        site_trend_pct=0.0, control_trend_pct=0.0,
        algorithm_update_in_window=True, days_elapsed=28,
    )
    assert decision.action == "report_only"
    assert decision.confidence == "low"


def test_it_is_too_early_to_judge_before_fourteen_days():
    decision = rollback.on_performance_change(
        protected_deltas={"q": {"position_delta": 9.0, "clicks_pct": -0.9}},
        site_trend_pct=0.0, control_trend_pct=0.0,
        algorithm_update_in_window=False, days_elapsed=3,
    )
    assert decision.action == "keep_watching"
    assert decision.confidence == "low"


def test_small_movement_is_not_a_finding():
    decision = rollback.on_performance_change(
        protected_deltas={"q": {"position_delta": 0.4, "clicks_pct": -0.03}},
        site_trend_pct=0.0, control_trend_pct=0.0,
        algorithm_update_in_window=False, days_elapsed=28,
    )
    assert decision.action == "keep_watching"


def test_execute_refuses_anything_but_an_automatic_decision():
    """The narrow signature is the point: it cannot be argued into a revert."""
    decision = rollback.Decision("report_only", "ירידה בדירוג")
    result = rollback.execute(decision, make_change(), lambda: Result.success("x", "y"))
    assert not result
    assert result.code == "not_automatic"


def test_execute_reports_a_failed_rollback_as_unrecoverable():
    decision = rollback.on_technical_failure(make_change(), ["500"])
    result = rollback.execute(
        decision, make_change(), lambda: Result.failure("forbidden", "אין הרשאה")
    )
    assert not result
    assert result.recoverable is False
    assert "מיידית" in result.detail


def test_execute_performs_a_safe_rollback():
    decision = rollback.on_technical_failure(make_change(), ["500"])
    result = rollback.execute(
        decision, make_change(), lambda: Result.success("restored", "שוחזר")
    )
    assert result


# ═══════════════════════════════════════════════════════
#  The approval CLI the plan document points at
# ═══════════════════════════════════════════════════════

def test_saved_plan_round_trips(tmp_path):
    original = make_plan()
    plan_mod.save(original, tmp_path)
    loaded = plan_mod.load("plan_001", tmp_path)
    assert loaded
    assert loaded.data["plan"].fingerprint == original.fingerprint


def test_loading_a_missing_plan_is_reported(tmp_path):
    assert not plan_mod.load("nope", tmp_path)


def test_cli_approves_a_green_plan(tmp_path):
    plan = make_plan()
    plan_mod.save(plan, tmp_path)
    assert plan_mod.main(["approve", "plan_001", "--dir", str(tmp_path)]) == 0
    assert plan_mod.is_approved(plan, tmp_path)


def test_cli_show_does_not_approve(tmp_path):
    plan = make_plan()
    plan_mod.save(plan, tmp_path)
    assert plan_mod.main(["show", "plan_001", "--dir", str(tmp_path)]) == 0
    assert not plan_mod.is_approved(plan, tmp_path)


def test_cli_on_a_red_plan_demands_a_typed_confirmation(tmp_path, monkeypatch):
    composed = plan_mod.compose(
        plan_id="plan_red", client="x", skill="s", url="u", post_id=1,
        post_type="pages", builder="gutenberg", summary="s", rationale="r",
        payload={"content": "x"}, inverse={"content": "y"},
        before_text="a", after_text="b", before_hash="h",
        risk=risk.assess("Our garage door spring repair service", risk.protected_queries(ROWS)),
    )
    red = composed.data["plan"]
    plan_mod.save(red, tmp_path)

    monkeypatch.setattr("builtins.input", lambda *_: "לא")
    assert plan_mod.main(["approve", "plan_red", "--dir", str(tmp_path)]) == 1
    assert not plan_mod.is_approved(red, tmp_path)

    monkeypatch.setattr("builtins.input", lambda *_: "כן")
    assert plan_mod.main(["approve", "plan_red", "--dir", str(tmp_path)]) == 0
    assert plan_mod.is_approved(red, tmp_path)
