"""
Algorithm updates — correlation, and the refusal to call it cause.
==================================================================
"""

from __future__ import annotations

import json
from datetime import date

from seo_core.sources import updates as up
from seo_core.sources.queries import QueryRow


def row(url, clicks, impressions=1000, position=5.0):
    return QueryRow(query="q", url=url, clicks=clicks,
                    impressions=impressions, position=position)


def site(before: dict[str, int], after: dict[str, int]) -> list[up.PageChange]:
    return up.compare([row(u, c) for u, c in before.items()],
                      [row(u, c) for u, c in after.items()])


def flat_site(pages: int = 12, clicks: int = 100) -> dict[str, int]:
    return {f"https://x.com/p{i}": clicks for i in range(pages)}


# ═══════════════════════════════════════════════════════
#  The list
# ═══════════════════════════════════════════════════════

class TestUpdateList:
    def test_a_window_inside_a_rollout_finds_it(self):
        found = up.in_window(date(2025, 3, 20), date(2025, 3, 25))
        assert any("March 2025" in u.name for u in found)

    def test_a_quiet_window_finds_nothing(self):
        assert up.in_window(date(2025, 5, 5), date(2025, 5, 12)) == []

    def test_a_window_touching_only_the_edge_of_a_rollout_still_matches(self):
        """A rollout that completed on the 27th was moving results from the 13th."""
        assert up.in_window(date(2025, 3, 26), date(2025, 4, 10))

    def test_a_window_past_the_last_review_is_unknown_and_not_clean(self):
        outcome = up.coverage(date(2026, 4, 1))
        assert not outcome
        assert "אינו ממצא" in outcome.detail

    def test_a_window_inside_the_reviewed_period_is_covered(self):
        assert up.coverage(date(2025, 4, 1))

    def test_a_supplied_list_replaces_the_bundled_one(self, tmp_path):
        path = tmp_path / "updates.json"
        path.write_text(json.dumps({"updates": [
            {"name": "March 2026 core update", "start": "2026-03-01",
             "end": "2026-03-20", "kind": "core"}]}), encoding="utf-8")

        supplied = up.load_updates(path)
        assert len(supplied) == 1
        assert up.in_window(date(2026, 3, 5), date(2026, 3, 10), supplied)

    def test_a_supplied_list_extends_the_coverage_horizon(self, tmp_path):
        path = tmp_path / "updates.json"
        path.write_text(json.dumps({"updates": [
            {"name": "March 2026 core update", "start": "2026-03-01",
             "end": "2026-03-20", "kind": "core"}]}), encoding="utf-8")
        assert up.coverage(date(2026, 3, 15), up.load_updates(path))

    def test_a_malformed_entry_is_skipped_and_the_rest_load(self, tmp_path):
        path = tmp_path / "updates.json"
        path.write_text(json.dumps({"updates": [
            {"name": "broken", "start": "not-a-date", "end": "2026-03-20"},
            {"name": "fine", "start": "2026-03-01", "end": "2026-03-20"}]}),
            encoding="utf-8")
        assert [u.name for u in up.load_updates(path)] == ["fine"]


# ═══════════════════════════════════════════════════════
#  Measuring the site's own noise
# ═══════════════════════════════════════════════════════

class TestSpread:
    def test_the_threshold_comes_from_the_site_and_not_from_a_round_number(self):
        """A volatile site and a stable one do not share a definition of drop."""
        stable = site(flat_site(), {f"https://x.com/p{i}": 100 + (i % 3)
                                    for i in range(12)})
        volatile = site(flat_site(), {f"https://x.com/p{i}": 100 + (i * 25 - 150)
                                      for i in range(12)})
        assert (up.spread_of(volatile).high - up.spread_of(volatile).low) > \
               (up.spread_of(stable).high - up.spread_of(stable).low)

    def test_too_few_pages_is_reported_and_not_guessed(self):
        small = site({"https://x.com/a": 100}, {"https://x.com/a": 40})
        spread = up.spread_of(small)
        assert not spread.measurable
        assert "מעט מדי" in spread.describe()

    def test_a_page_too_small_to_read_does_not_shape_the_spread(self):
        before = {**flat_site(), "https://x.com/tiny": 4}
        after = {**flat_site(), "https://x.com/tiny": 0}
        assert up.spread_of(site(before, after)).sample == 12


# ═══════════════════════════════════════════════════════
#  The diagnosis
# ═══════════════════════════════════════════════════════

class TestDiagnose:
    def test_the_whole_site_falling_in_an_update_window_is_site_wide(self):
        changes = site(flat_site(), {f"https://x.com/p{i}": 55 for i in range(12)})
        found = up.diagnose(changes, date(2025, 3, 10), date(2025, 4, 10))

        assert {d.verdict for d in found} == {"site_wide"}
        assert found[0].blames_update
        assert "קורלציה, לא הוכחה" in found[0].describe()

    def test_the_whole_site_falling_with_no_update_points_somewhere_else(self):
        changes = site(flat_site(), {f"https://x.com/p{i}": 55 for i in range(12)})
        found = up.diagnose(changes, date(2025, 5, 1), date(2025, 5, 20))

        assert found[0].verdict == "site_wide"
        assert not found[0].blames_update
        assert "שינוי טכני" in found[0].describe()

    def test_one_page_falling_alone_is_never_the_algorithm(self):
        """An update does not take one page and leave the rest untouched."""
        after = flat_site()
        after["https://x.com/p3"] = 5
        found = up.diagnose(site(flat_site(), after),
                            date(2025, 3, 10), date(2025, 4, 10))

        hit = next(d for d in found if d.page.url.endswith("/p3"))
        assert hit.verdict == "page_specific"
        assert not hit.blames_update
        assert "בעיה של הדף" in hit.describe()

    def test_ordinary_movement_is_not_a_drop_whatever_the_calendar_says(self):
        after = {f"https://x.com/p{i}": 100 - (i % 4) * 3 for i in range(12)}
        found = up.diagnose(site(flat_site(), after),
                            date(2025, 3, 10), date(2025, 4, 10))
        assert {d.verdict for d in found} == {"within_noise"}
        assert up.losses(found) == []

    def test_a_page_too_small_to_read_is_said_to_be_unreadable(self):
        before = {**flat_site(), "https://x.com/tiny": 6}
        after = {**flat_site(), "https://x.com/tiny": 1}
        found = up.diagnose(site(before, after),
                            date(2025, 3, 10), date(2025, 4, 10))

        tiny = next(d for d in found if d.page.url.endswith("/tiny"))
        assert tiny.verdict == "unreadable"
        assert "מעט מדי" in tiny.describe()

    def test_a_page_that_disappeared_entirely_is_still_counted(self):
        after = {k: v for k, v in flat_site().items() if not k.endswith("/p3")}
        found = up.diagnose(site(flat_site(), after),
                            date(2025, 3, 10), date(2025, 4, 10))
        gone = next(d for d in found if d.page.url.endswith("/p3"))
        assert gone.page.after == 0
        assert gone.verdict == "page_specific"

    def test_only_real_losses_are_returned(self):
        after = flat_site()
        after["https://x.com/p3"] = 5
        after["https://x.com/p7"] = 400
        found = up.losses(up.diagnose(site(flat_site(), after),
                                      date(2025, 3, 10), date(2025, 4, 10)))
        assert [d.page.url for d in found] == ["https://x.com/p3"]


# ═══════════════════════════════════════════════════════
#  Findings
# ═══════════════════════════════════════════════════════

class TestFindings:
    def test_no_finding_here_proposes_an_automatic_rollback(self):
        changes = site(flat_site(), {f"https://x.com/p{i}": 30 for i in range(12)})
        found = up.diagnose(changes, date(2025, 3, 10), date(2025, 4, 10))

        for finding in up.to_findings(found, "x.com"):
            assert finding.action["kind"] == "investigate_loss"
            assert "שחזור אוטומטי" in (finding.action["instruction"]
                                        + finding.confidence_reason)

    def test_an_update_correlation_is_the_lowest_confidence_there_is(self):
        changes = site(flat_site(), {f"https://x.com/p{i}": 30 for i in range(12)})
        found = up.diagnose(changes, date(2025, 3, 10), date(2025, 4, 10))
        finding = up.to_findings(found, "x.com")[0]

        assert finding.confidence == "low"
        assert "קורלציה" in finding.confidence_reason

    def test_a_page_specific_loss_is_more_confident_than_an_update_story(self):
        after = flat_site()
        after["https://x.com/p3"] = 5
        found = up.diagnose(site(flat_site(), after),
                            date(2025, 3, 10), date(2025, 4, 10))
        finding = up.to_findings(found, "x.com")[0]
        assert finding.confidence == "medium"

    def test_the_finding_carries_the_sites_own_movement_as_evidence(self):
        after = flat_site()
        after["https://x.com/p3"] = 5
        found = up.diagnose(site(flat_site(), after),
                            date(2025, 3, 10), date(2025, 4, 10))
        evidence = up.to_findings(found, "x.com")[0].evidence
        assert "site_median_change" in evidence
        assert len(evidence["site_noise_band"]) == 2
