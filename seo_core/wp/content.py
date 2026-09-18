"""
Reading and editing post content, whichever builder produced it.
================================================================

The single most expensive mistake in automated WordPress editing is writing to
`post_content` on a page built with Elementor. The request succeeds, the
revision is created, and nothing changes on screen — because Elementor renders
from a JSON widget tree stored in the `_elementor_data` post meta, and
`post_content` is only a stale fallback copy.

So every edit starts by asking which builder owns the page:

    elementor   the widget tree in `_elementor_data` is the truth
    gutenberg   `post_content` is block markup and must stay valid block markup
    classic     `post_content` is plain HTML

Inserting raw HTML into a Gutenberg post is the quieter version of the same
mistake: it renders, but the editor flags every touched block as containing
unexpected content, and the next human edit may silently drop it.
"""

from __future__ import annotations

import html
import json
import re
import uuid
from dataclasses import dataclass
from typing import Any, Literal

from ..schema import Result, content_hash

Builder = Literal["elementor", "gutenberg", "classic"]

#: Elementor writes this meta key when a page is built with it. The value is
#: "builder" for Elementor-rendered pages; pages reverted to the classic editor
#: keep the meta with a different value, so the value matters, not the key.
ELEMENTOR_MODE_KEY = "_elementor_edit_mode"
ELEMENTOR_DATA_KEY = "_elementor_data"

#: Elementor caches generated CSS per post. An edit that skips the cache bump
#: can render with the previous layout's styles until something else clears it.
ELEMENTOR_CSS_KEY = "_elementor_css"

_BLOCK_OPEN = re.compile(r"<!--\s*wp:")


@dataclass
class PostContent:
    """A post's editable content, normalised across builders."""

    post_id: int
    post_type: str
    builder: Builder
    raw_content: str                     # post_content as stored
    elementor_tree: list[dict[str, Any]] | None = None
    meta: dict[str, Any] | None = None

    @property
    def hash(self) -> str:
        """Identity of everything an edit might touch.

        Includes the Elementor tree, because on an Elementor page the tree is
        what changes while `post_content` sits still — the exact case a
        `modified_gmt` comparison misses.

        Empty meta values are dropped first. WordPress cannot unset a meta key
        over REST, so restoring a backup writes `""` where the key had been
        absent; hashing those two states differently would make every restored
        page look as though someone else had edited it.
        """
        return content_hash(
            self.raw_content, self.elementor_tree, _meaningful(self.meta)
        )

    @property
    def plain_text(self) -> str:
        """Visible text, for voice matching and term-frequency checks."""
        if self.builder == "elementor" and self.elementor_tree is not None:
            parts = _collect_elementor_text(self.elementor_tree)
            return _strip_tags(" ".join(parts))
        return _strip_tags(self.raw_content)


def _meaningful(meta: dict[str, Any] | None) -> dict[str, Any]:
    """Meta with empty values removed — absent and empty are the same state."""
    return {k: v for k, v in (meta or {}).items() if v not in ("", None, [])}


# ═══════════════════════════════════════════════════════
#  Detection
# ═══════════════════════════════════════════════════════

def detect_builder(post: dict[str, Any]) -> Builder:
    """Decide which builder owns this page, from a REST post payload."""
    meta = post.get("meta") or {}

    if str(meta.get(ELEMENTOR_MODE_KEY, "")).strip() == "builder":
        return "elementor"

    content = _raw_content(post)
    if _BLOCK_OPEN.search(content):
        return "gutenberg"
    return "classic"


def load(post: dict[str, Any], post_type: str = "posts") -> Result:
    """Turn a REST post payload into a `PostContent`."""
    builder = detect_builder(post)
    meta = dict(post.get("meta") or {})
    tree: list[dict[str, Any]] | None = None

    if builder == "elementor":
        raw = meta.get(ELEMENTOR_DATA_KEY)
        if not raw:
            return Result.failure(
                "elementor_data_missing",
                "הדף מסומן כ-Elementor אבל _elementor_data חסר — "
                "כנראה meta לא חשוף ב-REST. עריכה כאן תיכתב ולא תופיע",
                recoverable=True,
            )
        try:
            tree = json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError as exc:
            return Result.failure(
                "elementor_data_corrupt",
                f"_elementor_data אינו JSON תקין: {exc}",
                recoverable=False,
            )
        if not isinstance(tree, list):
            return Result.failure(
                "elementor_data_corrupt", "_elementor_data אינו מערך כצפוי", recoverable=False
            )

    return Result.success(
        "loaded",
        f"נטען כ-{builder}",
        content=PostContent(
            post_id=post.get("id", 0),
            post_type=post_type,
            builder=builder,
            raw_content=_raw_content(post),
            elementor_tree=tree,
            meta=meta,
        ),
    )


# ═══════════════════════════════════════════════════════
#  Editing
# ═══════════════════════════════════════════════════════

def insert_paragraph_after_heading(
    content: PostContent, heading: str, paragraph: str
) -> Result:
    """Add a paragraph directly after the named heading.

    Returns the payload to write plus the inverse needed to undo it. The
    heading is matched on its visible text, case-insensitively and ignoring
    surrounding markup, because that is how the heading was identified in the
    first place — by reading the rendered page.
    """
    if not paragraph.strip():
        return Result.failure("empty_paragraph", "אין טקסט להוסיף")

    if content.builder == "elementor":
        return _insert_elementor(content, heading, paragraph)
    if content.builder == "gutenberg":
        return _insert_gutenberg(content, heading, paragraph)
    return _insert_classic(content, heading, paragraph)


def insert_section_after_heading(
    content: PostContent, after_heading: str, new_heading: str, paragraph: str
) -> Result:
    """Add a whole section — an H2 and its answer — after an existing heading.

    Separate from `insert_paragraph_after_heading` because a query the page
    never addresses needs a heading of its own to be found under; appending a
    paragraph to somebody else's section buries it.
    """
    if not new_heading.strip() or not paragraph.strip():
        return Result.failure("empty_section", "צריך גם כותרת וגם טקסט")

    if content.builder == "elementor":
        return _insert_elementor_section(content, after_heading, new_heading, paragraph)
    if content.builder == "gutenberg":
        return _insert_gutenberg_section(content, after_heading, new_heading, paragraph)
    return _insert_classic_section(content, after_heading, new_heading, paragraph)


def _insert_gutenberg_section(
    content: PostContent, after_heading: str, new_heading: str, paragraph: str
) -> Result:
    blocks = _split_top_level_blocks(content.raw_content)
    target = _find_block_with_heading(blocks, after_heading)
    if target is None:
        return Result.failure(
            "heading_not_found", f"לא נמצאה כותרת {after_heading!r} בדף", recoverable=True
        )

    # The section is inserted after the target heading's own block *and* the
    # content that belongs to it, so it does not split an existing answer.
    end = _end_of_section(blocks, target)
    blocks[end:end] = [
        "<!-- wp:heading -->\n"
        f"<h2>{_escape_body(new_heading)}</h2>\n"
        "<!-- /wp:heading -->",
        "<!-- wp:paragraph -->\n"
        f"<p>{_escape_body(paragraph)}</p>\n"
        "<!-- /wp:paragraph -->",
    ]
    return Result.success(
        "composed",
        f"סעיף {new_heading!r} יתווסף אחרי הסעיף של {after_heading!r}",
        payload={"content": "\n\n".join(b for b in blocks if b.strip())},
        inverse={"content": content.raw_content},
        builder="gutenberg",
    )


def _end_of_section(blocks: list[str], start: int) -> int:
    """Index just past the last block belonging to the heading at `start`."""
    for index in range(start + 1, len(blocks)):
        if "wp:heading" in blocks[index]:
            return index
    return len(blocks)


def _insert_classic_section(
    content: PostContent, after_heading: str, new_heading: str, paragraph: str
) -> Result:
    markup = content.raw_content
    start = _find_classic_heading(markup, after_heading)
    if start is None:
        return Result.failure(
            "heading_not_found", f"לא נמצאה כותרת {after_heading!r} בדף", recoverable=True
        )

    following = re.search(r"<h[1-6]\b", markup[start:], re.I)
    insert_at = start + following.start() if following else len(markup)

    section = (
        f"\n<h2>{_escape_body(new_heading)}</h2>\n"
        f"<p>{_escape_body(paragraph)}</p>\n"
    )
    return Result.success(
        "composed",
        f"סעיף {new_heading!r} יתווסף אחרי הסעיף של {after_heading!r}",
        payload={"content": markup[:insert_at] + section + markup[insert_at:]},
        inverse={"content": markup},
        builder="classic",
    )


def _insert_elementor_section(
    content: PostContent, after_heading: str, new_heading: str, paragraph: str
) -> Result:
    tree = json.loads(json.dumps(content.elementor_tree))     # deep copy

    # Inserted in reverse, so the heading ends up above its own paragraph.
    for widget in (_text_widget(paragraph), _heading_widget(new_heading)):
        if not _place_after_heading_widget(tree, after_heading, widget):
            return Result.failure(
                "heading_not_found",
                f"לא נמצא ווידג'ט כותרת {after_heading!r} בעץ של Elementor",
                recoverable=True,
            )

    return Result.success(
        "composed",
        f"סעיף {new_heading!r} יתווסף אחרי {after_heading!r} באותה עמודה",
        payload={
            "meta": {
                ELEMENTOR_DATA_KEY: json.dumps(tree, ensure_ascii=False),
                ELEMENTOR_CSS_KEY: "",
            }
        },
        inverse={
            "meta": {
                ELEMENTOR_DATA_KEY: json.dumps(
                    content.elementor_tree, ensure_ascii=False
                ),
                ELEMENTOR_CSS_KEY: (content.meta or {}).get(ELEMENTOR_CSS_KEY, ""),
            }
        },
        builder="elementor",
        needs_css_regeneration=True,
    )


def _heading_widget(title: str) -> dict[str, Any]:
    return {
        "id": uuid.uuid4().hex[:7],
        "elType": "widget",
        "widgetType": "heading",
        "settings": {"title": title, "header_size": "h2"},
        "elements": [],
    }


def _insert_gutenberg(content: PostContent, heading: str, paragraph: str) -> Result:
    """Insert a valid paragraph block after the heading block."""
    blocks = _split_top_level_blocks(content.raw_content)
    target = _find_block_with_heading(blocks, heading)
    if target is None:
        return Result.failure(
            "heading_not_found", f"לא נמצאה כותרת {heading!r} בדף", recoverable=True
        )

    new_block = (
        "<!-- wp:paragraph -->\n"
        f"<p>{_escape_body(paragraph)}</p>\n"
        "<!-- /wp:paragraph -->"
    )
    blocks.insert(target + 1, new_block)
    updated = "\n\n".join(b for b in blocks if b.strip())

    return Result.success(
        "composed",
        f"פסקה תתווסף אחרי {heading!r} כבלוק Gutenberg תקין",
        payload={"content": updated},
        inverse={"content": content.raw_content},
        builder="gutenberg",
    )


def _insert_classic(content: PostContent, heading: str, paragraph: str) -> Result:
    insert_at = _find_classic_heading(content.raw_content, heading)
    if insert_at is None:
        return Result.failure(
            "heading_not_found", f"לא נמצאה כותרת {heading!r} בדף", recoverable=True
        )

    updated = (
        content.raw_content[:insert_at]
        + f"\n<p>{_escape_body(paragraph)}</p>\n"
        + content.raw_content[insert_at:]
    )
    return Result.success(
        "composed",
        f"פסקה תתווסף אחרי {heading!r}",
        payload={"content": updated},
        inverse={"content": content.raw_content},
        builder="classic",
    )


def _insert_elementor(content: PostContent, heading: str, paragraph: str) -> Result:
    """Insert a text-editor widget after the heading widget in the tree.

    The new widget is placed as a sibling inside the same column, which is what
    keeps it inside the heading's visual section rather than appended to the
    end of the page.
    """
    tree = json.loads(json.dumps(content.elementor_tree))   # deep copy
    placed = _place_after_heading_widget(tree, heading, _text_widget(paragraph))
    if not placed:
        return Result.failure(
            "heading_not_found",
            f"לא נמצא ווידג'ט כותרת {heading!r} בעץ של Elementor",
            recoverable=True,
        )

    return Result.success(
        "composed",
        f"ווידג'ט טקסט יתווסף אחרי {heading!r} באותה עמודה",
        payload={
            "meta": {
                ELEMENTOR_DATA_KEY: json.dumps(tree, ensure_ascii=False),
                # Bumping the cached CSS forces Elementor to regenerate it.
                ELEMENTOR_CSS_KEY: "",
            }
        },
        inverse={
            "meta": {
                ELEMENTOR_DATA_KEY: json.dumps(
                    content.elementor_tree, ensure_ascii=False
                ),
                ELEMENTOR_CSS_KEY: (content.meta or {}).get(ELEMENTOR_CSS_KEY, ""),
            }
        },
        builder="elementor",
        needs_css_regeneration=True,
    )


# ═══════════════════════════════════════════════════════
#  Elementor tree helpers
# ═══════════════════════════════════════════════════════

def _text_widget(paragraph: str) -> dict[str, Any]:
    return {
        "id": uuid.uuid4().hex[:7],
        "elType": "widget",
        "widgetType": "text-editor",
        "settings": {"editor": f"<p>{_escape_body(paragraph)}</p>"},
        "elements": [],
    }


def _place_after_heading_widget(
    elements: list[dict[str, Any]], heading: str, widget: dict[str, Any]
) -> bool:
    """Walk the tree, inserting `widget` after the matching heading widget."""
    wanted = _normalise(heading)

    for index, element in enumerate(elements):
        if _is_heading_widget(element, wanted):
            elements.insert(index + 1, widget)
            return True
        children = element.get("elements")
        if isinstance(children, list) and _place_after_heading_widget(
            children, heading, widget
        ):
            return True
    return False


def list_headings(content: PostContent) -> list[str]:
    """Every heading on the page, in document order.

    Used wherever a caller has to pick an insertion point without a human
    naming one — the staging rehearsal, for instance, which needs any heading
    at all rather than a particular one.
    """
    if content.builder == "elementor" and content.elementor_tree is not None:
        return _collect_elementor_headings(content.elementor_tree)

    return [
        _normalise_display(_strip_tags(m.group(2)))
        for m in re.finditer(r"<h([1-6])\b[^>]*>(.*?)</h\1>", content.raw_content, re.I | re.S)
        if _strip_tags(m.group(2)).strip()
    ]


def _collect_elementor_headings(elements: list[dict[str, Any]]) -> list[str]:
    found: list[str] = []
    for element in elements:
        if element.get("elType") == "widget" and element.get("widgetType") in (
            "heading", "theme-post-title"
        ):
            title = _strip_tags(str((element.get("settings") or {}).get("title", "")))
            if title.strip():
                found.append(_normalise_display(title))
        children = element.get("elements")
        if isinstance(children, list):
            found.extend(_collect_elementor_headings(children))
    return found


def _normalise_display(text: str) -> str:
    """Collapse whitespace but keep the original casing, for display and matching."""
    return re.sub(r"\s+", " ", text).strip()


def _is_heading_widget(element: dict[str, Any], wanted: str) -> bool:
    if element.get("elType") != "widget":
        return False
    if element.get("widgetType") not in ("heading", "theme-post-title"):
        return False
    title = (element.get("settings") or {}).get("title", "")
    return _normalise(_strip_tags(str(title))) == wanted


def _collect_elementor_text(elements: list[dict[str, Any]]) -> list[str]:
    """Pull every piece of visible text out of the widget tree."""
    found: list[str] = []
    for element in elements:
        settings = element.get("settings") or {}
        for key in ("title", "editor", "text", "description_text", "content"):
            value = settings.get(key)
            if isinstance(value, str) and value.strip():
                found.append(value)
        children = element.get("elements")
        if isinstance(children, list):
            found.extend(_collect_elementor_text(children))
    return found


# ═══════════════════════════════════════════════════════
#  Gutenberg block helpers
# ═══════════════════════════════════════════════════════

def _split_top_level_blocks(markup: str) -> list[str]:
    """Split block markup into top-level blocks, keeping nesting intact.

    A naive split on `<!-- wp:` would tear a columns or group block apart, so
    depth is tracked and only depth-zero boundaries end a block.
    """
    token = re.compile(r"<!--\s*(/?)wp:([a-z0-9-]+(?:/[a-z0-9-]+)?)(.*?)(/?)-->", re.S)
    blocks: list[str] = []
    depth = 0
    start = 0
    cursor = 0

    for match in token.finditer(markup):
        closing, _name, _attrs, self_closing = match.groups()
        if depth == 0 and not closing:
            if cursor < match.start() and markup[cursor:match.start()].strip():
                blocks.append(markup[cursor:match.start()])
            start = match.start()

        if self_closing.strip() == "/":
            if depth == 0:
                blocks.append(markup[start:match.end()])
                cursor = match.end()
            continue

        if closing:
            depth -= 1
            if depth == 0:
                blocks.append(markup[start:match.end()])
                cursor = match.end()
        else:
            depth += 1

    if cursor < len(markup) and markup[cursor:].strip():
        blocks.append(markup[cursor:])
    return blocks


def _find_classic_heading(markup: str, heading: str) -> int | None:
    """Index just past the closing tag of the heading with this visible text.

    Compares stripped text rather than matching the tag literally, so a heading
    wrapped in `<strong>` or carrying an anchor still matches.
    """
    wanted = _normalise(heading)
    for match in re.finditer(r"<h([1-6])\b[^>]*>(.*?)</h\1>", markup, re.I | re.S):
        if _normalise(_strip_tags(match.group(2))) == wanted:
            return match.end()
    return None


def _find_block_with_heading(blocks: list[str], heading: str) -> int | None:
    wanted = _normalise(heading)
    for index, block in enumerate(blocks):
        if "wp:heading" not in block:
            continue
        if _normalise(_strip_tags(block)) == wanted:
            return index
    return None


# ═══════════════════════════════════════════════════════
#  Text helpers
# ═══════════════════════════════════════════════════════

def _raw_content(post: dict[str, Any]) -> str:
    content = post.get("content")
    if isinstance(content, dict):
        return content.get("raw") or content.get("rendered") or ""
    return content or ""


def _strip_tags(markup: str) -> str:
    without_comments = re.sub(r"<!--.*?-->", " ", markup, flags=re.S)
    return html.unescape(re.sub(r"<[^>]+>", " ", without_comments))


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def _escape_body(text: str) -> str:
    """Escape text for insertion, leaving an already-marked-up string alone.

    Copy handed to us often contains a deliberate `<strong>` or an internal
    link; escaping that would surface the tags to the reader as literal text.
    """
    if re.search(r"</?(a|strong|em|b|i|br)\b", text, re.I):
        return text
    return html.escape(text, quote=False)


# ═══════════════════════════════════════════════════════
#  Linking a phrase that is already on the page
# ═══════════════════════════════════════════════════════

#: Widget settings that hold rich text in an Elementor tree.
_ELEMENTOR_TEXT_KEYS = ("editor", "text", "description_text")

_TOKENS = re.compile(r"(<[^>]+>)")
_HEADING_TAG = re.compile(r"</?h[1-6]\b", re.I)


def _link_first_occurrence(markup: str, phrase: str, href: str) -> str | None:
    """Wrap the first eligible occurrence of `phrase` in a link.

    Eligible means in body text: not inside a tag's attributes, not inside an
    anchor that already exists, and not inside a heading. Linking a heading
    changes what the page is about; nesting an anchor produces markup no
    browser agrees on; and matching inside an attribute would corrupt the page
    outright.

    Returns None when no eligible occurrence exists, so the caller can say so
    instead of reporting a change that did not happen.
    """
    pattern = re.compile(rf"\b{re.escape(phrase)}\b", re.I | re.U)
    tokens = _TOKENS.split(markup)

    in_anchor = 0
    in_heading = 0
    for index, token in enumerate(tokens):
        if index % 2:                                  # a tag
            lowered = token.lower()
            if lowered.startswith("<a") and not lowered.startswith("</a"):
                in_anchor += 1
            elif lowered.startswith("</a"):
                in_anchor = max(0, in_anchor - 1)
            elif _HEADING_TAG.match(token):
                in_heading = 0 if lowered.startswith("</") else in_heading + 1
            continue

        if in_anchor or in_heading or not token.strip():
            continue

        match = pattern.search(token)
        if not match:
            continue

        anchor = match.group(0)
        tokens[index] = (
            token[: match.start()]
            + f'<a href="{href}">{anchor}</a>'
            + token[match.end():]
        )
        return "".join(tokens)

    return None


def link_phrase(
    content: PostContent, phrase: str, target_url: str
) -> Result:
    """Turn a phrase the page already says into a link to another page.

    This is the safest internal link there is: the sentence was written by a
    human, the phrase is already in it, and nothing is added or removed —
    only wrapped. It is also idempotent, which matters more here than
    elsewhere, because running a link-building pass twice on the same page is
    the obvious way to end up with the same link twice in one paragraph.
    """
    if not phrase.strip():
        return Result.failure("empty_phrase", "לא נמסר ביטוי לקישור")
    if not target_url.strip():
        return Result.failure("empty_target", "לא נמסרה כתובת יעד")

    if content.builder == "elementor":
        return _link_elementor(content, phrase, target_url)
    return _link_markup(content, phrase, target_url)


def _already_links(markup: str, target_url: str) -> bool:
    return bool(re.search(
        rf"""href\s*=\s*["']{re.escape(target_url)}/?["']""", markup, re.I))


def _link_markup(content: PostContent, phrase: str, target_url: str) -> Result:
    markup = content.raw_content
    if _already_links(markup, target_url):
        return Result.failure(
            "already_linked",
            f"הדף כבר מקשר ל-{target_url} — אין מה להוסיף",
            recoverable=True,
        )

    updated = _link_first_occurrence(markup, phrase, target_url)
    if updated is None:
        return Result.failure(
            "phrase_not_found",
            f"הביטוי {phrase!r} לא נמצא בטקסט הגוף של הדף — "
            "ייתכן שהוא רק בכותרת, בתפריט, או כבר בתוך קישור",
            recoverable=True,
        )

    return Result.success(
        "composed",
        f"הביטוי {phrase!r} יקושר ל-{target_url}",
        payload={"content": updated},
        inverse={"content": markup},
        builder=content.builder,
    )


def _link_elementor(content: PostContent, phrase: str, target_url: str) -> Result:
    tree = json.loads(json.dumps(content.elementor_tree))      # deep copy
    # Checked against the widgets' own text, not the serialised tree: JSON
    # escaping puts a backslash before every quote, so a search for
    # `href="..."` over the serialised form finds nothing and the skill adds
    # a link the page already has.
    existing = " ".join(_collect_elementor_text(tree))
    if _already_links(existing, target_url):
        return Result.failure(
            "already_linked",
            f"הדף כבר מקשר ל-{target_url} — אין מה להוסיף",
            recoverable=True,
        )

    if not _link_in_widgets(tree, phrase, target_url):
        return Result.failure(
            "phrase_not_found",
            f"הביטוי {phrase!r} לא נמצא בטקסט של אף ווידג'ט בעץ של Elementor",
            recoverable=True,
        )

    return Result.success(
        "composed",
        f"הביטוי {phrase!r} יקושר ל-{target_url}",
        payload={
            "meta": {
                ELEMENTOR_DATA_KEY: json.dumps(tree, ensure_ascii=False),
                ELEMENTOR_CSS_KEY: "",
            }
        },
        inverse={
            "meta": {
                ELEMENTOR_DATA_KEY: json.dumps(
                    content.elementor_tree, ensure_ascii=False
                ),
                ELEMENTOR_CSS_KEY: (content.meta or {}).get(ELEMENTOR_CSS_KEY, ""),
            }
        },
        builder="elementor",
        needs_css_regeneration=True,
    )


def _link_in_widgets(
    elements: list[dict[str, Any]], phrase: str, target_url: str
) -> bool:
    """Link the phrase in the first text widget that carries it."""
    for element in elements:
        settings = element.get("settings") or {}
        for key in _ELEMENTOR_TEXT_KEYS:
            value = settings.get(key)
            if not isinstance(value, str) or not value.strip():
                continue
            updated = _link_first_occurrence(value, phrase, target_url)
            if updated is not None:
                settings[key] = updated
                return True

        children = element.get("elements")
        if isinstance(children, list) and _link_in_widgets(children, phrase, target_url):
            return True
    return False
