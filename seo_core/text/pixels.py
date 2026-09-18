"""
How wide a title or description actually renders in a search result.
====================================================================

Google truncates a snippet by **pixel width**, not by character count. The
advice to "keep titles under 60 characters" is a rule of thumb built on an
average that no real title has: sixty capital W's are more than twice as wide
as sixty lowercase l's, and one of those titles is cut in half while the other
has room to spare.

So width is measured character by character, against Arial's own metrics —
the font family Google renders results in — and compared against the widths
observed in live results.

Two things this module exists to catch that a character count cannot:

    1. A title that fits until the theme appends " | Business Name". What
       Yoast stores and what Google renders are different strings, and only
       the second one gets truncated. `apply_template` expands the templates
       so the measurement is taken on the rendered title.

    2. A title that fits in English and overflows in Hebrew, or the reverse.
       Hebrew letters are close to uniform in width; English is not. The same
       character count means different things in the two languages.

The pixel limits are approximations of observed behaviour, not a published
specification, and Google varies them by device and result type. They are
used as a threshold for "this will be cut", never reported as an exact
boundary.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

Kind    = Literal["title", "description"]
Surface = Literal["desktop", "mobile"]

# ═══════════════════════════════════════════════════════
#  Metrics
# ═══════════════════════════════════════════════════════

#: Arial advance widths, in units of 1/1000 em, from the font's own metrics.
_LATIN = {
    " ": 278, "!": 278, '"': 355, "#": 556, "$": 556, "%": 889, "&": 667,
    "'": 191, "(": 333, ")": 333, "*": 389, "+": 584, ",": 278, "-": 333,
    ".": 278, "/": 278, ":": 278, ";": 278, "<": 584, "=": 584, ">": 584,
    "?": 556, "@": 1015, "[": 278, "\\": 278, "]": 278, "^": 469, "_": 556,
    "`": 333, "{": 334, "|": 260, "}": 334, "~": 584,
    "A": 667, "B": 667, "C": 722, "D": 722, "E": 667, "F": 611, "G": 778,
    "H": 722, "I": 278, "J": 500, "K": 667, "L": 556, "M": 833, "N": 722,
    "O": 778, "P": 667, "Q": 778, "R": 722, "S": 667, "T": 611, "U": 722,
    "V": 667, "W": 944, "X": 667, "Y": 667, "Z": 611,
    "a": 556, "b": 556, "c": 500, "d": 556, "e": 556, "f": 278, "g": 556,
    "h": 556, "i": 222, "j": 222, "k": 500, "l": 222, "m": 833, "n": 556,
    "o": 556, "p": 556, "q": 556, "r": 333, "s": 500, "t": 278, "u": 556,
    "v": 500, "w": 722, "x": 500, "y": 500, "z": 500,
}
_LATIN.update({str(d): 556 for d in range(10)})

#: Hebrew in Arial is far more uniform than Latin — most letters sit near the
#: same advance, and only the four narrow ones are meaningfully different.
_HEBREW_DEFAULT = 570
_HEBREW = {
    "ו": 239, "י": 239, "ן": 239, "ז": 411, "ף": 411, "ר": 502, "ל": 502,
    "ג": 502, "כ": 502, "נ": 502, "ך": 502, "ץ": 411,
    "ם": 578, "ס": 578, "ט": 578, "ח": 578, "ה": 578, "ת": 578, "ב": 578,
    "ד": 502, "א": 578, "מ": 578, "ע": 578, "פ": 578, "צ": 578, "ק": 578,
    "ש": 742,
}

#: Anything we have no metric for — an emoji, a CJK glyph, a box-drawing
#: character. Deliberately wide: a title that overflows is worth flagging, and
#: a decorative glyph is usually wider than a letter, not narrower.
_UNKNOWN_WIDTH = 1000

#: The sizes Google renders results at.
FONT_PX: dict[Kind, int] = {"title": 20, "description": 14}

#: Widths observed in live results, in CSS pixels. `title/desktop` is one
#: line; the mobile figures allow for the extra lines a phone result shows.
LIMITS: dict[tuple[str, str], float] = {
    ("title", "desktop"):        580.0,
    ("title", "mobile"):         920.0,
    ("description", "desktop"):  920.0,
    ("description", "mobile"):  1300.0,
}

#: Below this a title is not being truncated — it is leaving the space empty,
#: which is a different problem with a different fix.
SHORT_TITLE_PX = 300.0

_HEBREW_RANGE = re.compile(r"[֐-׿]")


def char_width(ch: str, font_px: int) -> float:
    """Rendered width of one character, in CSS pixels."""
    units = _LATIN.get(ch)
    if units is None:
        units = _HEBREW.get(ch, _HEBREW_DEFAULT if _HEBREW_RANGE.match(ch) else None)
    if units is None:
        units = _UNKNOWN_WIDTH
    return units * font_px / 1000.0


def width(text: str, font_px: int) -> float:
    """Rendered width of a string, in CSS pixels."""
    return sum(char_width(ch, font_px) for ch in text)


def is_hebrew(text: str) -> bool:
    return bool(_HEBREW_RANGE.search(text))


# ═══════════════════════════════════════════════════════
#  Templates — what is stored is not what renders
# ═══════════════════════════════════════════════════════

#: Every plugin invents its own variable syntax for the same four values. A
#: title measured before the template is applied is measured on a string no
#: searcher will ever see.
_TEMPLATE_VARS = {
    "title":     ("%%title%%", "%title%", "%%post_title%%", "%%pt_single%%"),
    "sitename":  ("%%sitename%%", "%sitename%", "%%sitetitle%%", "%%site_title%%"),
    "sep":       ("%%sep%%", "%sep%", "%%separator%%"),
    "page":      ("%%page%%", "%page%"),
    "tagline":   ("%%sitedesc%%", "%sitedesc%", "%%tagline%%"),
}

DEFAULT_SEPARATOR = "-"


def apply_template(
    stored: str,
    template: str = "",
    *,
    site_name: str = "",
    separator: str = DEFAULT_SEPARATOR,
    tagline: str = "",
) -> str:
    """The title as Google receives it, after the plugin expands its template.

    A page with no title of its own falls back to the template alone, which is
    how a site ends up with four pages all titled "Business Name".
    """
    if not template:
        return stored.strip()

    rendered = template
    values = {
        "title": stored.strip(), "sitename": site_name,
        "sep": separator, "page": "", "tagline": tagline,
    }
    for key, variants in _TEMPLATE_VARS.items():
        for variant in variants:
            rendered = rendered.replace(variant, values[key])

    # An empty variable leaves its separator stranded: "Name |  | Site".
    rendered = re.sub(r"\s*[|\-–—·»]\s*([|\-–—·»]\s*)+", f" {separator} ", rendered)
    rendered = re.sub(r"^\s*[|\-–—·»]\s*|\s*[|\-–—·»]\s*$", "", rendered)
    return re.sub(r"\s{2,}", " ", rendered).strip()


# ═══════════════════════════════════════════════════════
#  Measurement
# ═══════════════════════════════════════════════════════

@dataclass(frozen=True)
class Measurement:
    """What one string costs in a search result, and what survives."""

    text: str
    kind: Kind
    surface: Surface
    pixels: float
    limit: float
    visible: str                # the part a searcher reads
    lost: str                   # the part that is cut

    @property
    def truncated(self) -> bool:
        return bool(self.lost)

    @property
    def overflow_px(self) -> float:
        return max(0.0, self.pixels - self.limit)

    @property
    def fill(self) -> float:
        """How much of the available width is used, 0–1+."""
        return self.pixels / self.limit if self.limit else 0.0

    def describe(self) -> str:
        if self.truncated:
            return (
                f"{self.pixels:.0f}px מתוך {self.limit:.0f} — "
                f"נחתך אחרי “{self.visible.strip()}”"
            )
        return f"{self.pixels:.0f}px מתוך {self.limit:.0f} ({self.fill:.0%} מהרוחב)"


def measure(text: str, kind: Kind, surface: Surface = "desktop") -> Measurement:
    """Measure a title or description and say exactly where it would be cut.

    The cut is taken at a word boundary, the way a truncated snippet actually
    reads — Google does not cut mid-word and neither does this.
    """
    font_px = FONT_PX[kind]
    limit = LIMITS[(kind, surface)]
    total = width(text, font_px)

    if total <= limit:
        return Measurement(text, kind, surface, total, limit, text, "")

    # The ellipsis occupies width too, so it has to come out of the budget.
    budget = limit - width("…", font_px)
    visible, used = "", 0.0
    for ch in text:
        step = char_width(ch, font_px)
        if used + step > budget:
            break
        visible += ch
        used += step

    cut = visible.rstrip()
    if " " in cut and not text[len(visible):len(visible) + 1].isspace():
        cut = cut[: cut.rindex(" ")]

    return Measurement(text, kind, surface, total, limit, cut, text[len(cut):].strip())


def preview(text: str, kind: Kind, surface: Surface = "desktop") -> str:
    """The snippet line as it would appear, ellipsis and all."""
    m = measure(text, kind, surface)
    return f"{m.visible.strip()}…" if m.truncated else m.text


def fits(text: str, kind: Kind, surface: Surface = "desktop") -> bool:
    return not measure(text, kind, surface).truncated


def budget_chars(kind: Kind, sample: str, surface: Surface = "desktop") -> int:
    """How many more characters of this kind of text would still fit.

    Written for a person about to edit a title: "you have room for eight more
    characters like the ones already there" is actionable in a way that a
    pixel figure is not.
    """
    m = measure(sample, kind, surface)
    if m.overflow_px:
        return 0
    letters = [c for c in sample if not c.isspace()]
    if not letters:
        return 0
    average = width("".join(letters), FONT_PX[kind]) / len(letters)
    return int((m.limit - m.pixels) / average) if average else 0


def infer_affixes(stored: str, rendered: str) -> tuple[str, str]:
    """What the theme wraps around a stored title, learned rather than guessed.

    Reading the plugin's title template out of `wp_options` is not possible
    over the REST API, and guessing it is how a title is measured against the
    wrong string. But the site already answers the question: fetch the page,
    compare the title the plugin stores with the title the browser receives,
    and whatever is left on either side is the template.

    Returns two empty strings when the stored title is not visibly inside the
    rendered one — which itself means something, usually that Google is being
    handed a title nobody in the CMS chose.
    """
    stored, rendered = stored.strip(), rendered.strip()
    if not stored or stored not in rendered:
        return "", ""
    cut = rendered.index(stored)
    return rendered[:cut], rendered[cut + len(stored):]


def render_like(candidate: str, prefix: str, suffix: str) -> str:
    """A proposed title as the live site would render it."""
    return f"{prefix}{candidate.strip()}{suffix}"
