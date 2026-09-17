"""Tests for the client registry and credential handling."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from seo_core import clients, secrets  # noqa: E402
from seo_core.clients import (  # noqa: E402
    Client,
    ClientConfigError,
    load,
    load_all,
    validate,
)

GOOD = {
    "denvergaragedoor.com": {
        "market": "us",
        "content_language": "en",
        "gsc_property": "sc-domain:denvergaragedoor.com",
        "cms": {
            "type": "wordpress",
            "base_url": "https://denvergaragedoor.com",
            "username": "seo",
            "auth_env": "DGD_WP_APP_PASSWORD",
            "staging_url": "https://staging.denvergaragedoor.com",
        },
        "ga4_property_id": "123456789",
        "conversions": ["phone_click", "form_submit"],
        "competitors": ["a.com", "b.com"],
    }
}


@pytest.fixture
def registry(tmp_path) -> Path:
    path = tmp_path / "clients.json"
    path.write_text(json.dumps(GOOD, ensure_ascii=False), encoding="utf-8")
    return path


# ═══════════════════════════════════════════════════════
#  Loading
# ═══════════════════════════════════════════════════════

def test_loads_a_client(registry):
    client = load("denvergaragedoor.com", registry)
    assert client.market == "us"
    assert client.location_code == 2840
    assert client.cms.is_writable is True


def test_www_prefix_is_ignored(registry):
    assert load("www.denvergaragedoor.com", registry).domain == "denvergaragedoor.com"


def test_unknown_client_lists_the_known_ones(registry):
    with pytest.raises(ClientConfigError, match="denvergaragedoor.com"):
        load("nosuchsite.com", registry)


def test_missing_registry_says_what_to_do(tmp_path):
    with pytest.raises(ClientConfigError, match="clients.example.json"):
        load_all(tmp_path / "absent.json")


def test_malformed_json_is_reported_clearly(tmp_path):
    bad = tmp_path / "clients.json"
    bad.write_text("{ not json", encoding="utf-8")
    with pytest.raises(ClientConfigError, match="JSON"):
        load_all(bad)


# ═══════════════════════════════════════════════════════
#  Validation — a half-configured client must not load
# ═══════════════════════════════════════════════════════

def test_wordpress_without_credentials_is_invalid():
    client = Client(domain="x.com", gsc_property="sc-domain:x.com")
    client.cms.type = "wordpress"
    client.cms.base_url = "https://x.com"
    problems = validate(client)
    assert any("auth_env" in p for p in problems)


def test_missing_search_console_property_is_invalid():
    assert any("gsc_property" in p for p in validate(Client(domain="x.com")))


def test_ga4_without_conversion_events_is_invalid():
    client = Client(
        domain="x.com", gsc_property="sc-domain:x.com", ga4_property_id="999"
    )
    assert any("conversions" in p for p in validate(client))


def test_invalid_client_refuses_to_load(tmp_path):
    broken = {"x.com": {"market": "us", "cms": {"type": "wordpress"}}}
    path = tmp_path / "clients.json"
    path.write_text(json.dumps(broken), encoding="utf-8")
    with pytest.raises(ClientConfigError, match="gsc_property"):
        load("x.com", path)


def test_unknown_market_is_flagged():
    client = Client(domain="x.com", gsc_property="p", market="fr")
    assert any("market" in p for p in validate(client))


# ═══════════════════════════════════════════════════════
#  Conversion readiness drives how impact is expressed
# ═══════════════════════════════════════════════════════

def test_client_with_ga4_and_events_has_conversion_data(registry):
    assert load("denvergaragedoor.com", registry).has_conversion_data is True


def test_client_without_ga4_has_no_conversion_data():
    client = Client(domain="x.com", gsc_property="p", conversions=["phone_click"])
    assert client.has_conversion_data is False


# ═══════════════════════════════════════════════════════
#  Credentials are read at point of use, never stored
# ═══════════════════════════════════════════════════════

def test_secret_is_read_from_the_environment(registry, monkeypatch):
    monkeypatch.setenv("DGD_WP_APP_PASSWORD", "abcd efgh ijkl mnop qrst uvwx")
    assert load("denvergaragedoor.com", registry).secret().startswith("abcd")


def test_missing_secret_points_at_the_env_file(registry, monkeypatch):
    monkeypatch.delenv("DGD_WP_APP_PASSWORD", raising=False)
    with pytest.raises(ClientConfigError, match=".env"):
        load("denvergaragedoor.com", registry).secret()


def test_client_object_does_not_carry_the_password(registry, monkeypatch):
    """A config dumped into a log or report must not leak a credential."""
    monkeypatch.setenv("DGD_WP_APP_PASSWORD", "abcd efgh ijkl mnop qrst uvwx")
    client = load("denvergaragedoor.com", registry)
    assert "abcd" not in repr(client)


# ═══════════════════════════════════════════════════════
#  Leak scanner
# ═══════════════════════════════════════════════════════

def test_scanner_finds_a_hardcoded_password(tmp_path):
    (tmp_path / "bad.py").write_text(
        'DATAFORSEO_PASSWORD = "s3cr3tvalue12345"\n', encoding="utf-8"
    )
    assert len(secrets.scan_for_leaks(tmp_path)) == 1


def test_scanner_finds_a_wordpress_application_password(tmp_path):
    (tmp_path / "bad.py").write_text(
        'pw = "abcd efgh ijkl mnop qrst uvwx"\n', encoding="utf-8"
    )
    assert secrets.scan_for_leaks(tmp_path)


def test_scanner_allows_reading_from_the_environment(tmp_path):
    (tmp_path / "good.py").write_text(
        'password = os.environ["DATAFORSEO_PASSWORD"]\n', encoding="utf-8"
    )
    assert secrets.scan_for_leaks(tmp_path) == []


def test_scanner_allows_placeholders(tmp_path):
    (tmp_path / "sample.py").write_text(
        'api_key = "<YOUR-KEY-HERE-CHANGEME>"\n', encoding="utf-8"
    )
    assert secrets.scan_for_leaks(tmp_path) == []


def test_scanner_skips_test_directories(tmp_path):
    """Fake credentials belong in tests — including the ones in this file."""
    test_dir = tmp_path / "tests"
    test_dir.mkdir()
    (test_dir / "test_thing.py").write_text(
        'FAKE_PASSWORD = "notarealvalue123"\n', encoding="utf-8"
    )
    assert secrets.scan_for_leaks(tmp_path) == []


def test_assert_no_leaks_raises_with_the_file_and_line(tmp_path):
    (tmp_path / "bad.py").write_text('token = "ghp_' + "a" * 32 + '"\n', encoding="utf-8")
    with pytest.raises(secrets.SecretsError, match="bad.py:1"):
        secrets.assert_no_leaks(tmp_path)


def test_env_file_does_not_override_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("CLARITY_API_TOKEN", "from-shell")
    env = tmp_path / ".env"
    env.write_text("CLARITY_API_TOKEN=from-file\n", encoding="utf-8")
    secrets.load_env(env)
    import os
    assert os.environ["CLARITY_API_TOKEN"] == "from-shell"


def test_require_names_the_missing_key(monkeypatch, tmp_path):
    monkeypatch.delenv("PAGESPEED_API_KEY", raising=False)
    monkeypatch.setattr(secrets, "ENV_PATH", tmp_path / "absent.env")
    with pytest.raises(secrets.SecretsError, match="PAGESPEED_API_KEY"):
        secrets.require("PAGESPEED_API_KEY")


# ═══════════════════════════════════════════════════════
#  A development site has no Search Console property
# ═══════════════════════════════════════════════════════

def local_registry(tmp_path, gsc_property=""):
    path = tmp_path / "clients.json"
    path.write_text(json.dumps({
        "my-site.local": {
            "gsc_property": gsc_property,
            "cms": {
                "type": "wordpress",
                "base_url": "http://my-site.local",
                "username": "admin",
                "auth_env": "LOCAL_WP_APP_PASSWORD",
            },
        }
    }, ensure_ascii=False), encoding="utf-8")
    return path


def test_a_local_site_loads_without_a_search_console_property(tmp_path):
    """Requiring one would block the rehearsal — the reason a local site exists."""
    client = clients.load("my-site.local", local_registry(tmp_path))
    assert client.cms.is_writable


def test_a_public_site_still_needs_one(tmp_path):
    path = tmp_path / "clients.json"
    path.write_text(json.dumps({
        "example.com": {
            "gsc_property": "",
            "cms": {"type": "wordpress", "base_url": "https://example.com",
                    "username": "u", "auth_env": "X"},
        }
    }), encoding="utf-8")
    with pytest.raises(clients.ClientConfigError, match="gsc_property"):
        clients.load("example.com", path)
