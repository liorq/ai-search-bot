"""Tests for decay classification — the part that decides whether to act at all."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from seo_core.sources.gsc_source import (  # noqa: E402
    PageWindow,
    classify,
    load_export,
    to_finding,
)

URL = "https://x.com/spring-repair"


def window(clicks, impressions, position, queries=None) -> PageWindow:
    return PageWindow(
        url=URL, clicks=clicks, impressions=impressions, position=position,
        queries=queries or [],
    )


# ═══════════════════════════════════════════════════════
#  What is not decay
# ═══════════════════════════════════════════════════════

def test_a_growing_page_is_not_decay():
    assert classify(window(400, 5000, 3.0), window(300, 4800, 3.2)) is None


def test_a_small_drop_is_not_decay():
    assert classify(window(270, 4800, 3.3), window(300, 5000, 3.2)) is None


def test_a_page_too_small_to_read_is_skipped():
    """Percentages on five clicks are noise wearing a trend's clothes."""
    assert classify(window(1, 90, 20.0), window(5, 95, 18.0)) is None


def test_a_page_with_too_few_impressions_is_skipped():
    assert classify(window(2, 30, 20.0), window(12, 80, 18.0)) is None


# ═══════════════════════════════════════════════════════
#  The five causes
# ═══════════════════════════════════════════════════════

def test_position_loss_is_actionable():
    verdict = classify(window(120, 4500, 9.1), window(300, 5000, 4.0), site_trend=0.0)
    assert verdict.cause == "position_loss"
    assert verdict.is_actionable is True
    assert verdict.confidence == "high"


def test_seasonality_is_not_actionable():
    """Rankings held, demand left. Rewriting cannot bring the traffic back."""
    verdict = classify(window(90, 1500, 4.1), window(300, 5000, 4.0))
    assert verdict.cause == "seasonality"
    assert verdict.is_actionable is False
    assert "הביקוש ירד" in verdict.explanation


def test_serp_takeover_requires_a_named_competitor():
    """Search Console cannot tell "we slipped" from "they overtook us"."""
    without = classify(window(120, 4900, 7.5), window(300, 5000, 3.0))
    assert without.cause == "position_loss"

    with_evidence = classify(
        window(120, 4900, 7.5), window(300, 5000, 3.0),
        new_competitors=["fastdoorpros.com"],
    )
    assert with_evidence.cause == "serp_takeover"
    assert with_evidence.is_actionable is True
    assert "fastdoorpros.com" in with_evidence.explanation


def test_cannibalisation_calls_for_consolidation_not_a_rewrite():
    verdict = classify(
        window(120, 4500, 9.0), window(300, 5000, 4.0),
        competing_urls=["https://x.com/springs-guide"],
    )
    assert verdict.cause == "cannibalized"
    assert verdict.is_actionable is False


def test_collapsed_impressions_read_as_an_indexing_problem():
    verdict = classify(window(3, 200, 40.0), window(300, 5000, 4.0))
    assert verdict.cause == "deindexed"
    assert verdict.confidence == "high"


def test_an_unreadable_pattern_says_so():
    """No clear movement anywhere — measuring again beats guessing."""
    verdict = classify(window(150, 4700, 3.9), window(300, 5000, 4.0))
    assert verdict.cause == "unclear"
    assert verdict.confidence == "low"
    assert verdict.is_actionable is False


# ═══════════════════════════════════════════════════════
#  Site trend changes how sure we are
# ═══════════════════════════════════════════════════════

def test_a_drop_matching_the_site_trend_lowers_confidence():
    verdict = classify(window(120, 4500, 9.1), window(300, 5000, 4.0), site_trend=-0.55)
    assert verdict.cause == "position_loss"
    assert verdict.confidence == "medium"
    assert "מגמת האתר" in verdict.confidence_reason


# ═══════════════════════════════════════════════════════
#  Findings carry evidence and a real action
# ═══════════════════════════════════════════════════════

def test_finding_carries_the_numbers_and_a_concrete_action():
    verdict = classify(window(120, 4500, 9.1), window(300, 5000, 4.0))
    finding = to_finding(verdict, "x.com")
    assert finding.impact_clicks == 180.0
    assert finding.action["kind"] == "refresh_content"
    assert finding.evidence["position_delta"] == 5.1
    assert finding.impact_conversions is None      # no GA4 data supplied


def test_finding_expresses_conversions_when_the_client_has_the_data():
    verdict = classify(window(120, 4500, 9.1), window(300, 5000, 4.0))
    finding = to_finding(verdict, "x.com", conversion_rate=0.04)
    assert finding.impact_conversions == pytest.approx(7.2)


def test_a_deindexed_page_is_a_blocker():
    verdict = classify(window(3, 200, 40.0), window(300, 5000, 4.0))
    assert to_finding(verdict, "x.com").severity == "blocker"


def test_each_cause_maps_to_a_different_action():
    cases = [
        (classify(window(120, 4500, 9.1), window(300, 5000, 4.0)), "refresh_content"),
        (classify(window(90, 1500, 4.1), window(300, 5000, 4.0)), "schedule_for_season"),
        (classify(window(120, 4900, 7.5), window(300, 5000, 3.0),
                  new_competitors=["rival.com"]), "study_then_refresh"),
        (classify(window(3, 200, 40.0), window(300, 5000, 4.0)), "investigate_indexing"),
    ]
    for verdict, expected in cases:
        assert to_finding(verdict, "x.com").action["kind"] == expected


# ═══════════════════════════════════════════════════════
#  Loading the export
# ═══════════════════════════════════════════════════════

def export(tmp_path, payload) -> Path:
    path = tmp_path / "gsc.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_export_loads_both_windows(tmp_path):
    path = export(tmp_path, {
        "property": "sc-domain:x.com",
        "current": {"pages": [{"url": URL, "clicks": 120, "impressions": 4500, "position": 9.1}]},
        "prior": {"pages": [{"url": URL, "clicks": 300, "impressions": 5000, "position": 4.0}]},
        "site": {"clicks_pct": -0.05},
        "algorithm_updates": ["2026-08-12"],
    })
    result = load_export(path)
    assert result
    assert result.data["site_trend"] == -0.05
    assert result.data["algorithm_updates"] == ["2026-08-12"]
    assert result.data["current"][URL].clicks == 120


def test_an_export_with_one_window_is_refused(tmp_path):
    """Nothing can be concluded from a single period."""
    result = load_export(export(tmp_path, {"current": {"pages": []}}))
    assert not result
    assert result.code == "export_incomplete"


def test_a_missing_export_says_where_to_put_it(tmp_path):
    result = load_export(tmp_path / "absent.json")
    assert not result
    assert "GSC Wizard" in result.detail


def test_a_corrupt_export_is_reported_clearly(tmp_path):
    path = tmp_path / "gsc.json"
    path.write_text("{ broken", encoding="utf-8")
    assert load_export(path).code == "export_corrupt"


# ═══════════════════════════════════════════════════════
#  Ranking reflects recoverable clicks, not lost clicks
# ═══════════════════════════════════════════════════════

def test_an_actionable_decay_outranks_a_seasonal_one_that_lost_more():
    """Acting on a seasonal dip returns nothing, so it must not crowd out real work."""
    seasonal = to_finding(classify(window(50, 1200, 4.1), window(300, 5000, 4.0)), "x.com")
    actionable = to_finding(classify(window(200, 4500, 9.1), window(300, 5000, 4.0)), "x.com")

    assert seasonal.impact_clicks == 0.0
    assert seasonal.severity == "low"
    assert actionable.priority() > seasonal.priority()


def test_seasonality_still_states_what_was_lost():
    """Impact is zero, but the basis must still show the real number."""
    finding = to_finding(classify(window(50, 1200, 4.1), window(300, 5000, 4.0)), "x.com")
    assert "250 קליקים אבדו" in finding.impact_basis
    assert "ניתנים להשבה" in finding.impact_basis


def test_consolidation_is_priced_as_expensive_work():
    finding = to_finding(
        classify(window(120, 4500, 9.0), window(300, 5000, 4.0),
                 competing_urls=["https://x.com/other"]),
        "x.com",
    )
    assert finding.effort == "l"
    assert finding.impact_clicks == pytest.approx(126.0)     # 70% recoverable


def test_an_indexing_fix_is_cheap_and_ranks_top():
    deindexed = to_finding(classify(window(3, 200, 40.0), window(300, 5000, 4.0)), "x.com")
    position = to_finding(classify(window(120, 4500, 9.1), window(300, 5000, 4.0)), "x.com")
    assert deindexed.effort == "s"
    assert deindexed.priority() > position.priority()
