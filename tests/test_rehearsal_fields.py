"""The restore is verified field by field, not by a hash alone.

A hash covers what went into it. Yoast's title and description do not, so a
page could hash identically and still come back with its SEO title wrong —
which is the one thing a drill exists to rule out.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from seo_core.wp import rehearsal                   # noqa: E402


@dataclass
class Snapshot:
    content: str
    meta: dict[str, Any]


@dataclass
class Page:
    raw_content: str
    meta: dict[str, Any]


def test_every_backed_up_field_is_on_the_list():
    snapshot = Snapshot("body", {"_yoast_wpseo_title": "T", "_yoast_wpseo_metadesc": "D"})
    assert rehearsal.changed_fields(snapshot) == [
        "content", "meta._yoast_wpseo_metadesc", "meta._yoast_wpseo_title"]


def test_a_faithful_restore_reports_no_differences():
    snapshot = Snapshot("body", {"_yoast_wpseo_title": "T"})
    page = Page("body", {"_yoast_wpseo_title": "T"})
    assert rehearsal.compare_fields(snapshot, page) == []


def test_an_seo_title_that_did_not_come_back_is_caught():
    # The content is byte-identical, so the hash matches. Only the field-by-
    # field pass can see this.
    snapshot = Snapshot("body", {"_yoast_wpseo_title": "Back Facial Sarasota"})
    page = Page("body", {"_yoast_wpseo_title": "Untitled"})
    [difference] = rehearsal.compare_fields(snapshot, page)
    assert difference["field"] == "meta._yoast_wpseo_title"
    assert difference["before"] == "Back Facial Sarasota" and difference["after"] == "Untitled"


def test_a_field_that_lost_its_text_is_caught():
    snapshot = Snapshot("body", {"_yoast_wpseo_metadesc": "A real description"})
    assert rehearsal.compare_fields(snapshot, Page("body", {}))
    assert rehearsal.compare_fields(snapshot, Page("body", {"_yoast_wpseo_metadesc": ""}))


def test_empty_and_absent_are_the_same_value():
    # get_post_meta($id, $key, true) returns '' for a key that is not there,
    # so no reader can tell them apart. Elementor stores _elementor_css empty
    # and it comes back absent on every single drill.
    snapshot = Snapshot("body", {"_elementor_css": ""})
    assert rehearsal.compare_fields(snapshot, Page("body", {})) == []
    assert rehearsal.compare_fields(snapshot, Page("body", {"_elementor_css": None})) == []


def test_content_that_did_not_come_back_is_caught():
    [difference] = rehearsal.compare_fields(Snapshot("before", {}), Page("after", {}))
    assert difference["field"] == "content"


def test_long_values_are_shortened_for_display():
    snapshot = Snapshot("x" * 500, {})
    [difference] = rehearsal.compare_fields(snapshot, Page("y" * 500, {}))
    assert len(difference["before"]) <= 121 and difference["before"].endswith("…")
