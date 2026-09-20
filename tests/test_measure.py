"""Observed is not the same as attributable, and the code has to keep them apart.

Passing tests are not a measure of SEO success. What is measurable is whether
a claim about a change is supported by a control — and whether the toolkit
refuses to make the claim when it is not.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from seo_core.change_guard import ledger, measure          # noqa: E402
from seo_core.schema import ChangeRecord                   # noqa: E402

BIG = {"clicks": 100, "impressions": 4000, "position": 8.0}


def window(clicks, impressions=4000, position=8.0):
    return {"clicks": clicks, "impressions": impressions, "position": position}


def control(before, after):
    return {"clicks_before": before, "clicks_after": after}


# ── the arithmetic ────────────────────────────────────────────────────────

def test_what_moved_is_reported_before_anything_is_claimed():
    seen = measure.observe(window(100), window(130, position=6.0))
    assert seen["clicks"]["relative"] == 0.3
    assert seen["position"]["places_gained"] == 2.0      # lower is better
    assert seen["ctr"]["before"] == 0.025


def test_position_and_ctr_are_reported_separately():
    # A page that rose two places and kept its CTR earned the clicks from the
    # ranking, not from the title. Merging them would hide which one happened.
    seen = measure.observe(window(100), window(125, position=6.0))
    assert seen["position"]["places_gained"] == 2.0
    assert seen["ctr"]["relative"] is not None


# ── attribution ───────────────────────────────────────────────────────────

def test_without_a_control_nothing_is_attributed():
    result = measure.assess(window(100), window(140))
    assert result.verdict == "improved"
    assert result.attributable == "unknown"
    assert "ביקורת" in result.reason


def test_a_site_that_rose_just_as_much_is_the_simpler_explanation():
    # The site rose 20%, the page rose 18%: nothing here belongs to the change.
    result = measure.assess(window(100), window(118), control(1000, 1200))
    assert result.verdict == "improved" and result.attributable == "no"
    assert "לא מיוחדת לדף" in result.reason


def test_a_page_that_beat_the_site_with_a_steady_position_is_attributable():
    result = measure.assess(window(100), window(160, position=7.9), control(1000, 1030))
    assert result.attributable == "yes"


def test_a_position_that_moved_too_takes_the_credit_back_to_partly():
    result = measure.assess(window(100), window(160, position=5.0), control(1000, 1030))
    assert result.attributable == "partly"
    assert "דירוג" in result.reason


def test_mostly_hidden_clicks_cap_the_claim_at_partly():
    result = measure.assess(window(100), window(160, position=7.9), control(1000, 1030),
                            completeness={"anonymised_click_share": 0.61})
    assert result.attributable == "partly" and "61%" in result.reason


def test_a_sample_too_small_to_read_stays_inconclusive():
    result = measure.assess(window(2, impressions=40), window(4, impressions=50),
                            control(1000, 1030))
    assert result.verdict == "inconclusive" and not result.conclusive
    assert result.attributable == "unknown"


def test_a_page_that_did_not_move_says_so():
    assert measure.assess(window(100), window(103), control(1000, 1010)).verdict == "no_change"


def test_a_decline_is_measured_the_same_way():
    result = measure.assess(window(100), window(60), control(1000, 1010))
    assert result.verdict == "declined" and result.attributable in ("yes", "partly")


# ── the report ────────────────────────────────────────────────────────────

def change_entry():
    return {
        "url": "https://example.com/springs", "skill": "ctr-titles",
        "applied_at": "2026-09-20T10:00:00+00:00",
        "reason": {"type": "low_ctr", "instruction": "שכתוב טייטל",
                   "impact_basis": "במיקום 6 הדף מקבל 1% במקום 4% הצפויים"},
        "baseline": {"clicks": 100, "impressions": 4000, "position": 8.0,
                     "window": {"start": "2026-08-20", "end": "2026-09-16"}},
    }


def test_the_report_separates_what_was_seen_from_what_it_proves():
    result = measure.assess(window(100), window(160, position=7.9), control(1000, 1030))
    lines = measure.report_lines(change_entry(), result)
    body = "\n".join(lines)
    assert any(line.startswith("נצפה:") for line in lines)
    assert any(line.startswith("ניתן לייחס לפעולה:") for line in lines)
    # The five things the owner asked to keep, all present.
    for needed in ("דף:", "פעולה:", "סיבה:", "בוצע:", "בסיס:"):
        assert needed in body
    assert "בזכות" not in body                      # never a causal boast


def test_an_unattributable_report_says_so_in_words():
    lines = measure.report_lines(change_entry(), measure.assess(window(100), window(140)))
    assert "לא ניתן לקבוע" in "\n".join(lines)


# ── the ledger ────────────────────────────────────────────────────────────

def test_checkpoints_are_28_56_and_84_days():
    assert ledger.CHECKPOINT_DAYS == (28, 56, 84)
    applied = datetime(2026, 9, 20, tzinfo=timezone.utc)
    scheduled = ledger.schedule_checkpoints(applied)
    assert [c["day"] for c in scheduled] == [28, 56, 84]
    assert scheduled[0]["due_at"].startswith("2026-10-18")


def record(tmp_path, **extra):
    change = ChangeRecord(
        change_id="c1", plan_id="p1", skill="ctr-titles", client="example.com",
        url="https://example.com/springs", post_id=7, before_hash="abc",
        inverse={"content": "old"}, backup_ref="b1", backup_verified=True,
        status="applied", applied_at=datetime.now(timezone.utc) - timedelta(days=30),
        checkpoints=ledger.schedule_checkpoints(
            datetime.now(timezone.utc) - timedelta(days=30)),
        **extra)
    ledger.record(change, tmp_path)
    return change


def test_the_five_things_are_on_a_new_record(tmp_path):
    record(tmp_path, reason={"type": "low_ctr", "impact_basis": "why"},
           baseline={"clicks": 100, "window": {"start": "2026-08-20", "end": "2026-09-16"}})
    [entry] = ledger.load(tmp_path)
    assert entry["before_hash"] and entry["applied_at"]          # state, date
    assert entry["reason"]["impact_basis"] == "why"              # action and reason
    assert entry["baseline"]["window"]["start"]                  # baseline metrics
    assert [c["day"] for c in entry["checkpoints"]] == [28, 56, 84]


def test_an_inconclusive_read_leaves_the_follow_up_open(tmp_path):
    record(tmp_path)
    done = ledger.complete_checkpoint("c1", 28, "inconclusive", {"clicks": 4}, tmp_path)
    assert done and done.data["follow_up_open"] is True
    [entry] = ledger.load(tmp_path)
    assert [c["status"] for c in entry["checkpoints"]] == ["inconclusive", "pending", "pending"]
    assert ledger.next_checkpoint(entry)["day"] == 56


def test_a_conclusive_read_closes_the_later_ones(tmp_path):
    record(tmp_path)
    done = ledger.complete_checkpoint("c1", 28, "improved", {"clicks": 160}, tmp_path)
    assert done.data["follow_up_open"] is False
    [entry] = ledger.load(tmp_path)
    assert [c["status"] for c in entry["checkpoints"]] == ["improved", "not_needed", "not_needed"]
    assert ledger.next_checkpoint(entry) is None


def test_a_record_written_before_these_fields_existed_still_loads(tmp_path):
    import json
    old = {"change_id": "old1", "skill": "onpage-optimizer", "status": "applied",
           "applied_at": "2026-08-01T00:00:00+00:00",
           "checkpoints": [{"due_at": "2026-08-29T00:00:00+00:00", "day": 14,
                            "status": "pending", "measured": None}]}
    (tmp_path / ledger.LEDGER_NAME).write_text(
        json.dumps({"changes": [old]}), encoding="utf-8")
    [entry] = ledger.load(tmp_path)
    assert entry["change_id"] == "old1"
    assert entry.get("reason", {}) == {} and entry.get("baseline", {}) == {}
    assert ledger.next_checkpoint(entry)["day"] == 14      # its old schedule is honoured
