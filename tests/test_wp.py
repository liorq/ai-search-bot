"""Tests for the WordPress layer: capability probing, builders, SEO meta."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from seo_core.wp import content as wp_content  # noqa: E402
from seo_core.wp import seo_meta  # noqa: E402
from seo_core.wp.client import Capabilities, WordPressClient, _slug_from_url  # noqa: E402


# ═══════════════════════════════════════════════════════
#  Fixtures
# ═══════════════════════════════════════════════════════

GUTENBERG = (
    "<!-- wp:paragraph -->\n<p>Intro copy.</p>\n<!-- /wp:paragraph -->\n\n"
    '<!-- wp:heading -->\n<h2>Spring replacement cost</h2>\n<!-- /wp:heading -->\n\n'
    "<!-- wp:paragraph -->\n<p>Existing answer.</p>\n<!-- /wp:paragraph -->"
)

CLASSIC = "<p>Intro copy.</p>\n<h2>Spring replacement cost</h2>\n<p>Existing answer.</p>"

ELEMENTOR_TREE = [
    {
        "id": "sec1", "elType": "section",
        "elements": [
            {
                "id": "col1", "elType": "column",
                "elements": [
                    {
                        "id": "w1", "elType": "widget", "widgetType": "heading",
                        "settings": {"title": "Spring replacement cost"},
                        "elements": [],
                    },
                    {
                        "id": "w2", "elType": "widget", "widgetType": "text-editor",
                        "settings": {"editor": "<p>Existing answer.</p>"},
                        "elements": [],
                    },
                ],
            }
        ],
    }
]


def gutenberg_post() -> dict:
    return {"id": 10, "content": {"raw": GUTENBERG}, "meta": {}}


def classic_post() -> dict:
    return {"id": 11, "content": {"raw": CLASSIC}, "meta": {}}


def elementor_post(tree=None, mode="builder") -> dict:
    meta = {wp_content.ELEMENTOR_MODE_KEY: mode}
    if tree is not None:
        meta[wp_content.ELEMENTOR_DATA_KEY] = json.dumps(tree)
    return {
        "id": 12,
        # Elementor keeps a stale copy here. Editing it changes nothing on screen.
        "content": {"raw": "<p>Stale fallback copy that is never rendered.</p>"},
        "meta": meta,
    }


# ═══════════════════════════════════════════════════════
#  Builder detection
# ═══════════════════════════════════════════════════════

def test_detects_gutenberg_from_block_markup():
    assert wp_content.detect_builder(gutenberg_post()) == "gutenberg"


def test_detects_classic_when_no_blocks_present():
    assert wp_content.detect_builder(classic_post()) == "classic"


def test_detects_elementor_from_edit_mode_meta():
    assert wp_content.detect_builder(elementor_post(ELEMENTOR_TREE)) == "elementor"


def test_page_reverted_from_elementor_is_not_treated_as_elementor():
    """The meta key survives a revert to the classic editor; the value is what counts."""
    post = elementor_post(ELEMENTOR_TREE, mode="")
    assert wp_content.detect_builder(post) != "elementor"


def test_elementor_page_without_data_refuses_to_load():
    """Almost always means meta is not exposed in REST — writing here is invisible."""
    result = wp_content.load(elementor_post(tree=None))
    assert not result
    assert result.code == "elementor_data_missing"
    assert result.recoverable is True


def test_corrupt_elementor_data_is_unrecoverable():
    post = elementor_post(ELEMENTOR_TREE)
    post["meta"][wp_content.ELEMENTOR_DATA_KEY] = "{not json"
    result = wp_content.load(post)
    assert not result
    assert result.recoverable is False


# ═══════════════════════════════════════════════════════
#  The hash that modified_gmt would miss
# ═══════════════════════════════════════════════════════

def test_hash_changes_when_only_the_elementor_tree_changes():
    """post_content is identical in both; only the widget tree differs."""
    before = wp_content.load(elementor_post(ELEMENTOR_TREE)).data["content"]

    edited = json.loads(json.dumps(ELEMENTOR_TREE))
    edited[0]["elements"][0]["elements"][1]["settings"]["editor"] = "<p>Someone else.</p>"
    after = wp_content.load(elementor_post(edited)).data["content"]

    assert before.raw_content == after.raw_content
    assert before.hash != after.hash


# ═══════════════════════════════════════════════════════
#  Gutenberg editing
# ═══════════════════════════════════════════════════════

def test_gutenberg_insert_produces_valid_block_markup():
    post = wp_content.load(gutenberg_post()).data["content"]
    result = wp_content.insert_paragraph_after_heading(
        post, "Spring replacement cost", "Most Denver springs run $180-$350."
    )
    assert result
    updated = result.data["payload"]["content"]
    assert "<!-- wp:paragraph -->" in updated
    assert updated.count("<!-- /wp:paragraph -->") == 3
    # The new copy lands after the heading, not at the end of the post.
    assert updated.index("$180") > updated.index("Spring replacement cost")
    assert updated.index("$180") < updated.index("Existing answer")


def test_gutenberg_inverse_restores_the_original():
    post = wp_content.load(gutenberg_post()).data["content"]
    result = wp_content.insert_paragraph_after_heading(post, "Spring replacement cost", "New.")
    assert result.data["inverse"]["content"] == GUTENBERG


def test_nested_blocks_are_not_torn_apart():
    """A naive split on '<!-- wp:' would break a group block in half."""
    nested = (
        "<!-- wp:group -->\n<div class=\"wp-block-group\">\n"
        "<!-- wp:paragraph -->\n<p>Inside.</p>\n<!-- /wp:paragraph -->\n"
        "</div>\n<!-- /wp:group -->\n\n"
        "<!-- wp:heading -->\n<h2>Target</h2>\n<!-- /wp:heading -->"
    )
    blocks = wp_content._split_top_level_blocks(nested)
    assert len(blocks) == 2
    assert blocks[0].count("wp:paragraph") == 2       # open and close, intact


def test_missing_heading_is_reported_not_guessed():
    post = wp_content.load(gutenberg_post()).data["content"]
    result = wp_content.insert_paragraph_after_heading(post, "No such heading", "Text.")
    assert not result
    assert result.code == "heading_not_found"


def test_heading_match_ignores_case_and_whitespace():
    post = wp_content.load(gutenberg_post()).data["content"]
    assert wp_content.insert_paragraph_after_heading(
        post, "  spring REPLACEMENT   cost ", "Text."
    )


# ═══════════════════════════════════════════════════════
#  Elementor editing
# ═══════════════════════════════════════════════════════

def test_elementor_insert_writes_the_tree_not_post_content():
    """The whole point: an Elementor edit must land in meta, never post_content."""
    post = wp_content.load(elementor_post(ELEMENTOR_TREE)).data["content"]
    result = wp_content.insert_paragraph_after_heading(post, "Spring replacement cost", "New copy.")
    assert result
    payload = result.data["payload"]
    assert "content" not in payload
    assert wp_content.ELEMENTOR_DATA_KEY in payload["meta"]


def test_elementor_widget_lands_in_the_same_column():
    post = wp_content.load(elementor_post(ELEMENTOR_TREE)).data["content"]
    result = wp_content.insert_paragraph_after_heading(post, "Spring replacement cost", "New copy.")
    tree = json.loads(result.data["payload"]["meta"][wp_content.ELEMENTOR_DATA_KEY])
    widgets = tree[0]["elements"][0]["elements"]
    assert len(widgets) == 3
    assert widgets[1]["widgetType"] == "text-editor"
    assert "New copy." in widgets[1]["settings"]["editor"]
    assert widgets[2]["id"] == "w2"                   # existing content pushed down


def test_elementor_edit_clears_the_css_cache():
    """Without the bump, the page can render with the previous layout's styles."""
    post = wp_content.load(elementor_post(ELEMENTOR_TREE)).data["content"]
    result = wp_content.insert_paragraph_after_heading(post, "Spring replacement cost", "New.")
    assert result.data["payload"]["meta"][wp_content.ELEMENTOR_CSS_KEY] == ""
    assert result.data["needs_css_regeneration"] is True


def test_elementor_source_tree_is_not_mutated():
    original = json.loads(json.dumps(ELEMENTOR_TREE))
    post = wp_content.load(elementor_post(ELEMENTOR_TREE)).data["content"]
    wp_content.insert_paragraph_after_heading(post, "Spring replacement cost", "New.")
    assert post.elementor_tree == original


def test_elementor_inverse_restores_the_original_tree():
    post = wp_content.load(elementor_post(ELEMENTOR_TREE)).data["content"]
    result = wp_content.insert_paragraph_after_heading(post, "Spring replacement cost", "New.")
    restored = json.loads(result.data["inverse"]["meta"][wp_content.ELEMENTOR_DATA_KEY])
    assert restored == ELEMENTOR_TREE


def test_plain_text_reads_the_tree_not_the_stale_fallback():
    post = wp_content.load(elementor_post(ELEMENTOR_TREE)).data["content"]
    assert "Spring replacement cost" in post.plain_text
    assert "Stale fallback" not in post.plain_text


# ═══════════════════════════════════════════════════════
#  Escaping
# ═══════════════════════════════════════════════════════

def test_plain_text_is_escaped():
    post = wp_content.load(classic_post()).data["content"]
    result = wp_content.insert_paragraph_after_heading(
        post, "Spring replacement cost", "Costs < $300 & up"
    )
    assert "&lt; $300 &amp; up" in result.data["payload"]["content"]


def test_intentional_markup_survives():
    """Copy often carries a deliberate internal link; escaping it shows tags to readers."""
    post = wp_content.load(classic_post()).data["content"]
    result = wp_content.insert_paragraph_after_heading(
        post, "Spring replacement cost", 'See our <a href="/torsion">torsion guide</a>.'
    )
    assert '<a href="/torsion">' in result.data["payload"]["content"]


def test_empty_paragraph_is_rejected():
    post = wp_content.load(classic_post()).data["content"]
    assert not wp_content.insert_paragraph_after_heading(post, "Spring replacement cost", "   ")


# ═══════════════════════════════════════════════════════
#  SEO plugin fields
# ═══════════════════════════════════════════════════════

def test_plugin_detected_from_meta_keys():
    assert seo_meta.detect_plugin({"rank_math_title": "X"}) == "rankmath"
    assert seo_meta.detect_plugin({"_yoast_wpseo_title": "X"}) == "yoast"
    assert seo_meta.detect_plugin({"_seopress_titles_title": "X"}) == "seopress"


def test_plugin_detected_from_rendered_html_when_meta_is_bare():
    html = "<!-- This site is optimized with the Yoast SEO plugin -->"
    assert seo_meta.detect_plugin({}, html) == "yoast"


def test_unknown_plugin_is_admitted_not_guessed():
    assert seo_meta.detect_plugin({}, "<html></html>") == "unknown"


def test_writing_to_an_unknown_plugin_is_refused():
    """Writing Yoast's key on a Rank Math site succeeds and changes nothing."""
    result = seo_meta.write_payload("unknown", title="New title")
    assert not result
    assert result.code == "plugin_unknown"


def test_write_payload_uses_the_right_keys():
    result = seo_meta.write_payload("rankmath", title="T", description="D")
    assert result.data["meta"] == {"rank_math_title": "T", "rank_math_description": "D"}


def test_noindex_is_never_writable_here():
    result = seo_meta.write_payload("yoast", title="T")
    assert "noindex" not in json.dumps(result.data["meta"])


def test_rankmath_stores_robots_as_a_list():
    fields = seo_meta.read({"rank_math_robots": ["noindex", "nofollow"]}, "rankmath").data["fields"]
    assert fields.is_noindexed is True


def test_yoast_stores_noindex_as_a_flag():
    assert seo_meta.read({"_yoast_wpseo_meta-robots-noindex": "1"}, "yoast").data["fields"].noindex
    assert not seo_meta.read({"_yoast_wpseo_meta-robots-noindex": "0"}, "yoast").data["fields"].noindex


def test_inverse_restores_a_previously_absent_key_as_empty():
    """Dropping the key from the restore would leave our value in place."""
    inverse = seo_meta.inverse_payload({}, ["rank_math_title"])
    assert inverse == {"rank_math_title": ""}


# ═══════════════════════════════════════════════════════
#  Capability probe gates every write
# ═══════════════════════════════════════════════════════

class FakeTransport:
    def __init__(self, responses: dict):
        self.responses = responses
        self.calls: list[tuple[str, str]] = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url))
        # Match on the end of the URL first. "/wp-json/" is contained in every
        # route below it, so substring matching alone would shadow them all.
        for fragment, response in self.responses.items():
            if url.rstrip("/").endswith(fragment.rstrip("/")):
                return response
        for fragment in sorted(self.responses, key=len, reverse=True):
            if fragment in url:
                return self.responses[fragment]
        raise AssertionError(f"unexpected call: {url}")


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


def client_with(responses) -> WordPressClient:
    return WordPressClient("https://x.com", "u", "p", transport=FakeTransport(responses))


def test_write_without_probing_is_refused():
    client = client_with({})
    result = client.update_post(1, content="<p>x</p>")
    assert not result
    assert result.code == "not_probed"
    assert client._transport.calls == []          # nothing was sent


def test_write_without_edit_permission_is_refused():
    client = client_with({})
    client.capabilities = Capabilities(reachable=True, authenticated=True,
                                       can_edit={"post": False})
    result = client.update_post(1, content="<p>x</p>")
    assert not result
    assert result.code == "no_permission"


def test_meta_write_is_refused_when_meta_is_not_exposed():
    """This is the failure that returns 200 and silently discards the field."""
    client = client_with({})
    client.capabilities = Capabilities(reachable=True, authenticated=True,
                                       can_edit={"post": True},
                                       meta_exposed={"post": False})
    result = client.update_post(1, meta={"rank_math_title": "T"})
    assert not result
    assert result.code == "meta_not_exposed"


def test_content_write_is_allowed_when_only_meta_is_blocked():
    client = client_with({"/posts/1": FakeResponse(200, {"id": 1})})
    client.capabilities = Capabilities(reachable=True, authenticated=True,
                                       can_edit={"post": True},
                                       meta_exposed={"post": False})
    assert client.update_post(1, content="<p>x</p>")


def test_probe_reports_an_unreachable_site():
    class Dead:
        def request(self, *a, **k):
            raise ConnectionError("refused")

    caps = WordPressClient("https://x.com", "u", "p", transport=Dead()).probe()
    assert caps.reachable is False
    assert "לא נגיש" in caps.summary()


def test_probe_stops_at_a_rejected_password():
    client = client_with({"/wp-json/": FakeResponse(200, {"routes": {}}),
                          "users/me": FakeResponse(401)})
    caps = client.probe()
    assert caps.reachable is True
    assert caps.authenticated is False
    assert caps.can_write("post") is False


def test_probe_reads_capabilities_and_meta_exposure():
    routes = {"routes": {"/wp/v2/posts/(?P<id>[\\d]+)": {
        "endpoints": [{"args": {"meta": {}, "content": {}}}]}}}
    client = client_with({
        "users/me": FakeResponse(200, {"id": 3, "roles": ["editor"],
                                       "capabilities": {"edit_posts": True}}),
        "/wp-json/": FakeResponse(200, routes),
    })
    caps = client.probe(post_types=("post",))
    assert caps.can_write("post") is True
    assert caps.can_write_meta("post") is True


def test_probe_falls_back_to_role_when_capabilities_are_hidden():
    client = client_with({
        "users/me": FakeResponse(200, {"id": 3, "roles": ["subscriber"]}),
        "/wp-json/": FakeResponse(200, {"routes": {}}),
    })
    caps = client.probe(post_types=("post",))
    assert caps.can_edit["post"] is False
    assert any("הרשאת עריכה" in b for b in caps.blockers)


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://x.com/spring-repair/", "spring-repair"),
        ("https://x.com/services/spring-repair", "spring-repair"),
        ("https://x.com/old-page.html", "old-page"),
        ("https://x.com/", ""),
    ],
)
def test_slug_extraction(url, expected):
    assert _slug_from_url(url) == expected


def test_an_empty_meta_value_hashes_the_same_as_an_absent_one():
    """WordPress cannot unset a meta key over REST, so a restore writes "" where
    the key had been absent. Hashing those differently makes every restored page
    look like somebody else edited it."""
    absent = wp_content.load({"id": 1, "content": {"raw": CLASSIC}, "meta": {}}, "pages")
    empty = wp_content.load(
        {"id": 1, "content": {"raw": CLASSIC}, "meta": {"_elementor_css": ""}}, "pages"
    )
    assert absent.data["content"].hash == empty.data["content"].hash


def test_a_real_meta_change_still_moves_the_hash():
    before = wp_content.load({"id": 1, "content": {"raw": CLASSIC}, "meta": {}}, "pages")
    after = wp_content.load(
        {"id": 1, "content": {"raw": CLASSIC}, "meta": {"_yoast_wpseo_title": "New"}}, "pages"
    )
    assert before.data["content"].hash != after.data["content"].hash


def test_list_headings_reads_every_heading_in_order():
    page = wp_content.load(gutenberg_post(), "pages").data["content"]
    assert wp_content.list_headings(page) == ["Spring replacement cost"]


def test_list_headings_walks_the_elementor_widget_tree():
    page = wp_content.load(elementor_post(ELEMENTOR_TREE), "pages").data["content"]
    assert wp_content.list_headings(page) == ["Spring replacement cost"]


# ═══════════════════════════════════════════════════════
#  Whole sections — a heading and its answer
# ═══════════════════════════════════════════════════════

def test_a_gutenberg_section_lands_after_the_whole_previous_section():
    """Inserting straight after the heading block would split its answer in two."""
    page = wp_content.load(gutenberg_post(), "pages").data["content"]
    result = wp_content.insert_section_after_heading(
        page, "Spring replacement cost", "How long does it take?", "About two hours."
    )
    assert result
    markup = result.data["payload"]["content"]
    assert markup.index("Existing answer") < markup.index("How long does it take?")
    assert "<!-- wp:heading -->" in markup
    assert "<h2>How long does it take?</h2>" in markup


def test_a_classic_section_lands_before_the_next_heading():
    page = wp_content.load(classic_post(), "pages").data["content"]
    result = wp_content.insert_section_after_heading(
        page, "Spring replacement cost", "Warranty", "Ten years on parts."
    )
    assert result
    markup = result.data["payload"]["content"]
    assert markup.index("Existing answer") < markup.index("Warranty")
    assert "<h2>Warranty</h2>" in markup


def test_an_elementor_section_adds_a_heading_widget_above_its_text():
    page = wp_content.load(elementor_post(ELEMENTOR_TREE), "pages").data["content"]
    result = wp_content.insert_section_after_heading(
        page, "Spring replacement cost", "Warranty", "Ten years on parts."
    )
    assert result
    tree = json.loads(result.data["payload"]["meta"][wp_content.ELEMENTOR_DATA_KEY])
    widgets = tree[0]["elements"][0]["elements"]
    types = [w.get("widgetType") for w in widgets]

    assert types.count("heading") == 2
    assert types.index("heading", 1) < types.index("text-editor", 1)
    assert widgets[types.index("heading", 1)]["settings"]["header_size"] == "h2"


def test_an_elementor_section_writes_meta_and_never_post_content():
    """post_content on an Elementor page is a dead copy nobody renders."""
    page = wp_content.load(elementor_post(ELEMENTOR_TREE), "pages").data["content"]
    payload = wp_content.insert_section_after_heading(
        page, "Spring replacement cost", "Warranty", "Ten years."
    ).data["payload"]
    assert "content" not in payload
    assert wp_content.ELEMENTOR_CSS_KEY in payload["meta"]


def test_a_section_needs_both_a_heading_and_a_body():
    page = wp_content.load(classic_post(), "pages").data["content"]
    assert not wp_content.insert_section_after_heading(page, "Spring replacement cost", "", "x")
    assert not wp_content.insert_section_after_heading(page, "Spring replacement cost", "x", " ")


def test_a_section_after_a_heading_that_is_not_there_is_refused():
    page = wp_content.load(classic_post(), "pages").data["content"]
    result = wp_content.insert_section_after_heading(page, "Absent", "New", "Body")
    assert not result
    assert result.code == "heading_not_found"


def test_the_inverse_of_a_section_restores_the_original_exactly():
    page = wp_content.load(gutenberg_post(), "pages").data["content"]
    result = wp_content.insert_section_after_heading(
        page, "Spring replacement cost", "Warranty", "Ten years."
    )
    assert result.data["inverse"]["content"] == GUTENBERG
