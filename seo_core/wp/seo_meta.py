
"""
Reading and writing SEO title, description, canonical and robots.
=================================================================

Yoast, Rank Math and SEOPress each store the same four values under different
post meta keys. Writing Yoast's key on a Rank Math site is not an error — the
REST call succeeds, the meta is stored, and nothing about the rendered page
changes, because nothing reads that key. The failure is invisible until
someone checks the live `<title>` weeks later.

So the plugin is identified first, and an unidentified plugin is a refusal
rather than a guess.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

from ..schema import Result

Plugin = Literal["yoast", "rankmath", "seopress", "unknown"]

FIELDS: dict[str, dict[str, str]] = {
    "yoast": {
        "title":       "_yoast_wpseo_title",
        "description": "_yoast_wpseo_metadesc",
        "canonical":   "_yoast_wpseo_canonical",
        "noindex":     "_yoast_wpseo_meta-robots-noindex",
    },
    "rankmath": {
        "title":       "rank_math_title",
        "description": "rank_math_description",
        "canonical":   "rank_math_canonical_url",
        "noindex":     "rank_math_robots",
    },
    "seopress": {
        "title":       "_seopress_titles_title",
        "description": "_seopress_titles_desc",
        "canonical":   "_seopress_robots_canonical",
        "noindex":     "_seopress_robots_index",
    },
}

#: Fingerprints in the rendered HTML, used when post meta is not conclusive.
_HTML_SIGNATURES: list[tuple[Plugin, re.Pattern[str]]] = [
    ("yoast",    re.compile(r"yoast\s+seo\s+plugin|yoast_wpseo|<!--\s*/?\s*Yoast", re.I)),
    ("rankmath", re.compile(r"rank\s*math|rank-math", re.I)),
    ("seopress", re.compile(r"seopress", re.I)),
]

#: Yoast and SEOPress use "1"/"0"; Rank Math stores a list of robots directives.
_NOINDEX_TRUE = {"1", "true", "noindex", "yes"}


@dataclass
class SeoFields:
    plugin: Plugin
    title: str = ""
    description: str = ""
    canonical: str = ""
    noindex: bool = False

    @property
    def is_noindexed(self) -> bool:
        return self.noindex


def detect_plugin(post_meta: dict[str, Any], rendered_html: str = "") -> Plugin:
    """Identify the SEO plugin from post meta, falling back to page HTML.

    Meta keys are checked first because they are definitive for this post.
    A theme mentioning a plugin name in a comment would fool the HTML pass
    alone, but it cannot invent a populated meta key.
    """
    for plugin, keys in FIELDS.items():
        if any(key in post_meta for key in keys.values()):
            return plugin                                  # type: ignore[return-value]

    for plugin, pattern in _HTML_SIGNATURES:
        if pattern.search(rendered_html):
            return plugin
    return "unknown"


def read(post_meta: dict[str, Any], plugin: Plugin) -> Result:
    """Read the current SEO fields for a post."""
    if plugin == "unknown":
        return Result.failure(
            "plugin_unknown",
            "לא זוהה תוסף SEO — קריאה או כתיבה כאן תהיה ניחוש",
            recoverable=True,
        )

    keys = FIELDS[plugin]
    raw_noindex = post_meta.get(keys["noindex"], "")
    if isinstance(raw_noindex, (list, tuple)):             # Rank Math
        noindex = any(str(v).lower() == "noindex" for v in raw_noindex)
    else:
        noindex = str(raw_noindex).strip().lower() in _NOINDEX_TRUE

    return Result.success(
        "read",
        f"נקרא מ-{plugin}",
        fields=SeoFields(
            plugin=plugin,
            title=str(post_meta.get(keys["title"], "") or ""),
            description=str(post_meta.get(keys["description"], "") or ""),
            canonical=str(post_meta.get(keys["canonical"], "") or ""),
            noindex=noindex,
        ),
    )


def write_payload(
    plugin: Plugin,
    *,
    title: str | None = None,
    description: str | None = None,
    canonical: str | None = None,
) -> Result:
    """Build the meta payload for a write, and the keys needed to undo it.

    `noindex` is deliberately not writable here. Turning indexing on or off is
    never a side effect of a title rewrite, and the one place it legitimately
    changes — clearing a staging flag after a migration — is explicit and
    supervised.
    """
    if plugin == "unknown":
        return Result.failure(
            "plugin_unknown",
            "אי אפשר לכתוב שדות SEO בלי לדעת איזה תוסף מותקן",
            recoverable=True,
        )

    keys = FIELDS[plugin]
    payload: dict[str, Any] = {}
    if title is not None:
        payload[keys["title"]] = title
    if description is not None:
        payload[keys["description"]] = description
    if canonical is not None:
        payload[keys["canonical"]] = canonical

    if not payload:
        return Result.failure("empty_write", "לא הועבר שום שדה לעדכון")

    return Result.success(
        "payload_ready",
        f"מוכן לכתיבה ל-{plugin}",
        meta=payload,
        touched_keys=sorted(payload),
    )


def inverse_payload(post_meta: dict[str, Any], touched_keys: list[str]) -> dict[str, Any]:
    """The meta values needed to restore exactly the fields a write touched.

    A key absent before the write is restored as an empty string rather than
    being dropped: omitting it from the restore would leave our value in place.
    """
    return {key: post_meta.get(key, "") for key in touched_keys}
