"""
The audit: one work queue, and the double counting it has to remove.
====================================================================
"""

from __future__ import annotations

import json

from seo_core import report


def finding(**over) -> dict:
    base = {
        "skill": "onpage-optimizer", "client": "x.com", "type": "near_miss",
        "severity": "medium", "source": "gsc_wizard",
        "url": "https://x.com/springs", "query": "torsion spring",
        "impact_clicks": 40.0, "impact_conversions": None,
        "impact_basis": "3,000 הופעות במיקום 6.2",
        "confidence": "high", "confidence_reason": "עקומה שנמדדה מהאתר",
        "effort": "m",
        "evidence": {"impressions": 3000, "clicks": 60},
        "action": {"kind": "expand_section", "instruction": "הרחב את המענה"},
        "baseline": {"impressions": 3000, "clicks": 60, "position": 6.2},
        "priority": 16.0,
    }
    base.update(over)
    return base


def write(tmp_path, name: str, records: list[dict]):
    (tmp_path / name).write_text(
        json.dumps({"count": len(records), "findings": records},
                   ensure_ascii=False),
        encoding="utf-8")


# ═══════════════════════════════════════════════════════
#  Reading
# ═══════════════════════════════════════════════════════

class TestCollect:
    def test_every_skills_file_is_read(self, tmp_path):
        write(tmp_path, "onpage_findings.json", [finding()])
        write(tmp_path, "ctr_findings.json", [finding(skill="ctr-titles")])
        assert len(report.collect(tmp_path)) == 2

    def test_one_corrupt_file_does_not_stop_the_report(self, tmp_path):
        write(tmp_path, "good_findings.json", [finding()])
        (tmp_path / "bad_findings.json").write_text("{not json", encoding="utf-8")
        assert len(report.collect(tmp_path)) == 1

    def test_an_incomplete_record_is_skipped_and_the_rest_survive(self, tmp_path):
        """A validating constructor here would mean one bad record hides all."""
        write(tmp_path, "mixed_findings.json", [finding(), {"no": "skill"}])
        assert len(report.collect(tmp_path)) == 1

    def test_a_missing_directory_is_not_an_error(self, tmp_path):
        assert report.collect(tmp_path / "nothing") == []

    def test_the_same_finding_from_two_runs_is_collapsed(self, tmp_path):
        write(tmp_path, "a_findings.json", [finding(priority=10.0)])
        write(tmp_path, "b_findings.json", [finding(priority=30.0)])
        collapsed = report.dedupe(report.collect(tmp_path))
        assert len(collapsed) == 1
        assert collapsed[0]["priority"] == 30.0


# ═══════════════════════════════════════════════════════
#  Pooling
# ═══════════════════════════════════════════════════════

class TestPooling:
    def four_skills_on_one_page(self, impressions: int = 1500) -> list[dict]:
        baseline = {"impressions": impressions, "clicks": 60, "position": 6.2}
        return [
            finding(skill="onpage-optimizer", type="near_miss",
                    impact_clicks=80.0, baseline=dict(baseline)),
            finding(skill="internal-anchors", type="under_supported_page",
                    impact_clicks=45.0, baseline=dict(baseline)),
            finding(skill="ctr-titles", type="low_ctr_snippet",
                    impact_clicks=60.0, baseline=dict(baseline)),
            finding(skill="click-depth", type="deep_page_with_demand",
                    impact_clicks=25.0, baseline=dict(baseline)),
        ]

    def test_four_fixes_on_one_page_do_not_add_up(self):
        pages = report.pool(self.four_skills_on_one_page())
        assert pages[0].raw_claim == 210.0
        assert pages[0].was_capped
        # 1,500 impressions at the ceiling rate, minus the 60 clicks it has.
        assert pages[0].pooled_claim == 1500 * report.CEILING_CTR - 60

    def test_a_page_with_room_to_spare_keeps_the_whole_claim(self):
        """The rule removes arithmetic that was never possible, not optimism."""
        pages = report.pool(self.four_skills_on_one_page(impressions=3000))
        assert pages[0].pooled_claim == 210.0
        assert not pages[0].was_capped

    def test_two_different_pages_are_never_pooled_together(self):
        pages = report.pool([
            finding(url="https://x.com/a", impact_clicks=80.0),
            finding(url="https://x.com/b", impact_clicks=80.0),
        ])
        assert len(pages) == 2
        assert all(not p.was_capped for p in pages)

    def test_a_page_whose_impressions_we_cannot_find_is_left_uncapped(self):
        """Capping on a guess would be worse than not capping."""
        pages = report.pool([
            finding(impact_clicks=500.0, baseline={}, evidence={}),
        ])
        assert pages[0].headroom is None
        assert pages[0].pooled_claim == 500.0

    def test_a_page_already_earning_its_ceiling_claims_nothing(self):
        pages = report.pool([finding(
            impact_clicks=80.0,
            baseline={"impressions": 3000, "clicks": 3000},
        )])
        assert pages[0].pooled_claim == 0.0

    def test_a_fault_claiming_no_clicks_is_not_pooled(self):
        assert report.pool([finding(impact_clicks=0.0)]) == []

    def test_a_measured_curve_can_replace_the_reference_ceiling(self):
        pages = report.pool(self.four_skills_on_one_page(), ceiling_ctr=0.30)
        assert pages[0].pooled_claim == 210.0       # the whole claim now fits


# ═══════════════════════════════════════════════════════
#  The portfolio
# ═══════════════════════════════════════════════════════

class TestPortfolio:
    def test_the_headline_number_is_the_pooled_one(self, tmp_path):
        write(tmp_path, "a_findings.json", [
            finding(skill="onpage-optimizer", type="near_miss", impact_clicks=80.0),
            finding(skill="ctr-titles", type="low_ctr_snippet", impact_clicks=300.0),
        ])
        portfolio = report.build("x.com", tmp_path)

        assert portfolio.naive_claim == 380.0
        assert portfolio.total_claim < portfolio.naive_claim
        assert portfolio.double_counted > 0

    def test_faults_and_estimates_are_kept_apart(self, tmp_path):
        write(tmp_path, "a_findings.json", [
            finding(skill="click-depth", type="broken_internal_link",
                    impact_clicks=0.0, severity="high"),
            finding(impact_clicks=40.0),
        ])
        portfolio = report.build("x.com", tmp_path)
        assert len(portfolio.faults) == 1
        assert len(portfolio.estimates) == 1

    def test_faults_are_ordered_by_severity(self, tmp_path):
        write(tmp_path, "a_findings.json", [
            finding(type="deep", impact_clicks=0.0, severity="low"),
            finding(type="broken", impact_clicks=0.0, severity="blocker"),
        ])
        assert report.build("x.com", tmp_path).faults[0]["type"] == "broken"


# ═══════════════════════════════════════════════════════
#  Triage
# ═══════════════════════════════════════════════════════

class TestTriage:
    def test_a_certain_fault_is_never_deferred_for_an_estimate(self, tmp_path):
        """Leaving a 404 for a month to make room for a rewrite is the wrong
        trade every time."""
        write(tmp_path, "a_findings.json", [
            finding(type="broken_internal_link", skill="click-depth",
                    impact_clicks=0.0, severity="high", effort="s"),
            finding(type="near_miss", impact_clicks=900.0, effort="l",
                    priority=400.0),
        ])
        buckets = report.triage(report.build("x.com", tmp_path))
        assert buckets["now"][0]["type"] == "broken_internal_link"

    def test_the_week_stops_when_the_hours_run_out(self, tmp_path):
        write(tmp_path, "a_findings.json", [
            finding(type=f"near_miss_{i}", url=f"https://x.com/p{i}",
                    impact_clicks=40.0, effort="l", priority=100.0 - i)
            for i in range(5)
        ])
        buckets = report.triage(report.build("x.com", tmp_path))
        assert report.planned_hours(buckets["now"]) <= report.WEEKLY_BUDGET_HOURS
        assert buckets["soon"]

    def test_a_low_confidence_finding_never_leads_the_plan(self, tmp_path):
        write(tmp_path, "a_findings.json", [
            finding(confidence="low", impact_clicks=900.0, priority=900.0),
        ])
        buckets = report.triage(report.build("x.com", tmp_path))
        assert buckets["now"] == []
        assert buckets["watch"]

    def test_hours_are_summed_per_bucket(self):
        records = [finding(effort="s"), finding(effort="m"), finding(effort="l")]
        assert report.planned_hours(records) == 21.0


# ═══════════════════════════════════════════════════════
#  The document
# ═══════════════════════════════════════════════════════

class TestDocument:
    def test_the_double_counting_is_stated_and_not_hidden(self, tmp_path):
        write(tmp_path, "a_findings.json", [
            finding(skill="onpage-optimizer", type="near_miss", impact_clicks=80.0),
            finding(skill="ctr-titles", type="low_ctr_snippet", impact_clicks=300.0),
        ])
        portfolio = report.build("x.com", tmp_path)
        text = report.render_markdown(portfolio, report.triage(portfolio))

        assert "ספירה כפולה" in text
        assert f"{portfolio.total_claim:.0f}" in text

    def test_every_estimate_carries_its_basis_and_confidence(self, tmp_path):
        write(tmp_path, "a_findings.json", [finding()])
        portfolio = report.build("x.com", tmp_path)
        text = report.render_markdown(portfolio, report.triage(portfolio))

        assert "3,000 הופעות במיקום 6.2" in text
        assert "עקומה שנמדדה מהאתר" in text

    def test_an_empty_client_produces_a_document_and_not_a_crash(self, tmp_path):
        portfolio = report.build("x.com", tmp_path)
        text = report.render_markdown(portfolio, report.triage(portfolio))
        assert "אודיט SEO — x.com" in text


class TestHebrew:
    def test_a_single_item_is_not_printed_as_a_plural(self, tmp_path):
        """"1 פריטים" reads as a bug to the one person these reports are for."""
        write(tmp_path, "a_findings.json", [
            finding(type="broken", impact_clicks=0.0, severity="high", effort="s"),
        ])
        portfolio = report.build("x.com", tmp_path)
        text = report.render_markdown(portfolio, report.triage(portfolio))

        assert "1 פריטים" not in text
        assert "פריט אחד" in text
        assert "תקלה ודאית אחת" in text

    def test_one_hour_is_written_as_a_word(self, tmp_path):
        write(tmp_path, "a_findings.json", [finding(effort="s")])
        portfolio = report.build("x.com", tmp_path)
        text = report.render_markdown(portfolio, report.triage(portfolio))
        assert "~1 שעות" not in text
        assert "כשעה" in text
