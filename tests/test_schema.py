"""Tests for the Finding contract — the rule that keeps skills specific."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from seo_core.schema import (  # noqa: E402
    ChangeRecord,
    Finding,
    Result,
    content_hash,
    dedupe,
    save_findings,
)


def make_finding(**overrides) -> Finding:
    """A well-formed finding. Tests break one field at a time."""
    base = dict(
        skill="content-decay",
        client="denvergaragedoor.com",
        type="decayed_page",
        severity="high",
        source="gsc_wizard",
        url="https://denvergaragedoor.com/spring-repair",
        query="garage door spring repair denver",
        impact_clicks=140.0,
        impact_basis="140 clicks lost vs the same 28 days last year, position 4 to 9",
        confidence="high",
        confidence_reason="year-over-year comparison on 2,400 impressions, no core update in the window",
        effort="m",
        evidence={"clicks_before": 310, "clicks_now": 170, "position_before": 4.1},
        action={"kind": "refresh_section", "heading": "Spring replacement cost",
                "text": "Most Denver spring replacements run $180-$350..."},
        baseline={"clicks_28d": 170, "position": 9.2, "measured_on": "2026-09-16"},
    )
    base.update(overrides)
    return Finding(**base)


# ═══════════════════════════════════════════════════════
#  The contract: no evidence, no action, no finding
# ═══════════════════════════════════════════════════════

def test_well_formed_finding_is_accepted():
    finding = make_finding()
    assert finding.impact_clicks == 140.0
    assert finding.impact_conversions is None


@pytest.mark.parametrize("field_name", ["evidence", "action", "baseline"])
def test_empty_required_dict_is_rejected(field_name):
    with pytest.raises(ValueError, match=field_name):
        make_finding(**{field_name: {}})


def test_vacuous_impact_basis_is_rejected():
    """'important' is not an explanation of how a number was reached."""
    with pytest.raises(ValueError, match="impact_basis"):
        make_finding(impact_basis="important")


def test_vacuous_confidence_reason_is_rejected():
    with pytest.raises(ValueError, match="confidence_reason"):
        make_finding(confidence_reason="חשוב")


def test_short_impact_basis_is_rejected():
    with pytest.raises(ValueError, match="impact_basis"):
        make_finding(impact_basis="lost clicks")


def test_action_without_kind_is_rejected():
    with pytest.raises(ValueError, match="kind"):
        make_finding(action={"text": "something"})


def test_action_with_kind_but_no_payload_is_rejected():
    """A 'kind' alone is a category, not an instruction."""
    with pytest.raises(ValueError, match="ריק מתוכן"):
        make_finding(action={"kind": "refresh_section"})


def test_all_missing_pieces_are_reported_together():
    """One run should surface every gap, not just the first."""
    with pytest.raises(ValueError) as exc:
        make_finding(evidence={}, action={}, impact_basis="seo")
    message = str(exc.value)
    assert "evidence" in message
    assert "action" in message
    assert "impact_basis" in message


@pytest.mark.parametrize(
    "field_name,bad_value",
    [("severity", "urgent"), ("confidence", "certain"),
     ("effort", "xl"), ("source", "vibes")],
)
def test_unknown_vocabulary_is_rejected(field_name, bad_value):
    with pytest.raises(ValueError):
        make_finding(**{field_name: bad_value})


def test_negative_impact_is_rejected():
    with pytest.raises(ValueError, match="impact_clicks"):
        make_finding(impact_clicks=-5)


# ═══════════════════════════════════════════════════════
#  Ranking
# ═══════════════════════════════════════════════════════

def test_conversions_outrank_clicks_when_available():
    clicks_only = make_finding(impact_clicks=100)
    with_conversions = make_finding(impact_clicks=100, impact_conversions=4)
    assert with_conversions.priority() > clicks_only.priority()


def test_missing_conversion_data_does_not_invent_a_number():
    """A client without GA4 is ranked on clicks, not on a guess."""
    finding = make_finding(impact_conversions=None)
    assert finding.priority() == pytest.approx(140.0 / 2.5)


def test_low_confidence_is_discounted():
    sure = make_finding(confidence="high")
    unsure = make_finding(
        confidence="low",
        confidence_reason="only 60 impressions, and a core update landed mid-window",
    )
    assert unsure.priority() < sure.priority()


def test_effort_lowers_priority():
    easy = make_finding(effort="s")
    hard = make_finding(effort="l")
    assert easy.priority() > hard.priority()


# ═══════════════════════════════════════════════════════
#  Dedupe and persistence
# ═══════════════════════════════════════════════════════

def test_dedupe_keeps_the_stronger_of_two_reports():
    weak = make_finding(impact_clicks=10, skill="onpage-optimizer")
    strong = make_finding(impact_clicks=400, skill="content-decay")
    result = dedupe([weak, strong])
    assert len(result) == 1
    assert result[0].skill == "content-decay"


def test_dedupe_keeps_distinct_problems_apart():
    a = make_finding(type="decayed_page")
    b = make_finding(type="missing_h2")
    assert len(dedupe([a, b])) == 2


def test_saved_findings_are_ordered_and_readable(tmp_path):
    path = save_findings(
        [make_finding(impact_clicks=5), make_finding(impact_clicks=500, type="other")],
        tmp_path / "findings.json",
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["count"] == 2
    assert payload["findings"][0]["impact_clicks"] == 500
    # Hebrew must survive the round trip unescaped.
    assert "\\u" not in path.read_text(encoding="utf-8")


# ═══════════════════════════════════════════════════════
#  Result
# ═══════════════════════════════════════════════════════

def test_result_is_truthy_on_success():
    assert Result.success("wrote", "הדף עודכן")
    assert not Result.failure("conflict", "מישהו ערך בינתיים")


def test_failure_can_declare_itself_unrecoverable():
    result = Result.failure("conflict", "עריכה מקבילה", recoverable=False)
    assert result.recoverable is False
    assert result.code == "conflict"


# ═══════════════════════════════════════════════════════
#  ChangeRecord — the rollback safety gate
# ═══════════════════════════════════════════════════════

def make_change(**overrides) -> ChangeRecord:
    base = dict(
        change_id="chg_001", plan_id="plan_001", skill="content-decay",
        client="denvergaragedoor.com",
        url="https://denvergaragedoor.com/spring-repair", post_id=42,
        before_hash="abc123", inverse={"post_content": "<p>original</p>"},
        backup_ref="backups/chg_001.json", backup_verified=True,
    )
    base.update(overrides)
    return ChangeRecord(**base)


def test_auto_rollback_requires_a_verified_backup():
    """Restoring from a backup we never proved restorable is not a rollback."""
    assert make_change(backup_verified=True).safe_to_auto_rollback is True
    assert make_change(backup_verified=False).safe_to_auto_rollback is False


def test_auto_rollback_requires_an_inverse():
    assert make_change(inverse={}).safe_to_auto_rollback is False


def test_change_starts_planned():
    assert make_change().status == "planned"
    assert make_change().applied_at is None


def test_change_serialises_with_iso_timestamp():
    applied = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)
    d = make_change(applied_at=applied, status="applied").to_dict()
    assert d["applied_at"] == "2026-09-16T12:00:00+00:00"


# ═══════════════════════════════════════════════════════
#  Concurrent-edit detection
# ═══════════════════════════════════════════════════════

def test_hash_is_stable_across_calls():
    assert content_hash("<p>a</p>", {"t": "x"}) == content_hash("<p>a</p>", {"t": "x"})


def test_hash_changes_when_meta_changes_but_content_does_not():
    """The case modified_gmt misses: body untouched, SEO title edited."""
    before = content_hash("<p>body</p>", {"_yoast_wpseo_title": "Old"})
    after = content_hash("<p>body</p>", {"_yoast_wpseo_title": "New"})
    assert before != after
