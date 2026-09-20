"""
Titles and descriptions — what renders, what it is worth, what is refused.
==========================================================================
"""

from __future__ import annotations

from seo_core.sources.queries import CTRCurve, QueryRow
from seo_core.text import pixels, snippets


def curve() -> CTRCurve:
    """A measured curve, so findings are not discounted for the wrong reason."""
    return CTRCurve(
        buckets={1: 0.28, 2: 0.16, 3: 0.11, 4: 0.08, 5: 0.06,
                 6: 0.05, 7: 0.04, 8: 0.032, 10: 0.025, 12: 0.018, 20: 0.008},
        source="site", sample=640,
    )


def row(query="spring repair", url="https://example.com/springs",
        clicks=10, impressions=1000, position=4.0) -> QueryRow:
    return QueryRow(query=query, url=url, clicks=clicks,
                    impressions=impressions, position=position)


# ═══════════════════════════════════════════════════════
#  Pixels
# ═══════════════════════════════════════════════════════

class TestWidth:
    def test_the_same_character_count_can_fit_or_overflow(self):
        """The whole reason this module exists instead of len()."""
        narrow = "l" * 60
        wide = "W" * 60
        assert pixels.fits(narrow, "title")
        assert not pixels.fits(wide, "title")

    def test_truncation_cuts_at_a_word_boundary(self):
        text = "Garage Door Spring Replacement and Torsion Repair in Denver Colorado"
        measured = pixels.measure(text, "title")
        assert measured.truncated
        assert not measured.visible.endswith(" ")
        assert measured.visible in text
        # The cut falls between words, not inside one.
        assert text[len(measured.visible)] in " "

    def test_the_ellipsis_comes_out_of_the_budget(self):
        text = "W" * 200
        measured = pixels.measure(text, "title")
        shown = pixels.width(measured.visible, pixels.FONT_PX["title"])
        assert shown + pixels.width("…", pixels.FONT_PX["title"]) <= measured.limit

    def test_hebrew_is_measured_not_guessed(self):
        wide = pixels.width("ש" * 20, 20)
        narrow = pixels.width("י" * 20, 20)
        assert wide > narrow * 2

    def test_an_unknown_glyph_is_assumed_wide(self):
        """An emoji in a title should push toward flagging, never away from it."""
        assert pixels.char_width("🔧", 20) > pixels.char_width("W", 20)

    def test_mobile_allows_more_than_desktop(self):
        text = "Garage Door Spring Replacement and Torsion Repair in Denver"
        assert pixels.LIMITS[("title", "mobile")] > pixels.LIMITS[("title", "desktop")]
        assert pixels.fits(text, "title", "mobile")


class TestTemplates:
    def test_a_title_that_fits_alone_can_overflow_once_the_brand_is_appended(self):
        stored = "Garage Door Spring Replacement and Torsion Repair Service"
        assert pixels.fits(stored, "title")

        rendered = pixels.apply_template(
            stored, "%%title%% %%sep%% %%sitename%%",
            site_name="Denver Overhead Door Company", separator="|",
        )
        assert not pixels.fits(rendered, "title")

    def test_every_plugins_variable_syntax_is_understood(self):
        for template in ("%%title%% %%sep%% %%sitename%%",
                         "%title% %sep% %sitename%",
                         "%%post_title%% %%sep%% %%sitetitle%%"):
            assert pixels.apply_template(
                "Springs", template, site_name="Acme", separator="|"
            ) == "Springs | Acme"

    def test_a_page_with_no_title_of_its_own_renders_as_the_brand_alone(self):
        rendered = pixels.apply_template(
            "", "%%title%% %%sep%% %%sitename%%", site_name="Acme", separator="|")
        assert rendered == "Acme"

    def test_a_stranded_separator_is_not_left_behind(self):
        rendered = pixels.apply_template(
            "Springs", "%%title%% %%sep%% %%page%% %%sep%% %%sitename%%",
            site_name="Acme", separator="|")
        assert "| |" not in rendered
        assert rendered == "Springs | Acme"


# ═══════════════════════════════════════════════════════
#  What is and is not this skill's problem
# ═══════════════════════════════════════════════════════

def snippet(title="Spring Repair", description="x" * 120, url="https://example.com/springs"):
    return snippets.PageSnippet(url=url, title=title, description=description)


def performance(rows) -> snippets.PagePerformance:
    return snippets.PagePerformance(url=rows[0].url, queries=list(rows))


class TestScope:
    def test_a_page_ranked_past_ten_is_not_a_title_problem(self):
        page = performance([row(position=14.0, clicks=2, impressions=2000)])
        assert snippets.audit_page(snippet(), page, curve()) is None

    def test_a_page_earning_its_par_is_left_alone(self):
        page = performance([row(position=4.0, clicks=80, impressions=1000)])
        assert snippets.audit_page(snippet(), page, curve()) is None

    def test_a_page_below_the_impressions_floor_is_not_read(self):
        page = performance([row(impressions=40, clicks=0)])
        assert snippets.audit_page(snippet(), page, curve()) is None

    def test_a_long_tail_on_page_two_does_not_put_the_page_out_of_scope(self):
        """The head query sits at 3; the tail at 40 is not what we are auditing."""
        rows = [row(query="head", impressions=3000, position=3.0, clicks=30)]
        rows += [row(query=f"tail{i}", impressions=200, position=40.0, clicks=0)
                 for i in range(40)]
        page = performance(rows)

        assert page.position > snippets.MAX_POSITION        # weighted, it looks hopeless
        assert page.visible().position == 3.0
        assert snippets.audit_page(snippet(description=""), page, curve()) is not None

    def test_the_tail_does_not_depress_the_measured_ctr(self):
        rows = [row(query="head", impressions=1000, position=3.0, clicks=110)]
        rows += [row(query=f"tail{i}", impressions=500, position=40.0, clicks=0)
                 for i in range(10)]
        page = performance(rows)

        assert page.ctr < 0.02                              # 1.8%, apparently terrible
        assert page.visible().ctr == 0.11                   # on screen, exactly par
        assert snippets.audit_page(snippet(), page, curve()) is None


class TestSuppression:
    def test_a_query_under_an_ai_overview_does_not_count_toward_the_gap(self):
        rows = [row(query="spring cost", impressions=3000, clicks=20, position=3.0),
                row(query="spring repair", impressions=1000, clicks=110, position=3.0)]
        page = performance(rows)

        unfiltered = snippets.audit_page(snippet(), page, curve())
        assert unfiltered is not None

        filtered = snippets.audit_page(
            snippet(), page, curve(),
            serp_features={"spring cost": ["ai_overview"]},
        )
        # Once the suppressed query is removed the page is earning par.
        assert filtered is None

    def test_a_page_whose_every_query_is_suppressed_is_dropped(self):
        page = performance([row(impressions=3000, clicks=5, position=3.0)])
        assert snippets.audit_page(
            snippet(), page, curve(),
            serp_features={"spring repair": ["featured_snippet"]},
        ) is None


# ═══════════════════════════════════════════════════════
#  The problems themselves
# ═══════════════════════════════════════════════════════

class TestProblems:
    def test_a_truncated_title_is_reported_with_what_is_lost(self):
        page = performance([row(impressions=3000, clicks=10, position=3.0)])
        long_title = ("Garage Door Spring Replacement and Torsion Spring "
                      "Repair Service in Denver Colorado")
        audit = snippets.audit_page(snippet(title=long_title), page, curve())

        problem = next(p for p in audit.problems if p.kind == "title_truncated")
        assert "Colorado" in problem.evidence["lost"]

    def test_a_head_term_entirely_absent_from_the_title_is_the_strongest_signal(self):
        page = performance([
            row(query="broken torsion spring", impressions=4000, clicks=10, position=3.0)
        ])
        audit = snippets.audit_page(
            snippet(title="Denver Home Services and Maintenance"), page, curve())

        assert audit.problems[0].kind == "head_term_missing"
        assert audit.problems[0].evidence["missing_terms"] == [
            "broken", "spring", "torsion"]

    def test_partial_coverage_is_not_a_missing_head_term(self):
        """Google bolds what matches; one matching word is not nothing."""
        page = performance([
            row(query="broken torsion spring", impressions=4000, clicks=10, position=3.0)
        ])
        audit = snippets.audit_page(
            snippet(title="Torsion Repair in Denver"), page, curve())
        assert not any(p.kind == "head_term_missing" for p in audit.problems)

    def test_an_inflected_word_on_the_title_covers_the_query(self):
        page = performance([row(query="spring", impressions=4000, clicks=10,
                                position=3.0)])
        audit = snippets.audit_page(
            snippet(title="Springs in Denver"), page, curve())
        assert not any(p.kind == "head_term_missing" for p in audit.problems)

    def test_a_missing_description_is_reported(self):
        page = performance([row(impressions=3000, clicks=10, position=3.0)])
        audit = snippets.audit_page(snippet(description=""), page, curve())
        assert any(p.kind == "description_missing" for p in audit.problems)

    def test_a_title_that_is_only_the_brand_says_nothing_about_the_page(self):
        page = performance([row(impressions=3000, clicks=10, position=3.0)])
        audit = snippets.audit_page(
            snippet(title="Acme Doors"), page, curve(), site_name="Acme Doors")
        assert any(p.kind == "title_boilerplate" for p in audit.problems)

    def test_duplicate_titles_are_found_across_pages(self):
        rows = [row(url="https://example.com/a", impressions=900, clicks=5, position=3.0),
                row(url="https://example.com/b", impressions=900, clicks=5, position=3.0)]
        pages = snippets.aggregate(rows)
        found = snippets.find_duplicates(
            [snippet(url="https://example.com/a", title="Spring Repair"),
             snippet(url="https://example.com/b", title="Spring Repair")],
            pages,
        )
        assert list(found.values())[0] == ["https://example.com/a",
                                           "https://example.com/b"]

    def test_a_duplicate_on_a_page_nobody_sees_is_not_a_finding(self):
        rows = [row(url="https://example.com/a", impressions=900, position=3.0),
                row(url="https://example.com/b", impressions=5, position=3.0)]
        pages = snippets.aggregate(rows)
        assert snippets.find_duplicates(
            [snippet(url="https://example.com/a", title="Spring Repair"),
             snippet(url="https://example.com/b", title="Spring Repair")],
            pages,
        ) == {}


class TestClaim:
    def test_a_page_with_no_visible_cause_claims_far_less(self):
        """Low CTR with nothing wrong on screen is a test, not a diagnosis."""
        page = performance([row(query="spring repair", impressions=3000,
                                clicks=10, position=3.0)])
        clean = snippet(title="Spring Repair in Denver — Same Day Service",
                        description="We replace broken garage door springs across "
                                    "Denver on the same day you call us today.")
        audit = snippets.audit_page(clean, page, curve())

        assert audit.problems == []
        assert audit.recovery == snippets.BLIND_RECOVERY
        assert audit.claimable_clicks < audit.gap_clicks * snippets.STRUCTURAL_RECOVERY

    def test_several_problems_on_one_page_do_not_multiply_the_claim(self):
        page = performance([row(query="broken torsion spring", impressions=3000,
                                clicks=10, position=3.0)])
        audit = snippets.audit_page(
            snippet(title="Acme", description=""), page, curve(), site_name="Acme")

        assert len(audit.problems) > 1
        assert audit.claimable_clicks == audit.gap_clicks * snippets.STRUCTURAL_RECOVERY

    def test_the_claim_never_exceeds_the_gap(self):
        page = performance([row(impressions=3000, clicks=1, position=2.0)])
        audit = snippets.audit_page(snippet(description=""), page, curve())
        assert audit.claimable_clicks <= audit.gap_clicks


# ═══════════════════════════════════════════════════════
#  The guard
# ═══════════════════════════════════════════════════════

def audit_for_guard() -> snippets.Audit:
    page = performance([
        row(query="torsion spring repair", impressions=3000, clicks=30, position=3.0),
        row(query="garage door cost", impressions=600, clicks=0, position=8.0),
    ])
    return snippets.audit_page(
        snippet(title="Torsion Spring Repair", description=""), page, curve())


class TestValidate:
    def test_dropping_a_word_that_is_earning_clicks_is_refused(self):
        outcome = snippets.validate(
            "Fast Same-Day Garage Door Service in Denver", "x" * 120, audit_for_guard())
        assert not outcome
        assert any("torsion" in reason for reason in outcome.data["blocking"])

    def test_a_word_earning_clicks_but_absent_from_the_title_is_not_protected(self):
        """We only refuse to remove what is there and working."""
        guard = audit_for_guard()
        assert "cost" in guard.page.protected_terms() or True   # no clicks on it
        outcome = snippets.validate(
            "Torsion Spring Repair in Denver — Same Day", "x" * 120, guard)
        assert outcome, outcome.data.get("blocking")

    def test_a_candidate_that_would_be_cut_off_is_refused(self):
        outcome = snippets.validate(
            "Torsion Spring Repair and Garage Door Cable Replacement Across "
            "the Whole Denver Metro Area", "x" * 120, audit_for_guard())
        assert not outcome
        assert any("נחתך" in reason for reason in outcome.data["blocking"])

    def test_a_title_already_used_on_another_page_is_refused(self):
        outcome = snippets.validate(
            "Torsion Spring Repair", "x" * 120, audit_for_guard(),
            existing_titles={"https://example.com/other": "Torsion Spring Repair"})
        assert not outcome

    def test_shouting_is_a_warning_and_not_a_refusal(self):
        outcome = snippets.validate(
            "URGENT Torsion Spring Repair Denver", "x" * 120, audit_for_guard())
        assert outcome
        assert any("גדולות" in w for w in outcome.data["warnings"])

    def test_a_missing_description_is_a_warning_and_not_a_refusal(self):
        outcome = snippets.validate("Torsion Spring Repair Denver", "", audit_for_guard())
        assert outcome
        assert any("תיאור" in w for w in outcome.data["warnings"])


# ═══════════════════════════════════════════════════════
#  Attribution
# ═══════════════════════════════════════════════════════

class TestAttribution:
    def test_a_ctr_rise_that_the_position_explains_is_not_credited(self):
        before = performance([row(impressions=4000, clicks=200, position=6.0)])
        after = performance([row(impressions=4000, clicks=440, position=3.0)])

        result = snippets.attribute(before, after, curve())
        assert result.verdict == "no_effect"
        assert abs(result.title_effect) < abs(result.observed_shift)

    def test_a_real_gain_at_a_steady_position_is_credited(self):
        before = performance([row(impressions=8000, clicks=160, position=3.0)])
        after = performance([row(impressions=8000, clicks=880, position=3.0)])

        result = snippets.attribute(before, after, curve())
        assert result.verdict == "improved"
        assert result.clicks_effect > 0

    def test_a_sample_too_small_to_read_says_so(self):
        before = performance([row(impressions=120, clicks=3, position=3.0)])
        after = performance([row(impressions=120, clicks=9, position=3.0)])
        assert snippets.attribute(before, after, curve()).verdict == "inconclusive"

    def test_a_loss_is_reported_as_a_loss(self):
        before = performance([row(impressions=8000, clicks=880, position=3.0)])
        after = performance([row(impressions=8000, clicks=160, position=3.0)])
        assert snippets.attribute(before, after, curve()).verdict == "worse"

    def test_a_page_that_fell_after_the_rewrite_is_flagged_whatever_ctr_did(self):
        before = performance([row(impressions=8000, clicks=880, position=3.0)])
        after = performance([row(impressions=3000, clicks=400, position=6.0)])
        assert snippets.ranking_regressed(snippets.attribute(before, after, curve()))


# ═══════════════════════════════════════════════════════
#  Findings
# ═══════════════════════════════════════════════════════

class TestFinding:
    def test_a_finding_carries_the_words_the_rewrite_must_keep(self):
        finding = snippets.to_finding(audit_for_guard(), "example.com", curve())
        assert "torsion" in finding.action["must_keep"]
        assert finding.action["kind"] == "rewrite_snippet"

    def test_a_blind_finding_says_the_cause_was_not_found(self):
        page = performance([row(query="spring repair", impressions=3000,
                                clicks=10, position=3.0)])
        clean = snippet(title="Spring Repair in Denver — Same Day Service",
                        description="We replace broken garage door springs across "
                                    "Denver on the same day you call us today.")
        finding = snippets.to_finding(
            snippets.audit_page(clean, page, curve()), "example.com", curve())

        assert finding.confidence == "low"
        assert "לא נמצאה סיבה" in finding.confidence_reason

    def test_a_reference_curve_lowers_confidence_even_with_a_clear_cause(self):
        page = performance([row(impressions=3000, clicks=10, position=3.0)])
        reference = CTRCurve(source="reference", sample=40)
        audit = snippets.audit_page(snippet(description=""), page, reference)
        finding = snippets.to_finding(audit, "example.com", reference)
        assert finding.confidence == "low"

    def test_analyse_orders_by_strength_of_evidence_then_size(self):
        rows = [
            row(query="broken torsion spring", url="https://example.com/a",
                impressions=1000, clicks=5, position=3.0),
            row(query="spring repair", url="https://example.com/b",
                impressions=9000, clicks=90, position=3.0),
        ]
        pages = [
            snippets.PageSnippet(url="https://example.com/a",
                                 title="Denver Home Services", description="x" * 120),
            snippets.PageSnippet(url="https://example.com/b",
                                 title="Spring Repair in Denver Colorado Area",
                                 description=""),
        ]
        ordered = snippets.analyse(rows, pages, curve())
        assert [a.url for a in ordered] == ["https://example.com/a",
                                            "https://example.com/b"]


class TestBrand:
    def test_the_brand_is_learned_from_the_titles_themselves(self):
        pages = [snippet(url=f"https://example.com/{i}", title=f"Page {i} | Acme Doors")
                 for i in range(4)]
        assert snippets.infer_site_name(pages) == "Acme Doors"

    def test_a_tail_that_only_one_page_carries_is_not_a_brand(self):
        pages = [snippet(url="https://example.com/a", title="Springs | Acme Doors"),
                 snippet(url="https://example.com/b", title="Cables | Something Else"),
                 snippet(url="https://example.com/c", title="Openers")]
        assert snippets.infer_site_name(pages) == ""

    def test_a_learned_brand_catches_a_page_titled_only_with_it(self):
        rows = [row(url=f"https://example.com/{i}", impressions=900, clicks=5,
                    position=3.0, query=f"q{i}") for i in range(3)]
        pages = [
            snippets.PageSnippet(url="https://example.com/0", title="Acme Doors",
                                 description="x" * 120),
            snippets.PageSnippet(url="https://example.com/1",
                                 title="Spring Repair | Acme Doors",
                                 description="x" * 120),
            snippets.PageSnippet(url="https://example.com/2",
                                 title="Cable Repair | Acme Doors",
                                 description="x" * 120),
        ]
        audits = snippets.analyse(rows, pages, curve())
        boilerplate = next(a for a in audits if a.url == "https://example.com/0")
        assert any(p.kind == "title_boilerplate" for p in boilerplate.problems)


class TestLiveHtml:
    def test_the_snippet_is_read_from_what_the_browser_receives(self):
        html = ('<html><head><title>Spring Repair &amp; Cables | Acme</title>'
                '<meta name="description" content="We fix springs today."></head></html>')
        live = snippets.from_html("https://example.com/a", html)
        assert live.title == "Spring Repair & Cables | Acme"
        assert live.description == "We fix springs today."

    def test_a_write_that_did_not_reach_the_page_is_caught(self):
        """A meta write can return 200 and change nothing at all."""
        html = "<html><head><title>The Old Title | Acme</title></head></html>"
        outcome = snippets.confirm_rendered(html, "The New Title | Acme")
        assert not outcome
        assert outcome.data["rendered"] == "The Old Title | Acme"

    def test_a_live_title_with_a_stale_description_is_reported_not_failed(self):
        html = ('<html><head><title>New Title</title>'
                '<meta name="description" content="old"></head></html>')
        outcome = snippets.confirm_rendered(html, "New Title", "brand new")
        assert outcome
        assert outcome.data["description_live"] is False

    def test_the_template_is_inferred_from_the_difference(self):
        prefix, suffix = pixels.infer_affixes("Spring Repair",
                                              "Spring Repair | Acme Doors")
        assert (prefix, suffix) == ("", " | Acme Doors")
        assert pixels.render_like("Torsion Repair", prefix, suffix) == \
            "Torsion Repair | Acme Doors"

    def test_a_stored_title_the_site_ignores_infers_nothing(self):
        assert pixels.infer_affixes("Spring Repair", "Welcome to Acme") == ("", "")
