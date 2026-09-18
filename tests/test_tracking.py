"""
Conversion measurement — what the page proves, and what only GTM knows.
======================================================================
"""

from __future__ import annotations

from seo_core.sources import tracking as t
from seo_core.sources.queries import QueryRow

GA4 = '<script src="https://www.googletagmanager.com/gtag/js?id=G-ABC1234567"></script>' \
      "<script>gtag('config', 'G-ABC1234567');</script>"
GTM = '<script>(function(w,d,s,l,i){})(window,document,"script","dataLayer","GTM-AB12CD");</script>'


def pages(*htmls) -> list[t.PageTags]:
    return [t.read_page(f"https://x.com/p{i}", html)
            for i, html in enumerate(htmls)]


def kinds(issues) -> set[str]:
    return {i.kind for i in issues}


# ═══════════════════════════════════════════════════════
#  Reading one page
# ═══════════════════════════════════════════════════════

class TestReadPage:
    def test_a_measurement_id_and_its_config_are_both_seen(self):
        page = t.read_page("https://x.com/", GA4)
        assert page.distinct_ga4 == ["G-ABC1234567"]
        assert page.has_ga4_config
        assert not page.gtm_only

    def test_a_container_without_a_visible_config_is_marked_unverifiable(self):
        page = t.read_page("https://x.com/", GTM)
        assert page.gtm_ids == ["GTM-AB12CD"]
        assert page.gtm_only

    def test_contact_links_are_counted_by_kind(self):
        html = ('<a href="tel:+13035551234">Call</a>'
                '<a href="mailto:a@b.com">Mail</a>'
                '<a href="https://wa.me/972500000000">WhatsApp</a>')
        page = t.read_page("https://x.com/", html)
        assert page.contact_links == {"phone_click": 1, "email_click": 1,
                                      "whatsapp_click": 1}

    def test_a_search_box_is_not_counted_as_a_lead_form(self):
        html = '<form role="search"><input></form><form><input></form>'
        assert t.read_page("https://x.com/", html).forms == 1

    def test_a_thank_you_url_is_recognised(self):
        assert t.read_page("https://x.com/thank-you/", "").is_thank_you
        assert not t.read_page("https://x.com/services/", "").is_thank_you


# ═══════════════════════════════════════════════════════
#  What the markup settles
# ═══════════════════════════════════════════════════════

class TestFacts:
    def test_the_same_id_loaded_twice_doubles_every_number(self):
        issues = t.audit_pages(pages(GA4 + GA4))
        assert "ga4_duplicated" in kinds(issues)

    def test_two_different_properties_are_two_reports_that_disagree(self):
        second = GA4.replace("G-ABC1234567", "G-ZZZ9999999")
        issues = t.audit_pages(pages(GA4, second))
        assert "ga4_multiple_properties" in kinds(issues)

    def test_a_hole_in_the_coverage_is_the_finding(self):
        issues = t.audit_pages(pages(GA4, GA4, GA4, "<html>nothing</html>"))
        missing = next(i for i in issues if i.kind == "analytics_missing")
        assert missing.evidence == {"missing": 1, "crawled": 4}
        assert missing.severity == "high"

    def test_a_site_with_no_analytics_anywhere_is_not_reported_as_a_hole(self):
        """That is a different conversation, and not one a hole describes."""
        assert "analytics_missing" not in kinds(t.audit_pages(pages("", "")))

    def test_universal_analytics_still_loading_is_raised(self):
        issues = t.audit_pages(pages(GA4 + '<script>UA-12345-1</script>'))
        assert "universal_analytics" in kinds(issues)

    def test_a_clean_site_produces_nothing(self):
        assert t.audit_pages(pages(GA4, GA4, GA4)) == []


class TestLimits:
    def test_a_container_is_reported_as_unverifiable_and_never_as_a_pass(self):
        issues = t.audit_pages(pages(GTM, GTM))
        issue = next(i for i in issues if i.kind == "gtm_not_inspectable")
        assert not issue.verifiable
        assert len(issue.evidence["checklist"]) == 4

    def test_contact_links_are_always_unverifiable_from_the_page(self):
        issues = t.audit_pages(pages(GA4 + '<a href="tel:+1234567890">c</a>'))
        issue = next(i for i in issues if i.kind == "contact_clicks_unverified")
        assert not issue.verifiable

    def test_a_consent_banner_is_named_and_flagged(self):
        html = GA4 + '<script src="https://consent.cookiebot.com/uc.js"></script>'
        issue = next(i for i in t.audit_pages(pages(html))
                     if i.kind == "consent_gate")
        assert issue.evidence["platform"] == "cookiebot"

    def test_forms_with_no_thank_you_page_anywhere_are_raised(self):
        issues = t.audit_pages(pages(GA4 + "<form><input></form>"))
        assert "no_success_page" in kinds(issues)

    def test_a_thank_you_page_in_the_crawl_settles_it(self):
        both = [t.read_page("https://x.com/contact", GA4 + "<form><input></form>"),
                t.read_page("https://x.com/thank-you", GA4)]
        assert "no_success_page" not in kinds(t.audit_pages(both))


# ═══════════════════════════════════════════════════════
#  Clicks against sessions
# ═══════════════════════════════════════════════════════

class TestSessions:
    def rows(self, url, clicks):
        return [QueryRow(query="q", url=url, clicks=clicks,
                         impressions=clicks * 20, position=4.0)]

    def test_a_tag_that_is_not_firing_shows_as_a_gap(self):
        gaps = t.compare_sessions(self.rows("https://x.com/a", 900),
                                  {"https://x.com/a": 40})
        assert gaps[0].direction == "under"
        assert "תג שלא נורה" in gaps[0].describe()

    def test_ordinary_attribution_difference_is_not_a_gap(self):
        assert t.compare_sessions(self.rows("https://x.com/a", 900),
                                  {"https://x.com/a": 820}) == []

    def test_double_counting_shows_in_the_other_direction(self):
        gaps = t.compare_sessions(self.rows("https://x.com/a", 900),
                                  {"https://x.com/a": 2600})
        assert gaps[0].direction == "over"

    def test_a_page_too_small_to_read_is_left_alone(self):
        assert t.compare_sessions(self.rows("https://x.com/a", 40),
                                  {"https://x.com/a": 1}) == []

    def test_tracking_parameters_do_not_split_a_page_in_two(self):
        gaps = t.compare_sessions(self.rows("https://x.com/a", 900),
                                  {"https://x.com/a?utm_source=google": 40})
        assert len(gaps) == 1

    def test_a_page_with_no_session_data_is_skipped_and_not_assumed_zero(self):
        assert t.compare_sessions(self.rows("https://x.com/a", 900), {}) == []


# ═══════════════════════════════════════════════════════
#  The verdict
# ═══════════════════════════════════════════════════════

class TestVerdict:
    def test_a_doubled_tag_makes_every_number_untrustworthy(self):
        audit = t.Audit(pages=pages(GA4 + GA4))
        audit.issues = t.audit_pages(audit.pages)
        assert not audit.trustworthy

    def test_a_container_alone_does_not_condemn_the_data(self):
        """It cannot be verified here; that is not the same as being wrong."""
        audit = t.Audit(pages=pages(GTM, GTM))
        audit.issues = t.audit_pages(audit.pages)
        assert audit.trustworthy

    def test_a_clean_site_is_trustworthy(self):
        audit = t.Audit(pages=pages(GA4, GA4))
        audit.issues = t.audit_pages(audit.pages)
        assert audit.trustworthy


class TestFindings:
    def test_no_finding_here_claims_clicks(self):
        audit = t.Audit(pages=pages(GA4 + GA4))
        audit.issues = t.audit_pages(audit.pages)
        findings = t.to_findings(audit, "x.com")
        assert all(f.impact_clicks == 0.0 for f in findings)

    def test_an_unverifiable_issue_never_claims_high_confidence(self):
        audit = t.Audit(pages=pages(GTM))
        audit.issues = t.audit_pages(audit.pages)
        finding = next(f for f in t.to_findings(audit, "x.com")
                       if f.type == "gtm_not_inspectable")
        assert finding.confidence == "medium"
        assert "רק המכולה" in finding.confidence_reason


class TestCorrectInstallation:
    def test_a_correct_install_names_its_id_twice_and_is_not_a_duplicate(self):
        """The loader and the config both carry the ID. Counting bare mentions
        would report every properly tagged site on earth as double counting."""
        page = t.read_page("https://x.com/", GA4)
        assert page.mentions.count("G-ABC1234567") == 2
        assert page.duplicated_ga4 == []

    def test_two_loaders_for_one_id_is_a_duplicate(self):
        loader = '<script src="https://www.googletagmanager.com/gtag/js?id=G-ABC1234567"></script>'
        assert t.read_page("https://x.com/", loader * 2).duplicated_ga4 == \
            ["G-ABC1234567"]

    def test_two_config_calls_for_one_id_is_a_duplicate(self):
        config = "<script>gtag('config', 'G-ABC1234567');</script>"
        assert t.read_page("https://x.com/", config * 2).duplicated_ga4 == \
            ["G-ABC1234567"]

    def test_one_loader_configuring_two_properties_is_not_a_duplicate(self):
        html = GA4 + "<script>gtag('config', 'G-ZZZ9999999');</script>"
        page = t.read_page("https://x.com/", html)
        assert page.duplicated_ga4 == []
        assert len(page.distinct_ga4) == 2
