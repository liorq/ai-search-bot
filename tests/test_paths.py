"""Tests for where secrets live, and for noticing when that place is unsafe."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from seo_core import clients, paths, secrets  # noqa: E402


# ═══════════════════════════════════════════════════════
#  SEO_HOME moves everything at once
# ═══════════════════════════════════════════════════════

def test_the_default_location_is_outside_any_project(monkeypatch):
    monkeypatch.delenv("SEO_HOME", raising=False)
    assert paths.home() == Path.home() / ".claude" / "seo"


def test_seo_home_relocates_the_env_the_registry_and_the_data(monkeypatch, tmp_path):
    """Moving one of the three and not the others is how a run half-works."""
    monkeypatch.setenv("SEO_HOME", str(tmp_path))

    assert paths.env_path() == tmp_path / ".env"
    assert paths.registry_path() == tmp_path / "clients.json"
    assert paths.data_dir("x.com") == tmp_path / "data" / "x.com"


def test_a_relocated_env_is_actually_read(monkeypatch, tmp_path):
    monkeypatch.setenv("SEO_HOME", str(tmp_path))
    monkeypatch.delenv("GSC_WIZARD_API_KEY", raising=False)
    (tmp_path / ".env").write_text("GSC_WIZARD_API_KEY=from-the-new-place\n", encoding="utf-8")

    loaded = secrets.load_env()
    assert loaded["GSC_WIZARD_API_KEY"] == "from-the-new-place"


def test_a_relocated_registry_is_actually_read(monkeypatch, tmp_path):
    """The constant is a snapshot; the loader must resolve the path freshly."""
    monkeypatch.setenv("SEO_HOME", str(tmp_path))
    (tmp_path / "clients.json").write_text(
        '{"my-site.local": {"cms": {"type": "wordpress", '
        '"base_url": "http://my-site.local", "username": "admin", '
        '"auth_env": "LOCAL_WP_APP_PASSWORD"}}}',
        encoding="utf-8",
    )
    assert "my-site.local" in clients.load_all()


def test_a_tilde_in_seo_home_is_expanded(monkeypatch):
    monkeypatch.setenv("SEO_HOME", "~/seo-keys")
    assert paths.home() == Path.home() / "seo-keys"


# ═══════════════════════════════════════════════════════
#  Noticing an unsafe location
# ═══════════════════════════════════════════════════════

@pytest.mark.parametrize("folder", [
    "/Users/lior/Library/Mobile Documents/com~apple~CloudDocs/keys",
    "/Users/lior/Dropbox/seo",
    "/Users/lior/OneDrive/keys",
    "/Users/lior/Google Drive/seo",
])
def test_a_cloud_synced_folder_is_recognised(folder):
    assert paths.is_synced(Path(folder)) is True


def test_a_plain_folder_is_not_flagged_as_synced():
    assert paths.is_synced(Path("/Users/lior/.claude/seo")) is False


def test_the_default_location_raises_no_warnings(monkeypatch, tmp_path):
    monkeypatch.setenv("SEO_HOME", str(tmp_path))
    assert paths.warnings_for() == []


def test_a_synced_location_says_the_keys_will_leave_the_machine(monkeypatch, tmp_path):
    synced = tmp_path / "Dropbox" / "seo"
    synced.mkdir(parents=True)
    monkeypatch.setenv("SEO_HOME", str(synced))

    problems = paths.warnings_for()
    assert problems
    assert "ייצאו מהמחשב" in problems[0]


def test_a_world_readable_env_is_flagged(monkeypatch, tmp_path):
    """A WordPress password readable by every account on the machine."""
    monkeypatch.setenv("SEO_HOME", str(tmp_path))
    env = tmp_path / ".env"
    env.write_text("LOCAL_WP_APP_PASSWORD=secret\n", encoding="utf-8")
    env.chmod(0o644)

    assert any("chmod 600" in p for p in paths.warnings_for())


def test_a_correctly_locked_env_is_not_flagged(monkeypatch, tmp_path):
    monkeypatch.setenv("SEO_HOME", str(tmp_path))
    env = tmp_path / ".env"
    env.write_text("LOCAL_WP_APP_PASSWORD=secret\n", encoding="utf-8")
    env.chmod(0o600)

    assert paths.warnings_for() == []


def test_a_git_repo_as_the_secrets_home_is_flagged(monkeypatch, tmp_path):
    """An .env inside a repo is one `git add -A` away from being published."""
    monkeypatch.setenv("SEO_HOME", str(tmp_path))
    (tmp_path / ".git").mkdir()

    assert any("git" in p for p in paths.warnings_for())


def test_warnings_can_be_asked_about_a_path_that_is_not_the_current_home(tmp_path):
    synced = tmp_path / "OneDrive" / "keys"
    synced.mkdir(parents=True)
    assert paths.warnings_for(synced)
