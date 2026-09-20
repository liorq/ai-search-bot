"""Every skill runs on `--client` alone — and a missing source is never a pass.

Nine skills used to demand an export file that nothing in the toolkit produced.
They now resolve their rows through `gsc_wizard.rows_for` (the analysis cannot
proceed without them) or `optional_rows_for` (it can, in a reduced form). These
tests fix the two rules that matter more than the plumbing:

    * a fetch that fails stops a skill that needs the data — it does not fall
      through to "no findings", which reads as a clean bill of health;
    * a skill that degrades says out loud which part was NOT checked.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from seo_core import clients                       # noqa: E402
from seo_core.schema import Result                 # noqa: E402
from seo_core.sources import gsc_wizard            # noqa: E402

ROWS = [{"query": "facial sarasota", "url": "https://example.com/facial",
         "clicks": 2, "impressions": 400, "position": 8.0},
        {"query": "facial sarasota", "url": "https://example.com/spa",
         "clicks": 0, "impressions": 300, "position": 11.0}]
COVER = {"rows_total": 2, "rows_dropped": 0, "dropped_reasons": {},
         "impressions_total": 1000, "impressions_in_queries": 700,
         "clicks_total": 10, "clicks_in_queries": 2,
         "anonymised_share": 0.3, "anonymised_click_share": 0.8, "truncated": False}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SEO_HOME", str(tmp_path))
    (tmp_path / "clients.json").write_text(json.dumps({"example.com": {
        "market": "us", "gsc_property": "sc-domain:example.com",
        "cms": {"type": "manual", "base_url": "https://example.com"}}}),
        encoding="utf-8")
    return clients.load("example.com")


@pytest.fixture
def local_client(tmp_path, monkeypatch):
    """A development site: no Search Console property can exist for it."""
    monkeypatch.setenv("SEO_HOME", str(tmp_path))
    (tmp_path / "clients.json").write_text(json.dumps({"my-site.local": {
        "market": "us",
        "cms": {"type": "wordpress", "base_url": "http://my-site.local",
                "username": "admin", "auth_env": "LOCAL_WP_APP_PASSWORD"}}}),
        encoding="utf-8")
    return clients.load("my-site.local")


def export_file(tmp_path) -> Path:
    path = tmp_path / "queries.json"
    path.write_text(json.dumps({"property": "sc-domain:example.com",
                                "range": "2026-08-20..2026-09-16", "rows": ROWS,
                                "completeness": COVER}), encoding="utf-8")
    return path


def fetched_ok(path) -> Result:
    return Result.success("exported", "2 שורות נמשכו מ-GSC Wizard", path=path,
                          completeness=COVER, window={"start": "2026-08-20", "end": "2026-09-16"},
                          freshness={"settled_through": "2026-09-16", "maturity": None})


def fetch_failed() -> Result:
    return Result.failure("gsc_wizard_no_key", "חסר GSC_WIZARD_API_KEY ב-.env")


# ═══════════════════════════════════════════════════════
#  Data the analysis cannot do without
# ═══════════════════════════════════════════════════════

def test_rows_are_fetched_when_no_file_is_named(client, tmp_path, monkeypatch):
    path = export_file(tmp_path)
    monkeypatch.setattr(gsc_wizard, "ensure_queries", lambda c, days=28: fetched_ok(path))
    rows = gsc_wizard.rows_for(client)
    assert rows and rows.data["path"] == path and rows.data["fetched"]
    # The pull is described before a single finding is shown, and the hidden
    # clicks are named as Search Console's doing, not a gap in our fetch.
    labels = dict(rows.data["lines"])
    assert any("מסתיר" in value for value in labels.values())


def test_a_named_file_is_used_as_it_is(client, tmp_path, monkeypatch):
    # Replaying a saved export is how a past run is reproduced; re-fetching
    # would change the data under the comparison it is being compared to.
    monkeypatch.setattr(gsc_wizard, "ensure_queries",
                        lambda *a, **k: pytest.fail("a named file must not trigger a fetch"))
    rows = gsc_wizard.rows_for(client, str(export_file(tmp_path)))
    assert rows and not rows.data["fetched"] and rows.data["lines"] == []


def test_a_failed_fetch_is_a_failure_not_an_empty_analysis(client, monkeypatch):
    monkeypatch.setattr(gsc_wizard, "ensure_queries", lambda c, days=28: fetch_failed())
    rows = gsc_wizard.rows_for(client)
    assert not rows and rows.code == "gsc_wizard_no_key"


# ═══════════════════════════════════════════════════════
#  Data the analysis can survive without
# ═══════════════════════════════════════════════════════

def test_a_client_without_a_property_degrades_without_trying_to_fetch(local_client, monkeypatch):
    monkeypatch.setattr(gsc_wizard, "ensure_queries",
                        lambda *a, **k: pytest.fail("there is nothing to fetch for a local site"))
    rows = gsc_wizard.optional_rows_for(local_client)
    assert rows and rows.data["path"] is None
    assert "gsc_property" in rows.data["blocked"]


def test_a_failed_fetch_leaves_a_blocked_note_rather_than_silence(client, monkeypatch):
    monkeypatch.setattr(gsc_wizard, "ensure_queries", lambda c, days=28: fetch_failed())
    rows = gsc_wizard.optional_rows_for(client)
    assert rows                                   # the crawl still has something to say
    assert rows.data["path"] is None
    assert "חסומים, לא נקיים" in rows.data["blocked"]


def test_nothing_is_blocked_when_the_rows_arrive(client, tmp_path, monkeypatch):
    path = export_file(tmp_path)
    monkeypatch.setattr(gsc_wizard, "ensure_queries", lambda c, days=28: fetched_ok(path))
    rows = gsc_wizard.optional_rows_for(client)
    assert rows.data["path"] == path and rows.data["blocked"] is None


# ═══════════════════════════════════════════════════════
#  The skills themselves
# ═══════════════════════════════════════════════════════

def load_skill(name: str, module: str):
    import importlib.util

    path = Path(__file__).resolve().parents[1] / "skills" / name / "scripts" / f"{module}.py"
    spec = importlib.util.spec_from_file_location(f"_skill_{module}", path)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


REQUIRED = [("onpage-optimizer", "onpage_optimizer"),
            ("ctr-titles", "ctr_titles"),
            ("internal-anchors", "internal_anchors"),
            ("topic-cluster", "topic_cluster")]


@pytest.mark.parametrize("skill,module", REQUIRED)
def test_a_skill_that_needs_the_rows_stops_when_they_cannot_be_fetched(
        skill, module, client, monkeypatch, capsys):
    script = load_skill(skill, module)
    monkeypatch.setattr(script.gsc_wizard, "ensure_queries", lambda c, days=28: fetch_failed())
    assert script.main(["--client", "example.com"]) == 1
    printed = capsys.readouterr().out
    assert "GSC_WIZARD_API_KEY" in printed
    # The one thing it must never print instead: a clean result.
    assert "לא נמצאו" not in printed


@pytest.mark.parametrize("skill,module", REQUIRED)
def test_a_skill_that_needs_the_rows_accepts_a_named_file_without_fetching(
        skill, module, client, tmp_path, monkeypatch):
    script = load_skill(skill, module)
    monkeypatch.setattr(script.gsc_wizard, "ensure_queries",
                        lambda *a, **k: pytest.fail("--queries must not trigger a fetch"))
    # Anything past the load is the skill's own business (crawling, writing);
    # what is asserted here is only that the named file is honoured.
    try:
        script.main(["--client", "example.com", "--queries", str(export_file(tmp_path))])
    except Exception:
        pass
