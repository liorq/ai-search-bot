"""A write lands on a listed target, or it does not happen.

Recognising a development site by its name is what these tests exist to rule
out: `http://my-site.local.example.com` ends in neither `.local` nor anything
a suffix test catches, and `https://my-site.local` is a different server from
`http://my-site.local`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from seo_core.wp import targets                     # noqa: E402
from seo_core.wp.client import WordPressClient      # noqa: E402

LOCAL = "http://my-site.local"


@pytest.fixture
def allowed(tmp_path):
    path = tmp_path / "write_targets.json"
    path.write_text(json.dumps({"rehearsal": [LOCAL]}), encoding="utf-8")
    return targets.WriteGuard.for_purpose("rehearsal", path)


# ── the list, matched exactly ─────────────────────────────────────────────

def test_the_listed_origin_is_allowed(allowed):
    assert allowed.check("POST", f"{LOCAL}/wp-json/wp/v2/pages/7")


@pytest.mark.parametrize("url", [
    "http://my-site.local.evil.com/wp-json/wp/v2/pages/7",   # suffix trick
    "http://evil.com/my-site.local/wp-json/wp/v2/pages/7",   # path trick
    "http://my-site.local:8080/wp-json/wp/v2/pages/7",       # another server
    "https://my-site.local/wp-json/wp/v2/pages/7",           # another scheme
    "http://sub.my-site.local/wp-json/wp/v2/pages/7",        # another host
])
def test_a_near_miss_is_refused(allowed, url):
    refused = allowed.check("POST", url)
    assert not refused and refused.code == "write_target_refused"
    assert not refused.recoverable


def test_the_default_port_is_the_same_target(allowed):
    assert allowed.check("POST", "http://my-site.local:80/wp-json/wp/v2/pages/7")


def test_reads_are_not_gated(allowed):
    assert allowed.check("GET", "https://anywhere.example.com/wp-json/")


# ── fail closed ───────────────────────────────────────────────────────────

def test_a_missing_list_allows_nothing(tmp_path):
    guard = targets.WriteGuard.for_purpose("rehearsal", tmp_path / "nothing.json")
    refused = guard.check("POST", LOCAL)
    assert not refused and refused.code == "write_targets_empty"


def test_an_unreadable_list_allows_nothing(tmp_path):
    path = tmp_path / "write_targets.json"
    path.write_text("{ this is not json", encoding="utf-8")
    assert not targets.WriteGuard.for_purpose("rehearsal", path).check("POST", LOCAL)


def test_a_list_for_another_purpose_does_not_leak(tmp_path):
    path = tmp_path / "write_targets.json"
    path.write_text(json.dumps({"publish": [LOCAL]}), encoding="utf-8")
    assert not targets.WriteGuard.for_purpose("rehearsal", path).check("POST", LOCAL)


# ── the site has to say it is that site ───────────────────────────────────

class Reply:
    def __init__(self, payload, ok=True):
        self.payload, self.ok = payload, ok


def identity_call(payload, ok=True):
    from seo_core.schema import Result

    def call(url):
        if not ok:
            return Result.failure("not_found", f"לא נמצא: {url}")
        return Result.success("ok", "בוצע", payload=payload)
    return call


def test_a_site_that_confirms_its_address_passes():
    assert targets.verify_identity(LOCAL, identity_call({"home": LOCAL, "url": LOCAL}))


def test_a_site_answering_for_somebody_else_is_refused():
    # A proxy, a tunnel, or a hosts entry someone changed looks exactly like this.
    checked = targets.verify_identity(
        LOCAL, identity_call({"home": "https://live-site.com", "url": "https://live-site.com"}))
    assert not checked and checked.code == "identity_mismatch"
    assert "live-site.com" in checked.detail


def test_a_site_that_will_not_say_who_it_is_is_refused():
    checked = targets.verify_identity(LOCAL, identity_call({}))
    assert not checked and checked.code == "identity_missing"


def test_an_unreadable_root_is_refused():
    checked = targets.verify_identity(LOCAL, identity_call({}, ok=False))
    assert not checked and checked.code == "identity_unreadable"


# ── redirects ─────────────────────────────────────────────────────────────

class Response:
    def __init__(self, status=200, url=LOCAL, history=(), headers=None):
        self.status_code, self.url = status, url
        self.history, self.headers = list(history), headers or {}

    def json(self):
        return {"id": 7}


class Transport:
    def __init__(self, response):
        self.response, self.sent = response, []

    def request(self, method, url, **kwargs):
        self.sent.append((method, url, kwargs))
        return self.response


def client_with(guard, response):
    transport = Transport(response)
    wp = WordPressClient(LOCAL, "admin", "pw", transport=transport, write_guard=guard)
    return wp, transport


@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_a_redirected_write_fails_and_is_not_sent_again(allowed, status):
    response = Response(status=status, headers={"Location": "https://live-site.com/wp-json/"})
    wp, transport = client_with(allowed, response)
    outcome = wp._call("POST", f"{LOCAL}/wp-json/wp/v2/pages/7", json={"title": "x"})
    assert not outcome and outcome.code == "write_redirected"
    assert "live-site.com" in outcome.detail
    assert len(transport.sent) == 1              # no second attempt anywhere


def test_a_write_does_not_follow_redirects_at_all(allowed):
    wp, transport = client_with(allowed, Response())
    wp._call("POST", f"{LOCAL}/wp-json/wp/v2/pages/7", json={"title": "x"})
    assert transport.sent[0][2]["allow_redirects"] is False


def test_a_write_that_already_hopped_is_refused_even_to_a_listed_origin(allowed):
    # Refused rather than accepted: the body is not guaranteed to survive a
    # hop, and the caller should address the right URL itself.
    response = Response(history=[Response(status=301)])
    wp, _ = client_with(allowed, response)
    outcome = wp._call("POST", f"{LOCAL}/wp-json/wp/v2/pages/7", json={"title": "x"})
    assert not outcome and outcome.code == "write_redirected"


def test_a_refused_target_never_reaches_the_network(allowed):
    wp, transport = client_with(allowed, Response())
    outcome = wp._call("POST", "https://live-site.com/wp-json/wp/v2/pages/7", json={})
    assert not outcome and transport.sent == []


def test_without_a_guard_nothing_changes(tmp_path):
    # Skills' publish modes keep their behaviour until they opt in.
    transport = Transport(Response())
    wp = WordPressClient(LOCAL, "admin", "pw", transport=transport)
    assert wp._call("POST", "https://anywhere.example.com/x", json={})
    assert "allow_redirects" not in transport.sent[0][2]
