"""
Topic clusters — grouped by the page Google chose, and remembered.
==================================================================
"""

from __future__ import annotations

import json

from seo_core.sources import clusters as cl
from seo_core.sources.queries import CTRCurve, QueryRow


def curve() -> CTRCurve:
    return CTRCurve(
        buckets={1: 0.28, 2: 0.16, 3: 0.11, 4: 0.08, 5: 0.06, 6: 0.05,
                 7: 0.04, 8: 0.032, 10: 0.025, 12: 0.018, 20: 0.008},
        source="site", sample=640,
    )


def row(query, url, impressions=500, clicks=10, position=8.0) -> QueryRow:
    return QueryRow(query=query, url=url, clicks=clicks,
                    impressions=impressions, position=position)


SPRINGS = "https://x.com/springs"
CABLES = "https://x.com/cables"


def springs_rows() -> list[QueryRow]:
    return [
        row("torsion spring repair", SPRINGS),
        row("broken garage door spring", SPRINGS),
        row("spring replacement cost", SPRINGS),
    ]


# ═══════════════════════════════════════════════════════
#  Building
# ═══════════════════════════════════════════════════════

class TestBuild:
    def test_queries_answered_by_one_page_are_one_topic(self):
        built = cl.build(springs_rows())
        assert len(built) == 1
        assert {q.query for q in built[0].queries} == {
            "torsion spring repair", "broken garage door spring",
            "spring replacement cost"}

    def test_two_pages_join_when_a_query_ranks_on_both(self):
        """Google ranking both pages for one query is Google saying they are
        the same topic."""
        rows = springs_rows() + [
            row("cable repair", CABLES),
            row("garage door cable", CABLES),
            row("spring replacement cost", CABLES),      # the bridge
        ]
        built = cl.build(rows)
        assert len(built) == 1
        assert set(built[0].urls) == {SPRINGS, CABLES}

    def test_unrelated_pages_stay_apart(self):
        rows = springs_rows() + [
            row("cable repair", CABLES),
            row("garage door cable", CABLES),
            row("cable snapped", CABLES),
        ]
        assert len(cl.build(rows)) == 2

    def test_words_alone_never_merge_two_topics(self):
        """"spring water" and "spring repair" share a word and nothing else."""
        rows = springs_rows() + [
            row("spring water delivery", "https://x.com/water"),
            row("spring water cost", "https://x.com/water"),
            row("bottled spring water", "https://x.com/water"),
        ]
        built = cl.build(rows)
        assert len(built) == 2

    def test_a_pair_of_queries_is_not_a_topic(self):
        assert cl.build(springs_rows()[:2]) == []

    def test_a_query_nobody_searches_for_does_not_shape_the_structure(self):
        rows = springs_rows() + [row("noise", SPRINGS, impressions=3)]
        assert "noise" not in {q.query for q in cl.build(rows)[0].queries}

    def test_the_label_avoids_words_that_name_nothing(self):
        rows = [row("spring repair cost", SPRINGS),
                row("spring repair price", SPRINGS),
                row("spring repair service", SPRINGS)]
        label = cl.build(rows)[0].label
        assert "spring" in label
        assert "cost" not in label


class TestPillar:
    def test_the_page_earning_the_clicks_owns_the_topic(self):
        rows = [row("a", SPRINGS, clicks=90, impressions=900),
                row("b", CABLES, clicks=2, impressions=900),
                row("c", CABLES, clicks=2, impressions=900),
                row("a", CABLES, clicks=1, impressions=100)]
        assert cl.build(rows)[0].pillar == SPRINGS

    def test_a_topic_split_across_two_pages_is_flagged(self):
        rows = [row("a", SPRINGS, impressions=1000),
                row("b", SPRINGS, impressions=1000),
                row("c", CABLES, impressions=900),
                row("a", CABLES, impressions=100)]
        assert cl.build(rows)[0].is_split

    def test_a_page_with_a_sliver_of_the_topic_is_not_a_split(self):
        rows = [row("a", SPRINGS, impressions=5000),
                row("b", SPRINGS, impressions=5000),
                row("c", SPRINGS, impressions=5000),
                row("a", CABLES, impressions=60)]
        assert not cl.build(rows)[0].is_split


class TestSubtopics:
    def test_a_buried_subtopic_with_real_demand_is_found(self):
        rows = springs_rows() + [
            row("garage door spring winding bars", SPRINGS,
                impressions=900, clicks=3, position=14.0)]
        found = cl.build(rows)[0].subtopics()
        assert [q.query for q in found] == ["garage door spring winding bars"]

    def test_a_subtopic_the_page_already_answers_is_left_alone(self):
        rows = springs_rows() + [
            row("winding bars", SPRINGS, impressions=900, clicks=80,
                position=3.0)]
        assert cl.build(rows)[0].subtopics() == []

    def test_a_small_subtopic_does_not_earn_its_own_page(self):
        rows = springs_rows() + [
            row("winding bars", SPRINGS, impressions=120, position=15.0)]
        assert cl.build(rows)[0].subtopics() == []


# ═══════════════════════════════════════════════════════
#  The map that remembers
# ═══════════════════════════════════════════════════════

class TestMap:
    def test_a_first_run_says_it_has_nothing_to_compare_against(self, tmp_path):
        result = cl.load_and_merge(tmp_path, cl.build(springs_rows()), "x.com")
        assert result.data["delta"].first_run
        assert (tmp_path / cl.MAP_FILE).exists()

    def test_a_second_identical_run_reports_no_change(self, tmp_path):
        built = cl.build(springs_rows())
        cl.load_and_merge(tmp_path, built, "x.com")
        delta = cl.load_and_merge(tmp_path, built, "x.com").data["delta"]

        assert not delta.first_run
        assert delta.is_empty
        assert "אין שינוי" in delta.summary()

    def test_the_run_count_climbs(self, tmp_path):
        built = cl.build(springs_rows())
        for _ in range(3):
            result = cl.load_and_merge(tmp_path, built, "x.com")
        assert result.data["payload"]["runs"] == 3

    def test_a_new_query_is_reported_against_the_cluster_it_joined(self, tmp_path):
        cl.load_and_merge(tmp_path, cl.build(springs_rows()), "x.com")

        grown = springs_rows() + [row("spring winding bars", SPRINGS)]
        delta = cl.load_and_merge(tmp_path, cl.build(grown), "x.com").data["delta"]
        assert list(delta.new_queries.values())[0] == ["spring winding bars"]

    def test_a_query_that_stopped_appearing_is_kept_and_reported(self, tmp_path):
        """A topic that quietly disappeared is the most interesting thing a
        second run can say, and deleting the row would delete the finding."""
        grown = springs_rows() + [row("spring winding bars", SPRINGS)]
        cl.load_and_merge(tmp_path, cl.build(grown), "x.com")

        result = cl.load_and_merge(tmp_path, cl.build(springs_rows()), "x.com")
        delta = result.data["delta"]
        assert list(delta.lost_queries.values())[0] == ["spring winding bars"]

        stored = json.loads((tmp_path / cl.MAP_FILE).read_text(encoding="utf-8"))
        cluster = list(stored["clusters"].values())[0]
        assert "spring winding bars" in cluster["queries"]

    def test_first_seen_survives_every_later_run(self, tmp_path):
        built = cl.build(springs_rows())
        first = cl.load_and_merge(tmp_path, built, "x.com").data["payload"]
        original = list(first["clusters"].values())[0]["queries"][
            "torsion spring repair"]["first_seen"]

        again = cl.load_and_merge(tmp_path, built, "x.com").data["payload"]
        assert list(again["clusters"].values())[0]["queries"][
            "torsion spring repair"]["first_seen"] == original

    def test_a_pillar_that_changed_hands_is_reported(self, tmp_path):
        before = [row("a", SPRINGS, clicks=90), row("b", SPRINGS),
                  row("c", SPRINGS), row("a", CABLES, clicks=1)]
        cl.load_and_merge(tmp_path, cl.build(before), "x.com")

        after = [row("a", SPRINGS, clicks=1), row("b", SPRINGS),
                 row("c", SPRINGS), row("a", CABLES, clicks=900)]
        delta = cl.load_and_merge(tmp_path, cl.build(after), "x.com").data["delta"]
        assert delta.moved_pillars and delta.moved_pillars[0][1] == SPRINGS

    def test_a_whole_topic_that_vanished_is_not_erased(self, tmp_path):
        both = springs_rows() + [row("cable repair", CABLES),
                                 row("cable snapped", CABLES),
                                 row("door cable", CABLES)]
        cl.load_and_merge(tmp_path, cl.build(both), "x.com")

        result = cl.load_and_merge(tmp_path, cl.build(springs_rows()), "x.com")
        assert len(result.data["payload"]["clusters"]) == 2

    def test_a_corrupt_map_does_not_stop_the_run(self, tmp_path):
        (tmp_path / cl.MAP_FILE).write_text("{not json", encoding="utf-8")
        result = cl.load_and_merge(tmp_path, cl.build(springs_rows()), "x.com")
        assert result


# ═══════════════════════════════════════════════════════
#  Findings
# ═══════════════════════════════════════════════════════

class TestFindings:
    def test_a_new_page_is_never_claimed_with_confidence(self):
        rows = springs_rows() + [
            row("spring winding bars", SPRINGS, impressions=2000, clicks=5,
                position=15.0)]
        finding = next(f for f in cl.to_findings(cl.build(rows), "x.com", curve())
                       if f.type == "subtopic_needs_page")
        assert finding.confidence == "low"
        assert finding.effort == "l"

    def test_a_split_topic_claims_only_half_the_gap(self):
        rows = [row("a", SPRINGS, impressions=4000, clicks=40),
                row("b", SPRINGS, impressions=4000, clicks=40),
                row("c", CABLES, impressions=3000, clicks=10),
                row("a", CABLES, impressions=500, clicks=1)]
        finding = next(f for f in cl.to_findings(cl.build(rows), "x.com", curve())
                       if f.type == "topic_split")
        assert finding.impact_clicks > 0
        assert finding.confidence == "low"

    def test_a_tidy_site_produces_no_structural_findings(self):
        assert cl.to_findings(cl.build(springs_rows()), "x.com", curve()) == []


class TestIdentity:
    def test_a_topic_that_grew_a_query_is_not_a_new_topic(self, tmp_path):
        """The label is generated from the words, so it drifts as the cluster
        grows. Keying the map on it made every growing topic look brand new."""
        cl.load_and_merge(tmp_path, cl.build(springs_rows()), "x.com")

        grown = springs_rows() + [row("extension spring vs torsion", SPRINGS)]
        result = cl.load_and_merge(tmp_path, cl.build(grown), "x.com")

        assert result.data["delta"].new_clusters == []
        assert len(result.data["payload"]["clusters"]) == 1

    def test_a_label_that_drifted_is_reported_as_a_rename(self):
        before = cl.Cluster(label="spring torsion", queries=springs_rows())
        after = cl.Cluster(label="spring torsion winding", queries=springs_rows())

        stored, _ = cl.merge({}, [before], "x.com")
        _, delta = cl.merge(stored, [after], "x.com")

        assert delta.renamed == [(before.slug, "spring torsion",
                                  "spring torsion winding")]
        assert delta.new_clusters == []

    def test_a_genuinely_new_topic_is_still_new(self, tmp_path):
        cl.load_and_merge(tmp_path, cl.build(springs_rows()), "x.com")

        both = springs_rows() + [row("cable repair", CABLES),
                                 row("cable snapped", CABLES),
                                 row("door cable", CABLES)]
        delta = cl.load_and_merge(tmp_path, cl.build(both), "x.com").data["delta"]
        assert len(delta.new_clusters) == 1

    def test_two_topics_never_collapse_onto_one_stored_record(self, tmp_path):
        both = springs_rows() + [row("cable repair", CABLES),
                                 row("cable snapped", CABLES),
                                 row("door cable", CABLES)]
        cl.load_and_merge(tmp_path, cl.build(both), "x.com")
        result = cl.load_and_merge(tmp_path, cl.build(both), "x.com")

        assert len(result.data["payload"]["clusters"]) == 2
        assert result.data["delta"].is_empty

    def test_a_topic_replaced_wholesale_keeps_the_old_record_separate(self, tmp_path):
        cl.load_and_merge(tmp_path, cl.build(springs_rows()), "x.com")

        unrelated = [row("cable repair", CABLES), row("cable snapped", CABLES),
                     row("door cable", CABLES)]
        result = cl.load_and_merge(tmp_path, cl.build(unrelated), "x.com")
        assert len(result.data["payload"]["clusters"]) == 2
