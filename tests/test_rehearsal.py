"""Tests for the staging rehearsal — the drill that proves a restore works
before any client's live site is written to for the first time."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from seo_core.schema import Result  # noqa: E402
from seo_core.wp import content as wp_content  # noqa: E402
from seo_core.wp import rehearsal  # noqa: E402
from seo_core.wp.client import Capabilities  # noqa: E402

URL = "https://staging.x.com/services"
CLASSIC = "<p>Intro.</p>\n<h2>Spring replacement cost</h2>\n<p>Answer.</p>"


class FakeWP:
    """An in-memory WordPress. Enough surface for the drill, nothing more."""

    def __init__(self, *, writable=True, silent_discard=False, fail_on_call=None,
                 post=None):
        self.post = post or {"id": 7, "content": {"raw": CLASSIC}, "meta": {}}
        self.writable = writable
        self.silent_discard = silent_discard
        self.fail_on_call = fail_on_call        # 1 = the write, 2 = the restore
        self.writes = 0

    def probe(self):
        if not self.writable:
            return Capabilities(reachable=True, authenticated=True,
                                can_edit={"page": False},
                                blockers=["אין הרשאת עריכה ל-page"])
        return Capabilities(reachable=True, authenticated=True,
                            can_edit={"page": True, "post": True},
                            meta_exposed={"page": True, "post": True})

    def find_by_url(self, url):
        return Result.success("found", "נמצא", payload=self.post, post_type="pages")

    def get_post(self, post_id, post_type="posts"):
        return Result.success("ok", "בוצע", payload=self.post)

    def update_post(self, post_id, *, post_type="posts", content=None, meta=None,
                    title=None):
        self.writes += 1
        if self.fail_on_call == self.writes:
            return Result.failure("forbidden", "השרת דחה את הכתיבה")
        if self.silent_discard:
            return Result.success("ok", "בוצע")       # 200, and nothing changed
        if content is not None:
            self.post["content"]["raw"] = content
        if meta:
            self.post["meta"].update(meta)
        return Result.success("ok", "בוצע")


def original_hash(wp: FakeWP) -> str:
    return wp_content.load(wp.post, "pages").data["content"].hash


def rendered(wp: FakeWP):
    """A fetcher that serves whatever is currently stored."""
    def fetch(url):
        return 200, f"<html><body>{wp.post['content']['raw']}</body></html>"
    return fetch


# ═══════════════════════════════════════════════════════
#  The happy path
# ═══════════════════════════════════════════════════════

def test_a_clean_drill_writes_verifies_and_puts_the_page_back(tmp_path):
    wp = FakeWP()
    before = original_hash(wp)

    result = rehearsal.run(wp, URL, "x.com", tmp_path, fetch=rendered(wp))

    assert result, result.detail
    assert result.data["rehearsal"].passed is True
    assert original_hash(wp) == before                     # byte-identical
    assert rehearsal.MARKER_PREFIX not in wp.post["content"]["raw"]


def test_the_drill_picks_a_heading_itself_when_none_is_named(tmp_path):
    wp = FakeWP()
    result = rehearsal.run(wp, URL, "x.com", tmp_path)
    heading = next(s for s in result.data["rehearsal"].steps if s.name == "heading")
    assert "Spring replacement cost" in heading.detail


def test_a_page_with_no_headings_is_refused_rather_than_appended_to(tmp_path):
    wp = FakeWP(post={"id": 8, "content": {"raw": "<p>Just a paragraph.</p>"}, "meta": {}})
    result = rehearsal.run(wp, URL, "x.com", tmp_path)
    assert not result
    assert "לא נמצאה אף כותרת" in result.detail


def test_the_marker_is_unique_and_findable(tmp_path):
    wp = FakeWP()
    result = rehearsal.run(wp, URL, "x.com", tmp_path)
    marker = result.data["rehearsal"].marker
    assert len(marker) == 8
    assert marker in rehearsal.marker_text(marker)


# ═══════════════════════════════════════════════════════
#  What the drill exists to catch
# ═══════════════════════════════════════════════════════

def test_a_write_that_returns_200_and_changes_nothing_is_caught(tmp_path):
    """The failure that looks like success: meta not registered in REST."""
    wp = FakeWP(silent_discard=True)
    result = rehearsal.run(wp, URL, "x.com", tmp_path)

    assert not result
    failed = result.data["rehearsal"].failed_step
    assert failed.name == "confirm_write"
    assert "נזרק בשקט" in failed.detail


def test_no_write_permission_stops_before_anything_is_touched(tmp_path):
    wp = FakeWP(writable=False)
    result = rehearsal.run(wp, URL, "x.com", tmp_path)
    assert not result
    assert wp.writes == 0


def test_a_failed_restore_is_unrecoverable_and_names_the_leftover(tmp_path):
    """The one outcome that must never be retried automatically."""
    wp = FakeWP(fail_on_call=2)
    result = rehearsal.run(wp, URL, "x.com", tmp_path)

    assert not result
    assert result.recoverable is False
    assert "אל תריץ שוב" in result.detail
    assert rehearsal.MARKER_PREFIX in wp.post["content"]["raw"]   # still there


def test_a_restore_that_lands_on_different_content_is_caught(tmp_path):
    """Restoring 'successfully' to the wrong bytes is worse than failing."""
    wp = FakeWP()

    original_update = wp.update_post

    def sabotage(post_id, **kwargs):
        outcome = original_update(post_id, **kwargs)
        if wp.writes == 2:                       # the restore
            wp.post["content"]["raw"] += "<p>drift</p>"
        return outcome

    wp.update_post = sabotage
    result = rehearsal.run(wp, URL, "x.com", tmp_path)

    assert not result
    assert result.recoverable is False
    assert "לא חזר למצבו המקורי" in result.detail


def test_copy_that_never_reached_the_html_fails_the_drill_but_still_restores(tmp_path):
    """A cached page means our edits would not be crawlable. Still clean up."""
    wp = FakeWP()
    before = original_hash(wp)

    def cached(url):
        return 200, "<html><body>a stale cached copy</body></html>"

    result = rehearsal.run(wp, URL, "x.com", tmp_path, fetch=cached)

    assert not result
    assert result.code == "rehearsal_incomplete"
    assert original_hash(wp) == before                     # restore still ran


def test_a_page_that_errors_while_rendering_does_not_abort_the_restore(tmp_path):
    wp = FakeWP()
    before = original_hash(wp)

    def broken(url):
        raise RuntimeError("connection reset")

    rehearsal.run(wp, URL, "x.com", tmp_path, fetch=broken)
    assert original_hash(wp) == before


# ═══════════════════════════════════════════════════════
#  The record, and the gate it feeds
# ═══════════════════════════════════════════════════════

def test_a_failed_drill_is_recorded_as_failed_not_left_blank(tmp_path):
    rehearsal.run(FakeWP(writable=False), URL, "x.com", tmp_path)
    stored = json.loads((tmp_path / rehearsal.REHEARSAL_FILE).read_text(encoding="utf-8"))
    assert stored["passed"] is False
    assert stored["steps"][0]["name"] == "probe"


def test_a_client_that_never_rehearsed_is_told_how_to(tmp_path):
    result = rehearsal.require(tmp_path)
    assert not result
    assert result.code == "never_rehearsed"
    assert "seo_core.wp.rehearsal" in result.detail


def test_a_failed_drill_does_not_satisfy_the_gate(tmp_path):
    rehearsal.run(FakeWP(writable=False), URL, "x.com", tmp_path)
    assert not rehearsal.require(tmp_path)


def test_a_passing_drill_satisfies_the_gate(tmp_path):
    rehearsal.run(FakeWP(), URL, "x.com", tmp_path)
    assert rehearsal.require(tmp_path)


def test_a_rehearsal_older_than_the_ttl_has_to_be_redone(tmp_path):
    """Plugins and permissions drift. A two-year-old drill proves nothing."""
    rehearsal.run(FakeWP(), URL, "x.com", tmp_path)
    later = datetime.now(timezone.utc) + timedelta(days=rehearsal.REHEARSAL_TTL_DAYS + 1)

    result = rehearsal.require(tmp_path, now=later)
    assert not result
    assert result.code == "rehearsal_stale"


def test_a_corrupt_rehearsal_file_does_not_pass_the_gate(tmp_path):
    (tmp_path / rehearsal.REHEARSAL_FILE).write_text("{ broken", encoding="utf-8")
    assert rehearsal.status(tmp_path).code == "rehearsal_corrupt"


def test_the_backup_taken_during_the_drill_is_kept(tmp_path):
    rehearsal.run(FakeWP(), URL, "x.com", tmp_path)
    assert list((tmp_path / "backups").glob("rehearsal_*.json"))


# ═══════════════════════════════════════════════════════
#  Elementor — the case the drill exists for
# ═══════════════════════════════════════════════════════

ELEMENTOR_TREE = [{
    "id": "s1", "elType": "section", "elements": [{
        "id": "c1", "elType": "column", "elements": [
            {"id": "w1", "elType": "widget", "widgetType": "heading",
             "settings": {"title": "Spring replacement cost"}, "elements": []},
            {"id": "w2", "elType": "widget", "widgetType": "text-editor",
             "settings": {"editor": "<p>Answer.</p>"}, "elements": []},
        ],
    }],
}]


def elementor_wp(**kwargs) -> FakeWP:
    return FakeWP(post={
        "id": 9,
        "content": {"raw": "<p>Stale copy nobody sees.</p>"},
        "meta": {
            wp_content.ELEMENTOR_MODE_KEY: "builder",
            wp_content.ELEMENTOR_DATA_KEY: json.dumps(ELEMENTOR_TREE),
        },
    }, **kwargs)


def test_the_drill_on_an_elementor_page_edits_the_tree_and_restores_it(tmp_path):
    wp = elementor_wp()
    before = original_hash(wp)

    result = rehearsal.run(wp, URL, "x.com", tmp_path)

    assert result, result.detail
    assert result.data["rehearsal"].builder == "elementor"
    assert original_hash(wp) == before
    assert json.loads(wp.post["meta"][wp_content.ELEMENTOR_DATA_KEY]) == ELEMENTOR_TREE


def test_a_failed_elementor_restore_leaves_the_tree_modified_and_says_so(tmp_path):
    wp = elementor_wp(fail_on_call=2)
    result = rehearsal.run(wp, URL, "x.com", tmp_path)

    assert not result
    assert result.recoverable is False
    assert rehearsal.MARKER_PREFIX in wp.post["meta"][wp_content.ELEMENTOR_DATA_KEY]
