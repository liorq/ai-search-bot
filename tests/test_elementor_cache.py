"""A write nobody can see is not a write.

Measured on Elementor 4.2 (my-site.local, 2026-09-20): the widget tree saved
through REST every time, and the visitor kept getting the old HTML. Elementor
caches a page's rendered markup per document in `_elementor_element_cache`,
and only its own save path clears it — a new widget stayed invisible, and so
did an edit to an existing one. `DELETE /elementor/v1/cache` made both appear.

`_elementor_css`, which the toolkit used to blank on every edit, is not
exposed in REST at all: that write was accepted and dropped.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from seo_core.wp.client import Capabilities, WordPressClient   # noqa: E402

BASE = "http://my-site.local"
TREE = json.dumps([{"id": "a1", "elType": "container", "settings": {}, "elements": []}])


class Response:
    def __init__(self, status=200, payload=None, empty_body=False):
        self.status_code, self._payload = status, payload if payload is not None else {"id": 7}
        self.url, self.history, self.headers = BASE, [], {}
        self.empty_body = empty_body

    def json(self):
        if self.empty_body:
            raise ValueError("no JSON in an empty body")
        return self._payload


class Transport:
    """Records what was sent, and can fail one route on demand.

    The cache route answers 200 with an EMPTY body, which is what the real
    endpoint does — the first version of this code read that as a failure.
    """

    def __init__(self, cache_status=200):
        self.sent: list[tuple[str, str]] = []
        self.cache_status = cache_status

    def request(self, method, url, **kwargs):
        self.sent.append((method, url))
        if url.endswith("/elementor/v1/cache"):
            return Response(self.cache_status, empty_body=True)
        return Response()

    def calls_to(self, fragment):
        return [(m, u) for m, u in self.sent if fragment in u]


def ready_client(transport):
    wp = WordPressClient(BASE, "u", "p", transport=transport)
    wp.capabilities = Capabilities(reachable=True, authenticated=True,
                                   can_edit={"page": True}, meta_exposed={"page": True})
    return wp


def test_writing_the_widget_tree_clears_the_rendered_markup():
    transport = Transport()
    written = ready_client(transport).update_post(
        7, post_type="pages", meta={"_elementor_data": TREE})
    assert written and written.data["cache_cleared"] is True
    assert transport.calls_to("/elementor/v1/cache") == [("DELETE", f"{BASE}/wp-json/elementor/v1/cache")]


def test_the_cache_is_cleared_after_the_write_not_before():
    transport = Transport()
    ready_client(transport).update_post(7, post_type="pages", meta={"_elementor_data": TREE})
    urls = [u for _, u in transport.sent]
    assert urls.index(f"{BASE}/wp-json/wp/v2/pages/7") < urls.index(f"{BASE}/wp-json/elementor/v1/cache")


def test_an_ordinary_write_does_not_touch_elementor():
    # A Gutenberg or classic page renders from post_content; there is nothing
    # to re-render, and clearing a whole site's cache for it would be rude.
    transport = Transport()
    ready_client(transport).update_post(7, post_type="pages", content="<p>hello</p>")
    assert transport.calls_to("/elementor/v1/cache") == []


def test_a_yoast_only_write_does_not_touch_elementor():
    transport = Transport()
    ready_client(transport).update_post(
        7, post_type="pages", meta={"_yoast_wpseo_title": "New title"})
    assert transport.calls_to("/elementor/v1/cache") == []


def test_a_cache_that_will_not_clear_is_reported_and_the_write_still_stands():
    # The page was written; what is in doubt is whether anyone will see it.
    # Saying so beats both silence and throwing the write away.
    transport = Transport(cache_status=500)
    written = ready_client(transport).update_post(
        7, post_type="pages", meta={"_elementor_data": TREE})
    assert written
    assert written.data["cache_cleared"] is False
    assert "נכשל" in written.data["cache_detail"]


def test_an_empty_body_is_success_because_that_is_what_the_endpoint_returns():
    transport = Transport()
    assert ready_client(transport).clear_elementor_cache()


def test_an_older_elementor_without_the_endpoint_says_what_it_means():
    transport = Transport(cache_status=404)
    cleared = ready_client(transport).clear_elementor_cache()
    assert not cleared and cleared.code == "cache_endpoint_missing"
    assert "לא יופיע" in cleared.detail or "עלול לא להופיע" in cleared.detail
