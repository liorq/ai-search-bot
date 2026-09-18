"""
Titles and descriptions: why a well-ranked page is not being clicked.
=====================================================================

A page at position 3 earning a 2% click-through rate is losing more traffic
than most of the things an SEO audit flags, and none of the usual fixes touch
it. The page already ranks. The content is already good enough that Google put
it there. What is failing is the one line a searcher actually reads.

The trap in this analysis is that a low CTR has many causes, and only some of
them are the title:

    the title is cut off mid-sentence                 → fixable here
    the title does not contain the words searched     → fixable here
    four pages share one title                        → fixable here
    there is no description, so Google wrote one      → fixable here
    an AI Overview sits above every result            → not fixable at all
    the brand's own competitors outbid on ads         → not fixable here

So a structural cause is looked for first, and only a page where one is found
gets a full estimate. A page with a low CTR and no visible reason still gets
reported — it is worth testing — but it claims a fraction of the gap and says
plainly that the cause was not identified.

The second trap is that the title is a ranking signal. A rewrite that drops
the term the page ranks for can cost the position that earned the impressions
in the first place. Every candidate title is checked against the terms already
earning clicks, and one that drops them is refused rather than warned about.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from html import unescape
from dataclasses import dataclass, field
from typing import Any, Iterable, Literal

from ..schema import Finding, Result
from ..sources.queries import (
    CTRCurve, MIN_IMPRESSIONS, QueryRow, covers, _STOPWORDS, _WORD,
)
from . import pixels

# ═══════════════════════════════════════════════════════
#  Thresholds
# ═══════════════════════════════════════════════════════

#: Past this position the snippet is on the second page of results, where
#: nobody reads it. Queries there are excluded from the analysis entirely —
#: not only because the fix would not reach them, but because their near-zero
#: CTR would drag every page's measured rate below par and make the whole site
#: look like a title problem.
MAX_POSITION = 10.0

#: A page has to be missing a real share of its expected clicks before a
#: rewrite is worth the risk of touching a title that already ranks.
UNDERPERFORMANCE_RATIO = 0.7

#: What a snippet rewrite recovers of the gap when a structural cause was
#: found. Not all of it: the searcher's intent, the brand, and everything
#: else on the results page are all still there afterwards.
STRUCTURAL_RECOVERY = 0.60

#: And when no structural cause was found, the claim drops hard. The page may
#: simply be sitting under an AI Overview, which no title fixes.
BLIND_RECOVERY = 0.25

#: Google replaces the title it was given with one of its own in a large
#: minority of results. Quoted in the finding so nobody reads the estimate as
#: a promise that our text will be the text displayed.
GOOGLE_REWRITE_NOTE = (
    "גוגל מחליף טייטלים שהוא מקבל בחלק ניכר מהתוצאות. "
    "הטקסט שנכתב הוא הצעה, לא מה שמוצג בהכרח"
)

#: Two pages sharing a rendered title is only a finding when both are really
#: in the results.
DUPLICATE_MIN_IMPRESSIONS = 30

#: A description under this is not describing anything.
MIN_DESCRIPTION_PX = 300.0

#: SERP features that suppress clicks regardless of the snippet. When the
#: caller can supply them, queries carrying one are excluded from the gap.
SUPPRESSING_FEATURES = (
    "ai_overview", "featured_snippet", "local_pack", "shopping",
    "people_also_ask", "video_carousel",
)

_SHOUTING = re.compile(r"\b[A-Z]{4,}\b")
_PUNCT_SPAM = re.compile(r"[!?]{2,}|[!]\s*[!]")

ProblemKind = Literal[
    "title_truncated", "title_short", "title_boilerplate", "head_term_missing",
    "title_duplicate", "description_missing", "description_truncated",
]

#: Ordered by how confident we are that fixing it moves CTR.
_PROBLEM_WEIGHT: dict[str, int] = {
    "head_term_missing":     5,
    "title_duplicate":       4,
    "title_boilerplate":     4,
    "description_missing":   3,
    "title_truncated":       3,
    "description_truncated": 2,
    "title_short":           1,
}


# ═══════════════════════════════════════════════════════
#  What the site currently shows
# ═══════════════════════════════════════════════════════

@dataclass
class PageSnippet:
    """The title and description as a searcher would see them.

    `stored_title` is what the SEO plugin holds; `title` is what renders once
    the plugin's template has been applied. Measuring the first is measuring a
    string that never reaches a search result.
    """

    url: str
    title: str
    description: str = ""
    stored_title: str = ""
    template: str = ""
    post_id: int | None = None

    @classmethod
    def from_meta(
        cls, url: str, stored_title: str, description: str, *,
        template: str = "", site_name: str = "", separator: str = "-",
        post_id: int | None = None,
    ) -> "PageSnippet":
        rendered = pixels.apply_template(
            stored_title, template, site_name=site_name, separator=separator
        )
        return cls(
            url=url, title=rendered, description=description.strip(),
            stored_title=stored_title.strip(), template=template, post_id=post_id,
        )

    def terms(self) -> set[str]:
        return {w for w in _WORD.findall(self.title.lower()) if w not in _STOPWORDS}


# ═══════════════════════════════════════════════════════
#  What the page earns
# ═══════════════════════════════════════════════════════

@dataclass
class PagePerformance:
    """Every query one URL ranks for, rolled up."""

    url: str
    queries: list[QueryRow] = field(default_factory=list)

    @property
    def clicks(self) -> int:
        return sum(q.clicks for q in self.queries)

    @property
    def impressions(self) -> int:
        return sum(q.impressions for q in self.queries)

    @property
    def ctr(self) -> float:
        return self.clicks / self.impressions if self.impressions else 0.0

    @property
    def position(self) -> float:
        """Impression-weighted, because an unweighted mean of thirty tail
        queries says nothing about where the page actually appears."""
        total = self.impressions
        if not total:
            return 0.0
        return sum(q.position * q.impressions for q in self.queries) / total

    @property
    def head_query(self) -> QueryRow | None:
        return max(self.queries, key=lambda q: q.impressions, default=None)

    def visible(self, max_position: float = MAX_POSITION) -> "PagePerformance":
        """Only the queries whose results a searcher actually sees.

        A page ranking 3 for its main term will also collect a long tail of
        queries at position 40 that nobody scrolls to. Averaged in, that tail
        both pushes the page's position out of scope and depresses its CTR —
        so a page that is doing fine on screen reads as a snippet failure.
        The tail is dropped rather than weighted down, because a snippet on
        page two is not a snippet anybody rejected.
        """
        return PagePerformance(
            url=self.url,
            queries=[q for q in self.queries if 0 < q.position <= max_position],
        )

    def protected_terms(self) -> set[str]:
        """Words the page is already earning clicks on.

        A rewrite that drops one of these is trading a measured click for a
        hoped-for one. That trade is refused, not flagged.
        """
        terms: set[str] = set()
        for query in self.queries:
            if query.clicks > 0:
                terms |= query.terms()
        return terms


def aggregate(rows: Iterable[QueryRow]) -> dict[str, PagePerformance]:
    """Group Search Console rows by the URL that ranked."""
    pages: dict[str, PagePerformance] = {}
    for row in rows:
        pages.setdefault(row.url, PagePerformance(url=row.url)).queries.append(row)
    return pages


# ═══════════════════════════════════════════════════════
#  Problems
# ═══════════════════════════════════════════════════════

@dataclass
class Problem:
    kind: ProblemKind
    detail: str                              # one Hebrew sentence
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def weight(self) -> int:
        return _PROBLEM_WEIGHT[self.kind]


def _title_problems(snippet: PageSnippet, site_name: str) -> list[Problem]:
    found: list[Problem] = []
    title = snippet.title

    if not title.strip():
        return [Problem(
            "title_boilerplate",
            "לדף אין טייטל משלו — גוגל מציג את מה שהתבנית או ה-H1 נותנים",
            {"stored_title": snippet.stored_title, "template": snippet.template},
        )]

    measured = pixels.measure(title, "title")
    if measured.truncated:
        found.append(Problem(
            "title_truncated",
            f"הטייטל {measured.describe()}",
            {"pixels": round(measured.pixels), "limit": measured.limit,
             "visible": measured.visible, "lost": measured.lost},
        ))
    elif measured.pixels < pixels.SHORT_TITLE_PX:
        found.append(Problem(
            "title_short",
            f"הטייטל תופס {measured.pixels:.0f}px מתוך {measured.limit:.0f} — "
            f"יש מקום לעוד כ-{pixels.budget_chars('title', title)} תווים שאף אחד לא משתמש בהם",
            {"pixels": round(measured.pixels), "limit": measured.limit},
        ))

    # A title that is only the brand describes every page equally, which is to
    # say it describes none of them.
    bare = re.sub(r"[|\-–—·»]", " ", title).strip().lower()
    if site_name and bare in {site_name.strip().lower(),
                              f"home {site_name.strip().lower()}"}:
        found.append(Problem(
            "title_boilerplate",
            f"הטייטל הוא שם העסק בלבד — {title!r} לא אומר על מה הדף",
            {"title": title},
        ))
    return found


def _description_problems(snippet: PageSnippet) -> list[Problem]:
    description = snippet.description.strip()
    if not description:
        return [Problem(
            "description_missing",
            "אין מטא דיסקריפשן — גוגל מרכיב אחד מהטקסט בדף, ולרוב הוא חותך "
            "באמצע משפט",
            {"url": snippet.url},
        )]

    measured = pixels.measure(description, "description")
    if measured.truncated:
        return [Problem(
            "description_truncated",
            f"התיאור {measured.describe()}",
            {"pixels": round(measured.pixels), "lost": measured.lost},
        )]
    if measured.pixels < MIN_DESCRIPTION_PX:
        return [Problem(
            "description_missing",
            f"התיאור באורך {measured.pixels:.0f}px בלבד — קצר מכדי לומר משהו, "
            "וגוגל יחליף אותו בטקסט מהדף",
            {"pixels": round(measured.pixels)},
        )]
    return []


def _head_term_problem(snippet: PageSnippet, page: PagePerformance) -> Problem | None:
    """The words the page is found by, missing from the line it is found as.

    Google bolds the searched words in a result. A title that contains none of
    them is competing without the one visual cue the format gives away free.
    """
    head = page.head_query
    if head is None or head.impressions < MIN_IMPRESSIONS:
        return None

    words = snippet.terms()
    missing = sorted(term for term in head.terms() if not covers(term, words))
    if not missing or len(missing) < len(head.terms()):
        return None                     # partial coverage still gets the bolding

    return Problem(
        "head_term_missing",
        f"השאילתה המובילה “{head.query}” מביאה {head.impressions:,} "
        f"הופעות, ואף מילה ממנה לא מופיעה בטייטל",
        {"query": head.query, "impressions": head.impressions,
         "missing_terms": missing},
    )


#: How much of a site has to end its titles the same way before that tail is
#: the brand rather than a coincidence.
_BRAND_SHARE = 0.5

_TITLE_SPLIT = re.compile(r"\s[|\-–—·»:]\s")


def infer_site_name(pages: Iterable[PageSnippet]) -> str:
    """The brand the theme appends, learned from how many titles carry it.

    Asking for the site name as an argument invites a typo that quietly turns
    the boilerplate check off. The titles already answer it: when half the
    pages end with the same words after a separator, those words are the
    brand — and a page whose whole title is only those words is a page with
    no title of its own.
    """
    tails: Counter[str] = Counter()
    considered = 0
    for page in pages:
        if not page.title.strip():
            continue
        considered += 1
        parts = _TITLE_SPLIT.split(page.title)
        if len(parts) > 1 and parts[-1].strip():
            tails[parts[-1].strip()] += 1

    if not tails or considered < 2:
        return ""
    name, count = tails.most_common(1)[0]
    return name if count >= max(2, considered * _BRAND_SHARE) else ""


def find_duplicates(snippets: Iterable[PageSnippet],
                    pages: dict[str, PagePerformance]) -> dict[str, list[str]]:
    """Rendered titles shared by more than one URL that is really ranking."""
    seen: dict[str, list[str]] = {}
    for snippet in snippets:
        title = snippet.title.strip().lower()
        performance = pages.get(snippet.url)
        if not title or not performance:
            continue
        if performance.impressions < DUPLICATE_MIN_IMPRESSIONS:
            continue
        seen.setdefault(title, []).append(snippet.url)
    return {title: urls for title, urls in seen.items() if len(urls) > 1}


# ═══════════════════════════════════════════════════════
#  The audit
# ═══════════════════════════════════════════════════════

@dataclass
class Audit:
    """One page: what it earns, what it should earn, and what is in the way."""

    url: str
    snippet: PageSnippet
    page: PagePerformance
    expected_ctr: float
    problems: list[Problem] = field(default_factory=list)
    suppressed_queries: list[str] = field(default_factory=list)

    @property
    def actual_ctr(self) -> float:
        return self.page.ctr

    @property
    def has_structural_cause(self) -> bool:
        return bool(self.problems)

    @property
    def recovery(self) -> float:
        return STRUCTURAL_RECOVERY if self.has_structural_cause else BLIND_RECOVERY

    @property
    def gap_clicks(self) -> float:
        """Clicks between what the position earns and what it should."""
        return max(0.0, self.page.impressions * (self.expected_ctr - self.actual_ctr))

    @property
    def claimable_clicks(self) -> float:
        return self.gap_clicks * self.recovery

    @property
    def rank(self) -> tuple[int, float]:
        """Order for a work queue: strongest evidence first, then size."""
        strongest = max((p.weight for p in self.problems), default=0)
        return (strongest, self.claimable_clicks)

    def summary(self) -> str:
        return (
            f"מיקום {self.page.position:.1f}, {self.page.impressions:,} הופעות, "
            f"CTR {self.actual_ctr:.1%} מול {self.expected_ctr:.1%} צפויים"
        )


def audit_page(
    snippet: PageSnippet,
    page: PagePerformance,
    curve: CTRCurve,
    *,
    site_name: str = "",
    duplicates: dict[str, list[str]] | None = None,
    serp_features: dict[str, list[str]] | None = None,
) -> Audit | None:
    """Audit one page, or return None when it is not this skill's problem.

    Three pages are handed back: one with too few first-page impressions to
    read, one ranked too low for the snippet to be the binding constraint,
    and one already earning at or above par.
    """
    page = page.visible()
    if page.impressions < MIN_IMPRESSIONS:
        return None

    # A query sitting under an AI Overview earns less than par no matter what
    # the title says, so it does not count toward a gap we claim to close.
    suppressed: list[str] = []
    counted = page
    if serp_features:
        kept: list[QueryRow] = []
        for query in page.queries:
            features = serp_features.get(query.query) or []
            if any(f in SUPPRESSING_FEATURES for f in features):
                suppressed.append(query.query)
            else:
                kept.append(query)
        if not kept:
            return None
        counted = PagePerformance(url=page.url, queries=kept)
        if counted.impressions < MIN_IMPRESSIONS:
            return None

    expected = curve.expected(counted.position)
    if counted.ctr >= expected * UNDERPERFORMANCE_RATIO:
        return None

    problems = _title_problems(snippet, site_name)
    problems += _description_problems(snippet)
    head = _head_term_problem(snippet, counted)
    if head:
        problems.append(head)

    shared = (duplicates or {}).get(snippet.title.strip().lower())
    if shared:
        others = [u for u in shared if u != snippet.url]
        problems.append(Problem(
            "title_duplicate",
            f"אותו טייטל בדיוק מופיע על עוד {len(others)} דפים — "
            "גוגל לא יכול להבדיל ביניהם בתוצאות",
            {"title": snippet.title, "also_on": others},
        ))

    problems.sort(key=lambda p: p.weight, reverse=True)
    return Audit(
        url=snippet.url, snippet=snippet, page=counted,
        expected_ctr=expected, problems=problems, suppressed_queries=suppressed,
    )


def analyse(
    rows: list[QueryRow],
    snippets: list[PageSnippet],
    curve: CTRCurve,
    *,
    site_name: str | None = None,
    serp_features: dict[str, list[str]] | None = None,
) -> list[Audit]:
    """Every page worth a snippet rewrite, strongest evidence first."""
    if site_name is None:
        site_name = infer_site_name(snippets)
    pages = aggregate(rows)
    duplicates = find_duplicates(snippets, pages)

    audits: list[Audit] = []
    for snippet in snippets:
        page = pages.get(snippet.url)
        if page is None:
            continue
        audit = audit_page(
            snippet, page, curve, site_name=site_name,
            duplicates=duplicates, serp_features=serp_features,
        )
        if audit is not None:
            audits.append(audit)

    return sorted(audits, key=lambda a: a.rank, reverse=True)


# ═══════════════════════════════════════════════════════
#  The guard — a title is a ranking signal
# ═══════════════════════════════════════════════════════

#: A candidate that shouts or stacks punctuation is not blocked — it is a
#: judgement call and Lior makes it — but it is said out loud.
_ADVISORY_CHECKS = (
    (lambda t: bool(_SHOUTING.search(t)),
     "הטייטל מכיל מילים באותיות גדולות בלבד — גוגל נוטה לשכתב טייטלים צועקים"),
    (lambda t: bool(_PUNCT_SPAM.search(t)),
     "סימני קריאה כפולים בטייטל נקראים כספאם ומעלים את הסיכוי לשכתוב"),
)


def validate(
    title: str,
    description: str,
    audit: Audit,
    *,
    existing_titles: dict[str, str] | None = None,
) -> Result:
    """Refuse a candidate title that would cost more than it earns.

    The blocking rule that matters: the page is earning clicks on certain
    words today, and some of those words are in the title doing the work. A
    rewrite that removes one of them is not a snippet change, it is a ranking
    experiment — and this skill does not run ranking experiments on a page
    that is already on the first results page.
    """
    blocking: list[str] = []
    warnings: list[str] = []

    candidate = title.strip()
    if not candidate:
        return Result.failure("title_empty", "לא הוצע טייטל", blocking=["טייטל ריק"])

    measured = pixels.measure(candidate, "title")
    if measured.truncated:
        blocking.append(
            f"הטייטל המוצע נחתך: {measured.pixels:.0f}px מתוך {measured.limit:.0f} "
            f"— “{measured.lost}” לא ייראה"
        )
    elif measured.pixels < pixels.SHORT_TITLE_PX:
        warnings.append(
            f"הטייטל המוצע קצר — {measured.pixels:.0f}px מתוך {measured.limit:.0f}"
        )

    # Words that are both earning clicks and currently on display.
    current = audit.snippet.terms()
    candidate_words = {w for w in _WORD.findall(candidate.lower()) if w not in _STOPWORDS}
    working = {t for t in audit.page.protected_terms() if covers(t, current)}
    dropped = sorted(t for t in working if not covers(t, candidate_words))
    if dropped:
        blocking.append(
            f"הטייטל המוצע מוריד מילים שהדף כבר מקבל עליהן קליקים: "
            f"{', '.join(dropped)} — זה שינוי דירוג, לא שינוי מראה"
        )

    head = audit.page.head_query
    if head and all(not covers(t, candidate_words) for t in head.terms()):
        blocking.append(
            f"הטייטל המוצע לא מכיל אף מילה מ-“{head.query}”, "
            f"שמביאה {head.impressions:,} הופעות"
        )

    for url, other in (existing_titles or {}).items():
        if url != audit.url and other.strip().lower() == candidate.lower():
            blocking.append(f"הטייטל המוצע כבר קיים על {url}")
            break

    for predicate, message in _ADVISORY_CHECKS:
        if predicate(candidate):
            warnings.append(message)

    body = description.strip()
    if not body:
        warnings.append("לא הוצע תיאור — גוגל ימשיך להרכיב אחד מהטקסט בדף")
    else:
        desc = pixels.measure(body, "description")
        if desc.truncated:
            warnings.append(
                f"התיאור המוצע נחתך אחרי “{desc.visible.strip()[-40:]}”"
            )
        elif desc.pixels < MIN_DESCRIPTION_PX:
            warnings.append(f"התיאור המוצע קצר — {desc.pixels:.0f}px")

    if blocking:
        return Result.failure(
            "title_rejected",
            f"{len(blocking)} סיבות חוסמות לא לכתוב את הטייטל הזה",
            blocking=blocking, warnings=warnings,
        )
    return Result.success(
        "title_ok",
        f"הטייטל עובר את הבדיקות ({measured.pixels:.0f}px)"
        + (f", {len(warnings)} הערות" if warnings else ""),
        warnings=warnings, pixels=round(measured.pixels),
        preview=pixels.preview(candidate, "title"),
    )


# ═══════════════════════════════════════════════════════
#  Verification — separating the title's effect from the position's
# ═══════════════════════════════════════════════════════

#: An effect smaller than two standard errors of the measured rate is not
#: distinguishable from the ordinary week-to-week movement of the same page.
_SIGMA = 2.0

#: And when even a swing this large would be invisible in the sample, the
#: honest answer is that we cannot tell yet, not that nothing happened.
_DETECTABLE_FRACTION = 0.30

#: A title carries ranking weight. A page that fell this far after a rewrite
#: is reported as a ranking change regardless of what happened to CTR.
POSITION_REGRESSION = 1.0


@dataclass
class Attribution:
    """What the rewrite did, with the position's contribution taken out.

    CTR rose after a rewrite is not evidence that the rewrite worked: a page
    that also moved from 6 to 3 would have earned more with the old title.
    What is left after subtracting the movement the curve already explains is
    the part the new snippet can claim.
    """

    url: str
    day: int
    before: dict[str, Any]
    after: dict[str, Any]
    expected_shift: float          # CTR change the position change alone explains
    observed_shift: float
    title_effect: float
    margin: float                  # the smallest effect this sample could show
    verdict: Literal["improved", "worse", "no_effect", "inconclusive"]
    position_change: float

    @property
    def clicks_effect(self) -> float:
        return self.title_effect * self.after.get("impressions", 0)

    def describe(self) -> str:
        if self.verdict == "inconclusive":
            return (
                f"{self.after['impressions']:,} הופעות ב-{self.day} יום — "
                f"מדגם קטן מכדי להבחין בשינוי של {_DETECTABLE_FRACTION:.0%}"
            )
        direction = {"improved": "עלה", "worse": "ירד", "no_effect": "לא זז"}
        return (
            f"CTR {direction[self.verdict]} ב-{abs(self.title_effect):.2%} "
            f"אחרי נטרול תזוזת המיקום ({self.position_change:+.1f} מקומות), "
            f"כ-{self.clicks_effect:+.0f} קליקים"
        )


def attribute(
    before: PagePerformance, after: PagePerformance, curve: CTRCurve, day: int = 28
) -> Attribution:
    """Measure the rewrite, holding position constant."""
    expected_shift = curve.expected(after.position) - curve.expected(before.position)
    observed_shift = after.ctr - before.ctr
    effect = observed_shift - expected_shift

    rate, sample = after.ctr, after.impressions
    margin = (
        _SIGMA * math.sqrt(max(rate * (1 - rate), 1e-9) / sample) if sample else 1.0
    )

    par = curve.expected(after.position) or 0.01
    if sample < MIN_IMPRESSIONS or margin > par * _DETECTABLE_FRACTION:
        verdict: str = "inconclusive"
    elif effect > margin:
        verdict = "improved"
    elif effect < -margin:
        verdict = "worse"
    else:
        verdict = "no_effect"

    def snapshot(p: PagePerformance) -> dict[str, Any]:
        return {"clicks": p.clicks, "impressions": p.impressions,
                "position": round(p.position, 1), "ctr": round(p.ctr, 4)}

    return Attribution(
        url=after.url, day=day, before=snapshot(before), after=snapshot(after),
        expected_shift=expected_shift, observed_shift=observed_shift,
        title_effect=effect, margin=margin,
        verdict=verdict,                                   # type: ignore[arg-type]
        position_change=after.position - before.position,
    )


def ranking_regressed(attribution: Attribution) -> bool:
    """Whether the page fell far enough to suspect the title cost it ranking."""
    return attribution.position_change > POSITION_REGRESSION


# ═══════════════════════════════════════════════════════
#  Findings
# ═══════════════════════════════════════════════════════

_SEVERITY_FLOOR = ((100, "high"), (30, "medium"))


def _severity(claimable: float) -> str:
    for floor, level in _SEVERITY_FLOOR:
        if claimable >= floor:
            return level
    return "low"


def _confidence(audit: Audit, curve: CTRCurve) -> tuple[str, str]:
    if not audit.has_structural_cause:
        return "low", (
            "ה-CTR נמוך אבל לא נמצאה סיבה מבנית בטייטל או בתיאור — "
            "ייתכן AI Overview, מודעות, או כוונת חיפוש אחרת. שווה ניסוי, לא הבטחה"
        )
    if curve.source != "site":
        return "low", (
            "נמצאה סיבה מבנית, אבל האומדן נשען על עקומת CTR ממוצעת "
            "מהתעשייה ולא על נתוני האתר"
        )
    strongest = audit.problems[0]
    return "medium", (
        f"{strongest.detail}. {GOOGLE_REWRITE_NOTE}"
    )


def to_finding(
    audit: Audit, client: str, curve: CTRCurve, conversion_rate: float | None = None
) -> Finding:
    """One page's snippet problem, with the numbers that justify touching it."""
    confidence, reason = _confidence(audit, curve)
    claimable = round(audit.claimable_clicks, 1)
    head = audit.page.head_query

    basis = (
        f"{audit.page.impressions:,} הופעות במיקום {audit.page.position:.1f} "
        f"ב-CTR {audit.actual_ctr:.1%} במקום {audit.expected_ctr:.1%}. "
        f"{audit.gap_clicks:.0f} קליקים בפער, מתוכם "
        f"{audit.recovery:.0%} נחשבים ברי-השגה "
        + ("כי נמצאה סיבה מבנית" if audit.has_structural_cause
           else "בלבד, כי לא נמצאה סיבה מבנית")
    )

    return Finding(
        skill="ctr-titles",
        client=client,
        type="low_ctr_snippet",
        severity=_severity(claimable),
        source="gsc_wizard",
        url=audit.url,
        query=head.query if head else None,
        impact_clicks=claimable,
        impact_conversions=(
            round(claimable * conversion_rate, 2) if conversion_rate else None
        ),
        impact_basis=basis,
        confidence=confidence,
        confidence_reason=reason,
        effort="s",
        evidence={
            "impressions": audit.page.impressions,
            "clicks": audit.page.clicks,
            "position": round(audit.page.position, 1),
            "ctr": round(audit.actual_ctr, 4),
            "expected_ctr": round(audit.expected_ctr, 4),
            "curve_source": curve.source,
            "title": audit.snippet.title,
            "title_pixels": round(pixels.measure(audit.snippet.title, "title").pixels),
            "description_pixels": round(
                pixels.measure(audit.snippet.description, "description").pixels),
            "problems": [{"kind": p.kind, "detail": p.detail, **p.evidence}
                         for p in audit.problems],
            "suppressed_queries": audit.suppressed_queries,
        },
        action={
            "kind": "rewrite_snippet",
            "url": audit.url,
            "post_id": audit.snippet.post_id,
            "instruction": (
                "שכתוב טייטל ותיאור. הטייטל חייב לשמור את המילים שהדף כבר "
                "מקבל עליהן קליקים ולהיכנס ברוחב המוצג"
            ),
            "must_keep": sorted(
                t for t in audit.page.protected_terms()
                if covers(t, audit.snippet.terms())
            ),
            "current_title": audit.snippet.title,
            "current_description": audit.snippet.description,
            "title_budget_px": pixels.LIMITS[("title", "desktop")],
        },
        baseline={
            "position": round(audit.page.position, 1),
            "clicks": audit.page.clicks,
            "impressions": audit.page.impressions,
            "ctr": round(audit.actual_ctr, 4),
            "title": audit.snippet.title,
            "description": audit.snippet.description,
        },
    )


# ═══════════════════════════════════════════════════════
#  Reading the live page — what Google was actually handed
# ═══════════════════════════════════════════════════════

_TITLE_TAG = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_DESC_TAG = re.compile(
    r"<meta[^>]+name\s*=\s*[\"']description[\"'][^>]*>", re.I)
_CONTENT_ATTR = re.compile(r"content\s*=\s*[\"'](.*?)[\"']", re.I | re.S)


def _tidy(text: str) -> str:
    return re.sub(r"\s+", " ", unescape(text)).strip()


def from_html(url: str, html: str, *, post_id: int | None = None,
              stored_title: str = "") -> PageSnippet:
    """The snippet as the live page serves it.

    Taken from the rendered HTML rather than from post meta, because the
    plugin's title template, the theme, and any number of filters sit between
    the stored value and the one a crawler receives. The rendered page is the
    only version that is not a reconstruction.
    """
    title_match = _TITLE_TAG.search(html)
    desc_match = _DESC_TAG.search(html)
    description = ""
    if desc_match:
        content = _CONTENT_ATTR.search(desc_match.group(0))
        description = _tidy(content.group(1)) if content else ""

    return PageSnippet(
        url=url,
        title=_tidy(title_match.group(1)) if title_match else "",
        description=description,
        stored_title=stored_title.strip(),
        post_id=post_id,
    )


def confirm_rendered(html: str, title: str, description: str = "") -> Result:
    """Check that the site is serving the snippet we wrote.

    A meta write can return 200 and change nothing at all — the plugin was
    never the one rendering the title, a cache is still serving the old page,
    or the key belonged to a plugin that is not installed. The body text is
    untouched by a snippet rewrite, so the ordinary post-publish checks cannot
    see any of that. This is the one check that can.
    """
    live = from_html("", html)
    if not live.title:
        return Result.failure(
            "no_title", "לדף החי אין תגית title בכלל", recoverable=True)

    if title.strip() not in live.title:
        return Result.failure(
            "title_not_live",
            f"הטייטל שנכתב לא מופיע בדף החי. מוצג: “{live.title}” — "
            "ייתכן cache, תוסף אחר, או תבנית שדורסת",
            recoverable=True, rendered=live.title,
        )

    if description.strip() and description.strip() not in live.description:
        return Result.success(
            "title_live_description_not",
            f"הטייטל עלה. התיאור לא מופיע בדף החי — מוצג: "
            f"“{live.description[:80]}”",
            rendered_title=live.title, rendered_description=live.description,
            description_live=False,
        )

    return Result.success(
        "snippet_live",
        f"הדף מגיש את הטייטל שנכתב ({pixels.measure(live.title, 'title').pixels:.0f}px)",
        rendered_title=live.title, rendered_description=live.description,
        description_live=True,
    )
