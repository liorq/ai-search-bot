"""Tests for the refresh list — what an API edit leaves stale in Yoast, and
how the pages that need a human get collected instead of forgotten."""

from __future__ import annotations

import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from seo_core.change_guard import ledger  # noqa: E402
from seo_core.wp import seo_refresh  # noqa: E402

BASE = "https://example.com"


def change(change_id="chg_1", url=f"{BASE}/services", post_id=12,
           status="applied", notes=None, applied_at="2026-09-01T10:00:00+00:00",
           skill="content-decay"):
    return {
        "change_id": change_id, "url": url, "post_id": post_id,
        "post_type": "pages", "skill": skill, "status": status,
        "applied_at": applied_at, "notes": notes or [],
    }


def seeded(tmp_path, changes) -> Path:
    ledger._write(tmp_path, changes)
    return tmp_path


# ═══════════════════════════════════════════════════════
#  Why this cannot be automated
# ═══════════════════════════════════════════════════════

def test_the_reason_names_the_meta_the_browser_writes():
    """Stated plainly, because "just re-save it" is not an explanation."""
    reason = seo_refresh.why_manual("yoast")
    assert "_yoast_wpseo_linkdex" in reason
    assert "JavaScript" in reason


def test_a_site_with_no_seo_plugin_has_nothing_to_refresh():
    assert "אין מה לרענן" in seo_refresh.why_manual("unknown")


def test_an_elementor_page_gets_the_caveat_that_the_score_was_never_right():
    """Yoast reads post_content, which on Elementor nobody renders."""
    caveat = seo_refresh.elementor_caveat("elementor", "yoast")
    assert caveat and "post_content" in caveat


def test_a_gutenberg_page_gets_no_such_caveat():
    assert seo_refresh.elementor_caveat("gutenberg", "yoast") is None


# ═══════════════════════════════════════════════════════
#  Collecting what is waiting
# ═══════════════════════════════════════════════════════

def test_an_applied_change_is_waiting_for_a_re_save():
    pages = seo_refresh.pending([change()])
    assert len(pages) == 1
    assert pages[0].change_id == "chg_1"


def test_a_rolled_back_change_is_not_waiting():
    """The page is back to the content its score already described."""
    assert seo_refresh.pending([change(status="rolled_back")]) == []


def test_a_plan_that_was_never_published_is_not_waiting():
    assert seo_refresh.pending([change(status="planned")]) == []


def test_two_edits_to_one_page_are_one_trip_to_the_editor():
    pages = seo_refresh.pending([
        change(change_id="chg_1"),
        change(change_id="chg_2", applied_at="2026-09-05T10:00:00+00:00"),
    ])
    assert len(pages) == 1


def test_pages_come_back_oldest_first():
    pages = seo_refresh.pending([
        change(change_id="chg_2", url=f"{BASE}/b", applied_at="2026-09-05T10:00:00+00:00"),
        change(change_id="chg_1", url=f"{BASE}/a", applied_at="2026-09-01T10:00:00+00:00"),
    ])
    assert [p.change_id for p in pages] == ["chg_1", "chg_2"]


def test_a_page_already_refreshed_drops_off_the_list():
    done = change(notes=[f"{seo_refresh.REFRESH_NOTE} 2026-09-02T09:00:00+00:00"])
    assert seo_refresh.pending([done]) == []


# ═══════════════════════════════════════════════════════
#  The list itself
# ═══════════════════════════════════════════════════════

def test_each_page_carries_a_link_that_opens_the_editor():
    page = seo_refresh.pending([change()])[0]
    assert page.edit_url(BASE) == f"{BASE}/wp-admin/post.php?post=12&action=edit"


def test_a_page_with_no_post_id_falls_back_to_its_own_url():
    page = seo_refresh.pending([change(post_id=None)])[0]
    assert page.edit_url(BASE) == f"{BASE}/services"


def test_the_list_carries_the_command_that_closes_it_out():
    text = seo_refresh.render(seo_refresh.pending([change()]), BASE, "yoast")
    assert "--done chg_1" in text
    assert "_yoast_wpseo_linkdex" in text


def test_an_empty_list_says_so_rather_than_printing_a_heading():
    assert seo_refresh.render([], BASE, "yoast") == "אין דפים שממתינים לרענון."


# ═══════════════════════════════════════════════════════
#  Marking them done
# ═══════════════════════════════════════════════════════

def test_marking_a_page_done_takes_it_off_the_next_list(tmp_path):
    seeded(tmp_path, [change()])
    assert seo_refresh.mark_done(["chg_1"], tmp_path)
    assert seo_refresh.pending(ledger.load(tmp_path)) == []


def test_marking_leaves_the_rest_of_the_record_intact(tmp_path):
    seeded(tmp_path, [change()])
    seo_refresh.mark_done(["chg_1"], tmp_path)

    stored = ledger.load(tmp_path)[0]
    assert stored["status"] == "applied"
    assert stored["url"] == f"{BASE}/services"


def test_an_unknown_change_id_is_reported_not_silently_ignored(tmp_path):
    seeded(tmp_path, [change()])
    result = seo_refresh.mark_done(["chg_nope"], tmp_path)
    assert not result
    assert "chg_nope" in result.detail


def test_marking_against_no_ledger_at_all_says_so(tmp_path):
    result = seo_refresh.mark_done(["chg_1"], tmp_path)
    assert not result
    assert result.code == "ledger_empty"


def test_only_the_named_changes_are_marked(tmp_path):
    seeded(tmp_path, [change(change_id="chg_1", url=f"{BASE}/a"),
                      change(change_id="chg_2", url=f"{BASE}/b")])
    seo_refresh.mark_done(["chg_1"], tmp_path)
    assert [p.change_id for p in seo_refresh.pending(ledger.load(tmp_path))] == ["chg_2"]


# ═══════════════════════════════════════════════════════
#  WP-CLI, where it exists
# ═══════════════════════════════════════════════════════

def runner(responses):
    def run(argv):
        return responses.get(" ".join(argv), (1, "", "command not found"))
    return run


def test_an_available_index_command_is_reported_with_its_limit():
    """It rebuilds indexables. It still does not compute the editor's score."""
    result = seo_refresh.reindex_command("yoast", runner({
        "wp help yoast": (0, "SUBCOMMANDS\n\n  index    Index all content\n", ""),
    }))
    assert result
    assert result.data["command"] == "wp yoast index"
    assert "לא מחשב" in result.detail


def test_a_plugin_without_cli_commands_is_not_pretended_to_have_them():
    result = seo_refresh.reindex_command("yoast", runner({}))
    assert not result
    assert result.code == "no_wpcli"


def test_cli_commands_without_an_index_command_are_reported_as_such():
    result = seo_refresh.reindex_command("yoast", runner({
        "wp help yoast": (0, "SUBCOMMANDS\n\n  cleanup   Clean up\n", ""),
    }))
    assert result.code == "no_index_command"


def test_an_unrecognised_plugin_has_no_command_to_offer():
    assert seo_refresh.reindex_command("unknown", runner({})).code == "no_plugin"


# ═══════════════════════════════════════════════════════
#  Saying the right thing when the plugin is unknown
# ═══════════════════════════════════════════════════════

def test_an_unidentified_plugin_does_not_claim_there_is_nothing_to_do():
    """The pages were still edited. Only the reason changes, not the list."""
    text = seo_refresh.render(seo_refresh.pending([change()]), BASE, "unknown")
    assert "אין כאן מה לעשות" in text
    assert f"{BASE}/wp-admin/post.php?post=12&action=edit" in text
    assert "דף אחד" in text


def test_one_page_is_not_called_pages():
    text = seo_refresh.render(seo_refresh.pending([change()]), BASE, "yoast")
    assert "דף אחד" in text
    assert "1 דפים" not in text


def test_one_change_marked_is_not_called_changes(tmp_path):
    seeded(tmp_path, [change()])
    assert "שינוי אחד" in seo_refresh.mark_done(["chg_1"], tmp_path).detail
