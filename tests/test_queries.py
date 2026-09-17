"""Tests for query-to-page analysis — and above all for the CTR curve the
whole estimate rests on."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from seo_core.sources import queries  # noqa: E402

PAGE = "https://x.com/services/garage-door-spring"
OTHER = "https://x.com/blog/spring-guide"


def row(query="spring replacement cost", url=PAGE, clicks=10, impressions=900,
        position=8.4) -> queries.QueryRow:
    return queries.QueryRow(query=query, url=url, clicks=clicks,
                            impressions=impressions, position=position)


def site_curve(rows=None) -> queries.CTRCurve:
    """A curve measured from enough rows to be the site's own."""
    generated = rows or [
        row(query=f"q{i}-{slot}", position=float(slot),
            impressions=500, clicks=int(500 * ctr))
        for slot, ctr in ((2, 0.16), (5, 0.07), (9, 0.03))
        for i in range(queries.MIN_ROWS_PER_BUCKET)
    ]
    return queries.build_curve(generated)


# ═══════════════════════════════════════════════════════
#  The curve
# ═══════════════════════════════════════════════════════

def test_a_site_without_enough_data_falls_back_and_says_so():
    """Three rows cannot describe how this site converts impressions."""
    curve = queries.build_curve([row(), row(query="another")])
    assert curve.source == "reference"
    assert "ממוצעת מהתעשייה" in curve.describe()


def test_a_site_with_enough_data_gets_its_own_curve():
    curve = site_curve()
    assert curve.source == "site"
    assert curve.expected(2) == pytest.approx(0.16, abs=0.01)
    assert "נמדדה מהאתר עצמו" in curve.describe()


def test_the_curve_uses_the_median_so_one_branded_query_cannot_skew_it():
    rows = [row(query=f"q{i}", position=5.0, impressions=500, clicks=35)
            for i in range(queries.MIN_ROWS_PER_BUCKET)]
    rows.append(row(query="brand", position=5.0, impressions=500, clicks=450))
    rows += [row(query=f"p{i}-{slot}", position=float(slot), impressions=500,
                 clicks=int(500 * ctr))
             for slot, ctr in ((2, 0.16), (9, 0.03))
             for i in range(queries.MIN_ROWS_PER_BUCKET)]

    curve = queries.build_curve(rows)
    assert curve.expected(5) == pytest.approx(0.07, abs=0.01)     # not 0.11


def test_the_curve_never_rewards_a_worse_position():
    """A sample where 6 beats 4 would produce 'move up and lose clicks'."""
    rows = []
    for slot, ctr in ((2, 0.05), (5, 0.09), (9, 0.03)):
        rows += [row(query=f"q{i}-{slot}", position=float(slot),
                     impressions=500, clicks=int(500 * ctr))
                 for i in range(queries.MIN_ROWS_PER_BUCKET)]

    curve = queries.build_curve(rows)
    values = [curve.expected(p) for p in (2, 5, 9)]
    assert values == sorted(values, reverse=True)


def test_the_curve_interpolates_between_measured_positions():
    curve = site_curve()
    assert curve.expected(9) < curve.expected(7) < curve.expected(5)


def test_positions_beyond_the_table_do_not_run_off_the_end():
    curve = queries.CTRCurve()
    assert curve.expected(1) > 0
    assert curve.expected(95) > 0


def test_a_query_below_the_impressions_floor_is_left_out_of_the_curve():
    rows = [row(query=f"q{i}", impressions=10, clicks=5) for i in range(50)]
    assert queries.build_curve(rows).source == "reference"


# ═══════════════════════════════════════════════════════
#  Near misses
# ═══════════════════════════════════════════════════════

def test_a_page_just_off_the_first_page_is_an_opportunity():
    found = queries.find_near_misses([row(position=9.0, clicks=25)], site_curve())
    assert found[0].kind == "near_miss"
    assert found[0].potential_clicks > 0
    assert "הופעות במיקום" in found[0].basis


def test_a_page_already_at_the_top_is_not_a_near_miss():
    assert queries.find_near_misses([row(position=1.4, clicks=300)], site_curve()) == []


def test_a_page_far_down_is_not_treated_as_three_places_away():
    """Position 40 is not an on-page problem, and pretending it is wastes a day."""
    assert queries.find_near_misses([row(position=41.0)], site_curve()) == []


def test_a_query_below_the_floor_is_ignored():
    assert queries.find_near_misses([row(impressions=20)], site_curve()) == []


def test_a_page_earning_far_below_its_position_is_routed_to_the_title_skill():
    """Content is not the problem when Google already ranks the page well."""
    found = queries.find_near_misses(
        [row(position=4.0, impressions=2000, clicks=10)], site_curve()
    )
    assert found[0].kind == "low_ctr"
    assert found[0].detail["belongs_to"] == "ctr-titles"
    assert found[0].is_actionable is False


# ═══════════════════════════════════════════════════════
#  Cannibalisation
# ═══════════════════════════════════════════════════════

def test_two_pages_on_one_query_are_reported_as_one_problem():
    found = queries.find_cannibalisation([
        row(url=PAGE, clicks=40, impressions=900, position=6.0),
        row(url=OTHER, clicks=5, impressions=400, position=14.0),
    ], site_curve())

    assert len(found) == 1
    assert found[0].detail["winner"] == PAGE
    assert found[0].detail["losers"][0]["url"] == OTHER


def test_the_stronger_page_wins_even_when_the_other_ranks_higher_occasionally():
    found = queries.find_cannibalisation([
        row(url=OTHER, clicks=2, impressions=500, position=5.0),
        row(url=PAGE, clicks=60, impressions=800, position=7.0),
    ], site_curve())
    assert found[0].detail["winner"] == PAGE


def test_a_second_page_with_almost_no_impressions_is_not_competition():
    assert queries.find_cannibalisation([
        row(url=PAGE, clicks=40, impressions=900),
        row(url=OTHER, clicks=0, impressions=4),
    ], site_curve()) == []


def test_consolidation_is_priced_below_a_full_recovery():
    found = queries.find_cannibalisation([
        row(url=PAGE, clicks=40, impressions=900, position=6.0),
        row(url=OTHER, clicks=5, impressions=400, position=14.0),
    ], site_curve())
    curve = site_curve()
    ceiling = 1300 * curve.expected(5.0) - 45
    assert found[0].potential_clicks < ceiling


# ═══════════════════════════════════════════════════════
#  Coverage gaps
# ═══════════════════════════════════════════════════════

def test_a_missing_term_on_the_ranking_page_is_the_clearest_signal_there_is():
    found = queries.find_coverage_gaps(
        [row(query="broken torsion spring", position=12.0)],
        {PAGE: "We replace garage door springs across Denver."},
        site_curve(),
    )
    assert found[0].kind == "coverage_gap"
    assert found[0].detail["missing_terms"] == ["broken", "torsion"]


def test_a_page_that_already_covers_the_query_is_not_flagged():
    assert queries.find_coverage_gaps(
        [row(query="spring replacement cost", position=12.0)],
        {PAGE: "Spring replacement cost in Denver starts at $180."},
        site_curve(),
    ) == []


def test_stopwords_missing_from_a_page_prove_nothing():
    assert queries.find_coverage_gaps(
        [row(query="the best spring near me", position=12.0)],
        {PAGE: "Spring repair, same day."},
        site_curve(),
    ) == []


def test_a_page_whose_text_was_not_supplied_is_skipped_not_guessed_at():
    assert queries.find_coverage_gaps(
        [row(query="torsion spring", position=12.0)], {}, site_curve()
    ) == []


# ═══════════════════════════════════════════════════════
#  Putting it together
# ═══════════════════════════════════════════════════════

def test_a_split_query_is_not_also_reported_as_a_near_miss():
    """One problem, one line. Two notes on the same query means two fixes."""
    result = queries.analyse([
        row(url=PAGE, clicks=40, impressions=900, position=6.0),
        row(url=OTHER, clicks=5, impressions=400, position=14.0),
    ])
    kinds = {o.kind for o in result["opportunities"]}
    assert kinds == {"cannibalised"}


def test_opportunities_come_back_biggest_first():
    result = queries.analyse([
        row(query="small", impressions=200, clicks=2, position=9.0),
        row(query="large", impressions=5000, clicks=40, position=9.0),
    ])
    assert result["opportunities"][0].query == "large"


# ═══════════════════════════════════════════════════════
#  Loading
# ═══════════════════════════════════════════════════════

def export(tmp_path, payload) -> Path:
    path = tmp_path / "queries.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_an_export_loads_and_lowercases_queries(tmp_path):
    result = queries.load_export(export(tmp_path, {"rows": [
        {"query": "Spring Cost", "url": PAGE, "clicks": 10,
         "impressions": 900, "position": 8.4}
    ]}))
    assert result
    assert result.data["rows"][0].query == "spring cost"


def test_a_malformed_row_is_skipped_rather_than_crashing_the_run(tmp_path):
    result = queries.load_export(export(tmp_path, {"rows": [
        {"query": "ok", "url": PAGE, "clicks": 1, "impressions": 90, "position": 5},
        {"query": "broken", "clicks": "many"},
    ]}))
    assert result
    assert len(result.data["rows"]) == 1


def test_an_export_with_nothing_usable_says_what_the_shape_should_be(tmp_path):
    result = queries.load_export(export(tmp_path, {"rows": []}))
    assert not result
    assert "impressions" in result.detail


def test_a_missing_export_says_where_to_get_one(tmp_path):
    assert "GSC Wizard" in queries.load_export(tmp_path / "absent.json").detail


# ═══════════════════════════════════════════════════════
#  Findings
# ═══════════════════════════════════════════════════════

def test_a_finding_carries_the_numbers_and_a_specific_action():
    curve = site_curve()
    opportunity = queries.find_near_misses([row(position=9.0, clicks=25)], curve)[0]
    finding = queries.to_finding(opportunity, "x.com", curve)

    assert finding.action["kind"] == "expand_section"
    assert finding.action["query"] == "spring replacement cost"
    assert finding.evidence["impressions"] == 900
    assert finding.impact_clicks > 0


def test_a_reference_curve_drags_confidence_down():
    curve = queries.CTRCurve()
    opportunity = queries.find_near_misses([row(position=9.0, clicks=25)], curve)[0]
    finding = queries.to_finding(opportunity, "x.com", curve)

    assert finding.confidence == "low"
    assert "ממוצעת מהתעשייה" in finding.confidence_reason


def test_a_measured_curve_earns_high_confidence_on_a_near_miss():
    curve = site_curve()
    opportunity = queries.find_near_misses([row(position=9.0, clicks=25)], curve)[0]
    assert queries.to_finding(opportunity, "x.com", curve).confidence == "high"


def test_a_ctr_problem_claims_no_click_impact_of_its_own():
    """It is real, but the clicks belong to whichever skill actually fixes it."""
    curve = site_curve()
    opportunity = queries.find_near_misses(
        [row(position=4.0, impressions=2000, clicks=10)], curve)[0]
    finding = queries.to_finding(opportunity, "x.com", curve)

    assert finding.impact_clicks == 0.0
    assert finding.action["kind"] == "rewrite_title"


def test_consolidation_is_priced_as_expensive_work():
    curve = site_curve()
    opportunity = queries.find_cannibalisation([
        row(url=PAGE, clicks=40, impressions=900, position=6.0),
        row(url=OTHER, clicks=5, impressions=400, position=14.0),
    ], curve)[0]
    assert queries.to_finding(opportunity, "x.com", curve).effort == "l"


def test_conversions_are_expressed_only_when_the_client_measures_them():
    curve = site_curve()
    opportunity = queries.find_near_misses([row(position=9.0, clicks=25)], curve)[0]

    assert queries.to_finding(opportunity, "x.com", curve).impact_conversions is None
    with_rate = queries.to_finding(opportunity, "x.com", curve, conversion_rate=0.04)
    assert with_rate.impact_conversions == pytest.approx(
        round(opportunity.potential_clicks * 0.04, 2))


# ═══════════════════════════════════════════════════════
#  Inflection — the difference between a finding and an insult
# ═══════════════════════════════════════════════════════

@pytest.mark.parametrize("term,text", [
    ("spring", "we replace garage door springs"),
    ("springs", "we replace a garage door spring"),
    ("repair", "same day repairs in denver"),
    ("install", "we installed it yesterday"),
    ("תריס", "אנחנו מתקנים את התריס"),
    ("החלפה", "החלפה של קפיץ"),
])
def test_an_inflected_form_on_the_page_counts_as_covering_the_term(term, text):
    """A page about "springs" is not missing "spring"."""
    assert queries.covers(term, set(queries._WORD.findall(text))) is True


def test_a_word_the_page_genuinely_never_says_is_still_missing():
    assert queries.covers("torsion", {"garage", "door", "springs"}) is False


def test_a_plural_page_does_not_hide_a_real_gap():
    found = queries.find_coverage_gaps(
        [row(query="torsion spring cost", position=12.0)],
        {PAGE: "We replace garage door springs."},
        site_curve(),
    )
    assert found[0].detail["missing_terms"] == ["cost", "torsion"]


# ═══════════════════════════════════════════════════════
#  Claims a client can be told with a straight face
# ═══════════════════════════════════════════════════════

def test_a_page_at_four_is_not_promised_the_top_spot():
    """Three places from 4 is number one. On-page work does not do that."""
    found = queries.find_near_misses(
        [row(position=4.4, impressions=1500, clicks=130)], site_curve())
    assert found[0].detail["target_position"] == queries.BEST_CLAIMABLE_POSITION
    assert "למיקום 3" in found[0].basis


def test_a_page_deep_on_the_second_page_still_gets_a_full_three_places():
    found = queries.find_near_misses(
        [row(position=14.0, impressions=1500, clicks=20)], site_curve())
    assert found[0].detail["target_position"] == pytest.approx(11.0)


def test_a_poor_ctr_on_the_second_page_is_not_called_a_title_problem():
    """At position 12 the expected rate is ~1%; half of that is noise, and the
    real problem is being on page two at all."""
    found = queries.find_near_misses(
        [row(position=12.4, impressions=760, clicks=3)], site_curve())
    assert found[0].kind == "near_miss"


def test_a_poor_ctr_high_up_is_still_a_title_problem():
    found = queries.find_near_misses(
        [row(position=4.0, impressions=2000, clicks=10)], site_curve())
    assert found[0].kind == "low_ctr"


def test_a_query_sent_to_ctr_titles_is_not_also_reported_as_a_content_gap():
    """Two skills claiming the same clicks is how a work order stops adding up."""
    result = queries.analyse(
        [row(query="torsion spring", url=PAGE, position=4.0,
             impressions=2000, clicks=10)]
        + [row(query=f"q{i}-{slot}", position=float(slot), impressions=500,
               clicks=int(500 * ctr))
           for slot, ctr in ((2, 0.16), (5, 0.07), (9, 0.03))
           for i in range(queries.MIN_ROWS_PER_BUCKET)],
        {PAGE: "We fix garage doors."},
    )
    for_query = [o.kind for o in result["opportunities"] if o.query == "torsion spring"]
    assert for_query == ["low_ctr"]
