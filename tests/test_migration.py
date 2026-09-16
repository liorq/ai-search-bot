"""Tests for the migration layer: limits, the state machine, and what must be
true afterwards. The failure scenarios are the point — a migration that works
is not the one that costs anyone anything."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from seo_core.schema import Result  # noqa: E402
from seo_core.wp import limits, migration  # noqa: E402

MB = 1024 ** 2


# ═══════════════════════════════════════════════════════
#  Sizes
# ═══════════════════════════════════════════════════════

@pytest.mark.parametrize("raw,expected", [
    ("512M", 512 * MB), ("1G", 1024 * MB), ("64m", 64 * MB),
    ("2048K", 2 * MB), ("1048576", 1048576), (33554432, 33554432),
])
def test_php_shorthand_is_read_correctly(raw, expected):
    assert limits.parse_size(raw) == expected


@pytest.mark.parametrize("raw", ["-1", -1, "0", None, "nonsense"])
def test_unlimited_and_unreadable_both_read_as_no_limit(raw):
    """`-1` means unlimited in PHP. `int("512M")` would have raised."""
    assert limits.parse_size(raw) in (None, 0)


def test_the_binding_constraint_is_named_not_just_the_number():
    server = limits.ServerLimits(
        upload_max_filesize=512 * MB, post_max_size=64 * MB, plugin_max_upload=256 * MB
    )
    assert server.import_ceiling == 64 * MB
    assert server.binding_constraint == "post_max_size"


def test_an_unlimited_setting_does_not_become_the_ceiling():
    server = limits.ServerLimits(upload_max_filesize=None, post_max_size=128 * MB)
    assert server.import_ceiling == 128 * MB


# ═══════════════════════════════════════════════════════
#  The archive gate
# ═══════════════════════════════════════════════════════

def generous() -> limits.ServerLimits:
    return limits.ServerLimits(
        collected=True,
        upload_max_filesize=2048 * MB, post_max_size=2048 * MB,
        memory_limit=512 * MB, free_disk_bytes=20 * 1024 * MB,
    )


def test_an_archive_over_the_hard_limit_is_refused_however_generous_the_server():
    result = limits.check_archive(600 * MB, generous())
    assert not result
    assert "500.0MB" in result.detail


def test_an_archive_over_the_plugin_ceiling_names_the_plugin():
    server = generous()
    server.plugin_max_upload = 128 * MB
    result = limits.check_archive(300 * MB, server)
    assert not result
    assert "מגבלת התוסף" in result.detail


def test_free_disk_is_measured_against_what_unpacking_needs():
    server = generous()
    server.free_disk_bytes = 300 * MB
    result = limits.check_archive(200 * MB, server)      # needs 500MB to unpack
    assert not result
    assert "דיסק פנוי" in result.detail


def test_a_low_memory_limit_is_raised_before_the_import_not_after():
    server = generous()
    server.memory_limit = 128 * MB
    assert not limits.check_archive(100 * MB, server)


def test_an_archive_that_fits_passes_and_reports_the_headroom():
    result = limits.check_archive(200 * MB, generous())
    assert result
    assert result.data["ceiling"] == 2048 * MB


def test_the_size_gate_shows_what_is_large_instead_of_deleting_it():
    result = migration.size_gate(
        700 * MB, generous(),
        largest=[("wp-content/uploads/2019", 400 * MB), ("videos", 180 * MB)],
    )
    assert not result
    assert "uploads/2019" in result.detail
    assert "לא ממשיכים אוטומטית" in result.detail
    assert "מדיה ותוכן לא נמחקים" in result.detail


# ═══════════════════════════════════════════════════════
#  Pruning
# ═══════════════════════════════════════════════════════

def test_only_regenerable_things_are_pruned():
    plan = migration.plan_pruning({
        "cache": 120 * MB, "logs": 30 * MB, "uploads": 900 * MB, "orders": 40 * MB,
    })
    chosen = [name for name, _ in plan.items]
    assert chosen == ["cache", "logs"]
    assert "uploads" in plan.refused
    assert "orders" in plan.refused


def test_media_is_never_prunable_no_matter_how_large():
    plan = migration.plan_pruning({"uploads": 5000 * MB})
    assert plan.items == []
    assert plan.savings == 0


def test_the_prune_plan_states_the_saving_in_readable_units():
    plan = migration.plan_pruning({"cache": 120 * MB})
    assert "120.0MB" in plan.render()


# ═══════════════════════════════════════════════════════
#  Tooling discovery — WP-CLI before Playwright
# ═══════════════════════════════════════════════════════

def runner(responses):
    def run(argv):
        key = " ".join(argv)
        return responses.get(key, (1, "", "command not found"))
    return run


HELP_OUTPUT = """usage: wp ai1wm <command>

SUBCOMMANDS

  backup       Create a backup of the site
  restore      Restore a backup
  list         List available backups
"""


def test_the_installed_subcommands_are_discovered_not_assumed():
    """Free and paid builds ship different CLI surfaces across releases."""
    report = migration.detect_tooling(runner({
        "wp --info": (0, "WP-CLI 2.9.0", ""),
        "wp help ai1wm": (0, HELP_OUTPUT, ""),
    }))
    assert report.subcommands == ["backup", "list", "restore"]
    assert report.can_export and report.can_import
    assert report.method == "wp-cli"


def test_no_wpcli_falls_back_to_the_browser():
    report = migration.detect_tooling(runner({}))
    assert report.has_wpcli is False
    assert report.method == "browser"


def test_wpcli_without_the_plugin_falls_back_to_the_browser():
    report = migration.detect_tooling(runner({"wp --info": (0, "WP-CLI 2.9.0", "")}))
    assert report.has_wpcli is True
    assert report.has_plugin is False
    assert report.method == "browser"


def test_a_plugin_that_only_exports_does_not_claim_it_can_import():
    report = migration.detect_tooling(runner({
        "wp --info": (0, "WP-CLI 2.9.0", ""),
        "wp help ai1wm": (0, "SUBCOMMANDS\n\n  backup       Create a backup\n", ""),
    }))
    assert report.can_export is True
    assert report.can_import is False
    assert report.method == "browser"


# ═══════════════════════════════════════════════════════
#  The state machine
# ═══════════════════════════════════════════════════════

def fresh() -> migration.MigrationState:
    return migration.MigrationState.new("mig_001", "old.com", "new.com")


def test_a_stage_cannot_run_before_its_predecessors():
    result = migration.begin(fresh(), "import")
    assert not result
    assert result.code == "out_of_order"
    assert "preflight" in result.detail


def test_running_the_same_stage_twice_is_a_skip_not_a_repeat():
    """A resumed run starts by re-entering stages that already completed."""
    state = migration.complete(fresh(), "preflight", "limits collected")
    result = migration.begin(state, "preflight")
    assert not result
    assert result.code == "already_done"


def test_the_next_stage_is_the_first_incomplete_one():
    state = fresh()
    for stage in ("preflight", "backup_source"):
        migration.complete(state, stage)
    assert state.next_stage == "backup_target"


def test_state_survives_a_round_trip_to_disk(tmp_path):
    state = migration.complete(fresh(), "preflight", "ok")
    state.archive_bytes = 300 * MB
    migration.save(state, tmp_path)

    loaded = migration.load("mig_001", tmp_path)
    assert loaded
    assert loaded.data["state"].is_done("preflight")
    assert loaded.data["state"].archive_bytes == 300 * MB


def test_a_corrupt_state_file_stops_rather_than_guesses(tmp_path):
    (tmp_path / "mig_001.json").write_text("{ broken", encoding="utf-8")
    result = migration.load("mig_001", tmp_path)
    assert not result
    assert result.recoverable is False


# ═══════════════════════════════════════════════════════
#  Backups
# ═══════════════════════════════════════════════════════

def test_an_import_is_blocked_unless_both_backups_verify():
    result = migration.verify_backup_pair(
        Result.success("ok", "source fine"),
        Result.failure("backup_unusable", "גיבוי היעד לא נטען"),
    )
    assert not result
    assert result.recoverable is False
    assert "גיבוי היעד" in result.detail


def test_backups_are_not_released_without_an_explicit_confirmation():
    state = fresh()
    migration.complete(state, "postcheck")
    assert not migration.release_backups(state, confirmed=False)
    assert state.backups_released is False


def test_backups_are_not_released_before_the_checks_have_run():
    assert not migration.release_backups(fresh(), confirmed=True)


def test_a_confirmed_release_after_a_clean_postcheck_is_allowed():
    state = fresh()
    migration.complete(state, "postcheck")
    assert migration.release_backups(state, confirmed=True)
    assert state.backups_released is True


# ═══════════════════════════════════════════════════════
#  Failing halfway
# ═══════════════════════════════════════════════════════

def test_a_failure_before_the_import_says_nothing_was_touched():
    state = migration.complete(fresh(), "preflight")
    migration.mark_failed(state, "backup_source", "disk full")
    text = migration.recovery_instructions(state)
    assert "אף אתר לא נגע" in text
    assert "disk full" in text


def test_a_failure_during_the_import_warns_against_a_second_attempt():
    """The target is half-overwritten. Importing again on top of it compounds it."""
    state = fresh()
    for stage in ("preflight", "backup_source", "backup_target",
                  "verify_backups", "export", "size_gate"):
        migration.complete(state, stage)
    migration.mark_failed(state, "import", "timeout at 60%")

    text = migration.recovery_instructions(state)
    assert "שחזר מגיבוי היעד" in text
    assert "אל תריץ ייבוא נוסף" in text


def test_recovery_always_states_that_the_backups_are_still_there():
    assert "הגיבויים נשמרים" in migration.recovery_instructions(fresh())


# ═══════════════════════════════════════════════════════
#  After the move
# ═══════════════════════════════════════════════════════

GOOD_PAGE = """<html><head>
<link rel="canonical" href="https://new.com/page">
</head><body>
<h1>Title</h1><form action="/submit"></form>
<img src="https://new.com/wp-content/uploads/a.jpg">
</body></html>"""


def fetcher(pages):
    def fetch(url):
        if url in pages:
            return pages[url]
        return (404, "")
    return fetch


def test_a_clean_migration_passes_every_breaking_check():
    report = migration.postcheck(
        "https://new.com/page", "old.com", "new.com",
        fetcher({
            "https://new.com/page": (200, GOOD_PAGE),
            "https://new.com/wp-content/uploads/a.jpg": (200, ""),
        }),
        expected_forms=1,
    )
    assert report.breaking_failures == []


def test_noindex_carried_over_from_staging_is_caught():
    """The single most expensive migration bug there is."""
    html = GOOD_PAGE.replace("<h1>", '<meta name="robots" content="noindex,follow"><h1>')
    report = migration.postcheck(
        "https://new.com/page", "old.com", "new.com",
        fetcher({"https://new.com/page": (200, html),
                 "https://new.com/wp-content/uploads/a.jpg": (200, "")}),
    )
    failed = [c for c in report.breaking_failures if c.name == "האתר מאונדקס"]
    assert failed and "נגרר מסביבת הפיתוח" in failed[0].detail


def test_robots_txt_blocking_everything_is_caught_too():
    report = migration.postcheck(
        "https://new.com/page", "old.com", "new.com",
        fetcher({"https://new.com/page": (200, GOOD_PAGE),
                 "https://new.com/wp-content/uploads/a.jpg": (200, "")}),
        robots_txt="User-agent: *\nDisallow: /",
    )
    assert any(c.name == "האתר מאונדקס" for c in report.breaking_failures)


def test_a_missed_search_and_replace_is_counted_not_guessed():
    html = GOOD_PAGE.replace("https://new.com/wp-content", "https://old.com/wp-content")
    report = migration.postcheck(
        "https://new.com/page", "old.com", "new.com",
        fetcher({"https://new.com/page": (200, html)}),
    )
    failed = [c for c in report.breaking_failures if "Search & Replace" in c.name]
    assert failed and "1 אזכורים" in failed[0].detail


def test_images_that_did_not_make_the_trip_are_caught():
    report = migration.postcheck(
        "https://new.com/page", "old.com", "new.com",
        fetcher({"https://new.com/page": (200, GOOD_PAGE)}),   # image 404s
    )
    failed = [c for c in report.breaking_failures if c.name == "תמונות נטענות"]
    assert failed and "uploads" in failed[0].detail


def test_a_canonical_left_pointing_at_the_old_site_is_caught():
    html = GOOD_PAGE.replace("https://new.com/page", "https://old.com/page")
    report = migration.postcheck(
        "https://new.com/page", "", "new.com",
        fetcher({"https://new.com/page": (200, html),
                 "https://new.com/wp-content/uploads/a.jpg": (200, "")}),
    )
    assert any("קנוניקל" in c.name for c in report.breaking_failures)


def test_mixed_content_after_an_ssl_move_is_caught():
    html = GOOD_PAGE.replace("https://new.com/wp-content", "http://new.com/wp-content")
    report = migration.postcheck(
        "https://new.com/page", "old.com", "new.com",
        fetcher({"https://new.com/page": (200, html),
                 "http://new.com/wp-content/uploads/a.jpg": (200, "")}),
    )
    assert any(c.name == "אין תוכן מעורב" for c in report.breaking_failures)


def test_elementor_css_that_was_not_regenerated_is_caught():
    html = GOOD_PAGE.replace(
        "<h1>", '<link rel="stylesheet" href="/wp-content/uploads/elementor/css/post-12.css"><h1>'
    )
    report = migration.postcheck(
        "https://new.com/page", "old.com", "new.com",
        fetcher({"https://new.com/page": (200, html),
                 "https://new.com/wp-content/uploads/a.jpg": (200, "")}),
    )
    failed = [c for c in report.breaking_failures if "Elementor" in c.name]
    assert failed and "Regenerate" in failed[0].detail


def test_a_lost_form_is_a_breaking_failure_not_a_cosmetic_one():
    html = GOOD_PAGE.replace('<form action="/submit"></form>', "")
    report = migration.postcheck(
        "https://new.com/page", "old.com", "new.com",
        fetcher({"https://new.com/page": (200, html),
                 "https://new.com/wp-content/uploads/a.jpg": (200, "")}),
        expected_forms=1,
    )
    assert any(c.name == "הטפסים במקומם" for c in report.breaking_failures)


def test_licence_bound_plugins_are_listed_because_html_cannot_show_them():
    report = migration.postcheck(
        "https://new.com/page", "old.com", "new.com",
        fetcher({"https://new.com/page": (200, GOOD_PAGE),
                 "https://new.com/wp-content/uploads/a.jpg": (200, "")}),
    )
    licence = next(c for c in report.checks if c.name == "רישיונות תוספים")
    assert "Elementor Pro" in licence.detail
    assert licence.breaking is False


def test_a_page_that_does_not_load_stops_the_checklist_immediately():
    report = migration.postcheck(
        "https://new.com/page", "old.com", "new.com", fetcher({}),
    )
    assert report.should_roll_back
    assert len(report.checks) == 1


# ═══════════════════════════════════════════════════════
#  Is the archive even whole?
# ═══════════════════════════════════════════════════════

def wpress(tmp_path, *, name=b"database.sql", body_mb=2, terminated=True) -> Path:
    path = tmp_path / "site.wpress"
    header = name.ljust(255, b"\x00") + b"\x00" * (migration.WPRESS_BLOCK - 255)
    blob = header + b"x" * (body_mb * MB)
    if terminated:
        blob += b"\x00" * migration.WPRESS_BLOCK
    path.write_bytes(blob)
    return path


def test_a_complete_archive_verifies(tmp_path):
    assert migration.verify_archive(wpress(tmp_path))


def test_a_truncated_archive_is_caught_before_it_is_imported(tmp_path):
    """How most 'the backup was corrupt' stories actually start."""
    result = migration.verify_archive(wpress(tmp_path, terminated=False))
    assert not result
    assert result.code == "archive_truncated"
    assert result.recoverable is False


def test_an_error_page_saved_as_wpress_is_caught(tmp_path):
    path = tmp_path / "site.wpress"
    path.write_bytes(b"<html><body>504 Gateway Timeout</body></html>")
    result = migration.verify_archive(path)
    assert not result
    assert result.code == "archive_too_small"


def test_a_large_file_that_is_not_a_wpress_is_caught(tmp_path):
    path = tmp_path / "site.wpress"
    path.write_bytes(b"\xff" * (3 * MB) + b"\x00" * migration.WPRESS_BLOCK)
    assert migration.verify_archive(path).code == "archive_not_wpress"


def test_a_missing_archive_is_unrecoverable(tmp_path):
    result = migration.verify_archive(tmp_path / "nothing.wpress")
    assert result.code == "archive_missing"
    assert result.recoverable is False


def test_two_verified_archives_clear_the_import_gate(tmp_path):
    source_dir, target_dir = tmp_path / "source", tmp_path / "target"
    source_dir.mkdir()
    target_dir.mkdir()

    source = migration.verify_archive(wpress(source_dir))
    target = migration.verify_archive(wpress(target_dir))
    assert migration.verify_backup_pair(source, target)


def multi_file_wpress(tmp_path) -> Path:
    """An archive holding three files of known size, in wpress layout."""
    path = tmp_path / "multi.wpress"
    blob = b""
    for name, prefix, size in (
        (b"database.sql", b".", 3 * MB),
        (b"hero.jpg", b"wp-content/uploads/2024", 9 * MB),
        (b"style.css", b"wp-content/themes/x", 1024),
    ):
        header = (
            name.ljust(255, b"\x00")
            + str(size).encode().ljust(14, b"\x00")
            + b"0".ljust(12, b"\x00")
            + prefix.ljust(4096, b"\x00")
        )
        blob += header + b"x" * size
    path.write_bytes(blob + b"\x00" * migration.WPRESS_BLOCK)
    return path


def test_the_archive_inventory_is_read_without_extracting_anything(tmp_path):
    entries = migration.largest_entries(multi_file_wpress(tmp_path))
    assert [name for name, _ in entries] == [
        "wp-content/uploads/2024/hero.jpg", "./database.sql", "wp-content/themes/x/style.css",
    ]
    assert entries[0][1] == 9 * MB


def test_the_inventory_of_an_unreadable_file_is_empty_not_a_guess(tmp_path):
    assert migration.largest_entries(tmp_path / "absent.wpress") == []


def test_the_size_gate_names_the_largest_file_from_the_archive_itself(tmp_path):
    archive = multi_file_wpress(tmp_path)
    result = migration.size_gate(
        700 * MB, generous(), largest=migration.largest_entries(archive)
    )
    assert "hero.jpg" in result.detail


# ═══════════════════════════════════════════════════════
#  Unknown is not unlimited
# ═══════════════════════════════════════════════════════

def test_limits_that_could_not_be_read_do_not_read_as_no_limits():
    """Otherwise a 400MB archive sails past a server that caps uploads at 64MB."""
    unread = limits.ServerLimits(site="new.com")
    assert unread.collected is False
    assert "לא נאספו" in unread.summary()


def test_an_archive_checked_against_unknown_limits_says_so():
    result = limits.check_archive(200 * MB, limits.ServerLimits())
    assert result
    assert result.data["limits_known"] is False
    assert "מגבלות השרת לא נבדקו" in result.detail


def test_the_hard_limit_still_applies_when_nothing_could_be_read():
    assert not limits.check_archive(600 * MB, limits.ServerLimits())


def test_a_limit_read_off_the_plugin_screen_counts_as_collected():
    """Browser mode: a human reads the number the plugin displays and passes it."""
    server = limits.unknown("new.com", plugin_max_upload="256M")
    assert server.collected is True
    assert server.import_ceiling == 256 * MB
    assert not limits.check_archive(300 * MB, server)
