"""Tests for PageSpeed: does the field get the final word over the lab?"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from seo_core.schema import Result  # noqa: E402
from seo_core.sources import pagespeed  # noqa: E402

URL = "https://x.com/services/repair"


def crux(lcp=4200, inp=180, cls_hundredths=5, fallback=False):
    metrics = {}
    if lcp is not None:
        metrics["LARGEST_CONTENTFUL_PAINT_MS"] = {"percentile": lcp}
    if inp is not None:
        metrics["INTERACTION_TO_NEXT_PAINT"] = {"percentile": inp}
    if cls_hundredths is not None:
        metrics["CUMULATIVE_LAYOUT_SHIFT_SCORE"] = {"percentile": cls_hundredths}
    experience = {"metrics": metrics}
    if fallback:
        experience["origin_fallback"] = True
    return experience


def psi(audits=None, score=0.42, experience=None, **kwargs):
    payload = {
        "id": URL,
        "lighthouseResult": {
            "fetchTime": "2026-09-01T00:00:00.000Z",
            "finalUrl": URL,
            "categories": {"performance": {"score": score}},
            "audits": {
                "largest-contentful-paint": {"numericValue": 4600},
                "total-blocking-time": {"numericValue": 520},
                "server-response-time": {"numericValue": 900, "score": 0,
                                         "title": "Reduce server response time",
                                         "details": {"overallSavingsMs": 600}},
                **(audits or {}),
            },
        },
    }
    if experience is not False:
        payload["loadingExperience"] = experience if experience is not None else crux()
    payload.update(kwargs)
    return payload


def diagnose(**kwargs):
    result = pagespeed.parse(psi(**kwargs), url=URL, strategy="mobile")
    assert result, result.detail
    return result.data["diagnosis"]


# ═══════════════════════════════════════════════════════
#  Reading the field
# ═══════════════════════════════════════════════════════

def test_cls_is_read_as_a_fraction_not_an_integer():
    """CrUX reports CLS as hundredths. Read literally it is 50x too large."""
    assert diagnose().field.cls == pytest.approx(0.05)


def test_a_slow_lcp_in_the_field_reads_as_poor():
    field = diagnose().field
    assert field.verdict("lcp") == "poor"
    assert field.verdict("cls") == "good"
    assert field.failing == ["lcp"]


def test_origin_fallback_is_flagged_as_not_about_this_page():
    assert diagnose(experience=crux(fallback=True)).field.origin_fallback is True


def test_a_page_with_no_field_data_says_so():
    field = diagnose(experience=False).field
    assert field.has_data is False
    assert field.verdict("lcp") == "unknown"
    assert "אין נתוני שדה" in field.summary()


def test_origin_experience_is_used_when_the_url_has_none():
    payload = psi(experience=False)
    payload["originLoadingExperience"] = crux(lcp=3000)
    diagnosis = pagespeed.parse(payload, url=URL).data["diagnosis"]
    assert diagnosis.field.lcp_ms == 3000
    assert diagnosis.field.origin_fallback is True


# ═══════════════════════════════════════════════════════
#  Opportunities
# ═══════════════════════════════════════════════════════

def test_an_uncatalogued_audit_is_ignored():
    """PSI's raw list is noise. Only audits we can explain in WordPress terms."""
    diagnosis = diagnose(audits={
        "some-future-audit": {"score": 0, "details": {"overallSavingsMs": 5000}}
    })
    assert [o.audit_id for o in diagnosis.opportunities] == ["server-response-time"]


def test_a_passing_audit_is_not_an_opportunity():
    diagnosis = diagnose(audits={
        "unminified-css": {"score": 1, "title": "Minify CSS", "details": {}}
    })
    assert "unminified-css" not in [o.audit_id for o in diagnosis.opportunities]


def test_a_saving_on_a_healthy_metric_is_discounted_not_dropped():
    """A 2s lab saving on a CLS that the field already passes is not urgent."""
    diagnosis = diagnose(audits={
        "unsized-images": {"score": 0, "title": "Size images",
                           "details": {"overallSavingsMs": 2000}},
    })
    unsized = next(o for o in diagnosis.opportunities if o.audit_id == "unsized-images")
    ttfb = next(o for o in diagnosis.opportunities if o.audit_id == "server-response-time")

    assert unsized.weight == 0.2                  # CLS is good in the field
    assert ttfb.weight == 1.0                     # LCP is poor in the field

    # Cheap work on a healthy metric must not outrank work on a failing one,
    # however good its score looks in isolation.
    assert unsized.score > ttfb.score
    assert diagnosis.ranked()[0].audit_id == "server-response-time"


def test_effort_changes_the_order_not_just_the_saving():
    diagnosis = diagnose(audits={
        "unminified-css": {"score": 0, "title": "Minify CSS",
                           "details": {"overallSavingsMs": 400}},
    })
    # Both move LCP, which the field says is failing, so score decides:
    # 600ms at large effort scores 100; 400ms at small effort scores 400.
    assert diagnosis.ranked()[0].audit_id == "unminified-css"


def test_a_diagnostic_without_a_saving_still_counts_as_reducible_work():
    """bootup-time reports a total, not a saving. 30% of it is the estimate."""
    diagnosis = diagnose(audits={
        "bootup-time": {"score": 0.2, "numericValue": 3000,
                        "title": "Reduce JavaScript execution time", "details": {}},
    })
    bootup = next(o for o in diagnosis.opportunities if o.audit_id == "bootup-time")
    assert bootup.savings_ms == pytest.approx(900)


def test_third_party_wasted_time_is_read_from_the_summary():
    diagnosis = diagnose(audits={
        "third-party-summary": {"score": 0, "title": "Third-party code",
                                "details": {"summary": {"wastedMs": 820}}},
    })
    third = next(o for o in diagnosis.opportunities if o.audit_id == "third-party-summary")
    assert third.savings_ms == pytest.approx(820)


def test_every_opportunity_carries_a_wordpress_instruction():
    """The value here is the hint. A saving with no instruction is PSI's job."""
    for opportunity in diagnose().opportunities:
        assert len(opportunity.hint) > 20


# ═══════════════════════════════════════════════════════
#  Refusals
# ═══════════════════════════════════════════════════════

def test_a_response_without_lighthouse_is_refused():
    result = pagespeed.parse({"error": {"message": "rate limited"}})
    assert not result
    assert result.code == "psi_unusable"


def test_an_unknown_strategy_is_refused():
    assert pagespeed.parse(psi(), strategy="tablet").code == "bad_strategy"


def export(tmp_path, payload) -> Path:
    path = tmp_path / "psi.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_an_export_without_mobile_is_refused(tmp_path):
    """Google ranks on mobile. A desktop-only run cannot answer the question."""
    result = pagespeed.load_export(export(tmp_path, {"url": URL, "desktop": psi()}))
    assert not result
    assert result.code == "psi_no_mobile"


def test_an_export_loads_both_strategies(tmp_path):
    result = pagespeed.load_export(
        export(tmp_path, {"url": URL, "mobile": psi(), "desktop": psi(score=0.9)})
    )
    assert result
    assert set(result.data["diagnoses"]) == {"mobile", "desktop"}


def test_a_missing_export_says_how_to_produce_one(tmp_path):
    result = pagespeed.load_export(tmp_path / "absent.json")
    assert "PAGESPEED_API_KEY" in result.detail


# ═══════════════════════════════════════════════════════
#  Findings
# ═══════════════════════════════════════════════════════

def test_without_traffic_data_impact_is_zero_and_says_why():
    """Inventing a click number would rank this against real GSC findings."""
    finding = pagespeed.to_finding(diagnose(), "x.com")
    assert finding.impact_clicks == 0.0
    assert finding.impact_conversions is None
    assert "בלי נתוני תנועה" in finding.impact_basis


def test_a_failing_page_with_traffic_gets_a_conservative_click_estimate():
    finding = pagespeed.to_finding(
        diagnose(), "x.com", traffic={"clicks_28d": 1000}
    )
    assert finding.impact_clicks == pytest.approx(30.0)     # 3%
    assert "3%" in finding.impact_basis


def test_a_page_that_passes_cwv_gets_no_click_impact():
    """Passing pages have no ranking headroom left to win."""
    finding = pagespeed.to_finding(
        diagnose(experience=crux(lcp=1800, inp=120, cls_hundredths=4)),
        "x.com", traffic={"clicks_28d": 1000},
    )
    assert finding.impact_clicks == 0.0
    assert "עובר Core Web Vitals" in finding.impact_basis


def test_conversions_are_estimated_only_when_the_client_measures_them():
    finding = pagespeed.to_finding(
        diagnose(), "x.com",
        traffic={"clicks_28d": 1000, "sessions_28d": 4000, "conversion_rate": 0.03},
    )
    assert finding.impact_conversions > 0
    assert "100ms" in finding.impact_basis


def test_lab_only_data_is_held_at_low_confidence():
    finding = pagespeed.to_finding(diagnose(experience=False), "x.com")
    assert finding.confidence == "low"
    assert "מעבדה" in finding.confidence_reason


def test_a_poor_field_metric_makes_it_high_severity():
    assert pagespeed.to_finding(diagnose(), "x.com").severity == "high"


def test_a_healthy_page_is_low_severity_even_with_a_bad_lab_score():
    finding = pagespeed.to_finding(
        diagnose(experience=crux(lcp=1800, inp=120, cls_hundredths=4), score=0.30),
        "x.com",
    )
    assert finding.severity == "low"


def test_the_action_names_the_steps_and_how_to_verify():
    action = pagespeed.to_finding(diagnose(), "x.com").action
    assert action["kind"] == "speed_work_order"
    assert action["steps"]
    assert "verify" in action


# ═══════════════════════════════════════════════════════
#  Before and after
# ═══════════════════════════════════════════════════════

def test_an_improvement_in_the_field_reads_as_improved():
    before = diagnose()
    after = diagnose(experience=crux(lcp=2100))
    assert pagespeed.compare(before, after)["verdict"] == "improved"


def test_a_regression_is_named_as_one():
    before = diagnose(experience=crux(lcp=2100))
    after = diagnose(experience=crux(lcp=4800))
    assert pagespeed.compare(before, after)["verdict"] == "regressed"


def test_without_field_data_the_comparison_refuses_to_conclude():
    comparison = pagespeed.compare(diagnose(experience=False), diagnose(experience=False))
    assert comparison["verdict"] == "inconclusive"


def test_the_comparison_warns_that_field_data_lags():
    assert "28 יום" in pagespeed.compare(diagnose(), diagnose())["note"]


# ═══════════════════════════════════════════════════════
#  The total has to be a number a site could reach
# ═══════════════════════════════════════════════════════

def test_overlapping_opportunities_are_not_simply_added_up():
    """Render-blocking CSS and unoptimised images claim the same LCP seconds."""
    diagnosis = diagnose(audits={
        "render-blocking-resources": {"score": 0, "title": "Render blocking",
                                      "details": {"overallSavingsMs": 1200}},
        "modern-image-formats": {"score": 0, "title": "Next-gen formats",
                                 "details": {"overallSavingsMs": 900}},
    })
    raw = sum(o.expected_gain_ms for o in diagnosis.opportunities)
    assert raw == pytest.approx(2700)                       # 600 + 1200 + 900

    # LCP is 4200ms in the field, so 1700ms is all there is to win.
    assert diagnosis.total_expected_gain_ms == pytest.approx(1700)


def test_cls_work_is_not_counted_as_milliseconds_saved():
    """CLS is a layout score. Adding it to a time total is a category error."""
    diagnosis = diagnose(audits={
        "unsized-images": {"score": 0, "title": "Size images",
                           "details": {"overallSavingsMs": 5000}},
    })
    assert diagnosis.total_expected_gain_ms == pytest.approx(600)


def test_a_metric_already_close_to_good_has_almost_nothing_to_win():
    diagnosis = diagnose(
        experience=crux(lcp=2600),
        audits={"render-blocking-resources": {"score": 0, "title": "Render blocking",
                                              "details": {"overallSavingsMs": 3000}}},
    )
    assert diagnosis.total_expected_gain_ms == pytest.approx(100)    # 2600 → 2500


def test_without_field_data_the_lab_total_stands_uncapped():
    """Nothing to cap against — only the 0.6 lab discount and low confidence."""
    diagnosis = diagnose(experience=False)
    assert diagnosis.total_expected_gain_ms == pytest.approx(600 * 0.6)


def test_the_conversion_model_is_capped_so_it_cannot_outvote_measured_data():
    """A 2.4s saving extrapolated linearly would claim a 20% conversion lift."""
    finding = pagespeed.to_finding(
        diagnose(), "x.com",
        traffic={"clicks_28d": 1000, "sessions_28d": 10_000, "conversion_rate": 0.03},
    )
    ceiling = 10_000 * 0.03 * pagespeed.MAX_CONVERSION_UPLIFT
    assert finding.impact_conversions <= ceiling
    assert "אומדן מודל ולא מדידה" in finding.impact_basis


# ═══════════════════════════════════════════════════════
#  Local Lighthouse — for sites PSI cannot reach
# ═══════════════════════════════════════════════════════

@pytest.mark.parametrize("url", [
    "http://localhost:10003/services",
    "http://127.0.0.1/",
    "http://mysite.local/page",
    "https://dev.test/",
    "http://192.168.1.40/wp/",
])
def test_a_site_google_cannot_reach_is_recognised(url):
    assert pagespeed.is_local(url) is True


@pytest.mark.parametrize("url", ["https://example.com/", "https://sub.localhost.example.com/"])
def test_a_public_site_is_not_treated_as_local(url):
    assert pagespeed.is_local(url) is False


class FakeRun:
    """Stands in for the Lighthouse subprocess."""

    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout, self.stderr, self.returncode = stdout, stderr, returncode
        self.command = None

    def __call__(self, argv):
        self.command = argv
        return self


def test_a_lighthouse_report_is_wrapped_into_the_psi_shape():
    """Lighthouse prints exactly what PSI nests under lighthouseResult."""
    report = json.dumps(psi()["lighthouseResult"])
    result = pagespeed.fetch_local(URL, "mobile", runner=FakeRun(stdout=report))

    assert result
    parsed = pagespeed.parse(result.data["payload"], url=URL)
    assert parsed
    assert parsed.data["diagnosis"].lab.score == pytest.approx(42)


def test_a_local_run_has_no_field_data_and_says_so():
    """CrUX needs real visitors. A local site has none, and that is not a bug."""
    report = json.dumps(psi()["lighthouseResult"])
    payload = pagespeed.fetch_local(URL, "mobile", runner=FakeRun(stdout=report)).data["payload"]
    diagnosis = pagespeed.parse(payload, url=URL).data["diagnosis"]

    assert diagnosis.field.has_data is False
    finding = pagespeed.to_finding(diagnosis, "x.com")
    assert finding.confidence == "low"


def test_the_desktop_run_uses_the_desktop_preset():
    runner = FakeRun(stdout=json.dumps(psi()["lighthouseResult"]))
    pagespeed.fetch_local(URL, "desktop", runner=runner)
    assert "--preset=desktop" in runner.command


def test_a_failed_lighthouse_run_reports_its_own_error():
    result = pagespeed.fetch_local(
        URL, "mobile", runner=FakeRun(stderr="Unable to connect to Chrome", returncode=1)
    )
    assert not result
    assert "Unable to connect to Chrome" in result.detail


def test_output_that_is_not_a_report_is_refused():
    result = pagespeed.fetch_local(URL, "mobile", runner=FakeRun(stdout='{"ok": true}'))
    assert not result
    assert result.code == "lighthouse_bad_output"


def test_measure_routes_a_local_url_away_from_the_api():
    """Sending localhost to PSI wastes a call that could never have worked."""
    calls = []
    original = pagespeed.fetch_local
    pagespeed.fetch_local = lambda url, strategy="mobile", runner=None: (
        calls.append(url) or Result.success("fetched", "ok", payload={})
    )
    try:
        pagespeed.measure("http://mysite.local/page", "mobile", api_key="k")
    finally:
        pagespeed.fetch_local = original
    assert calls == ["http://mysite.local/page"]
