"""
Whether the conversions are being counted at all.
=================================================

Every other skill in this toolkit reports `impact_conversions` as None until
a client has conversion data, and a client with conversion data that is wrong
is worse off than one with none: the numbers look authoritative and rank the
wrong work first.

This module audits the measurement itself. It does so from the rendered HTML
of a crawl, which sets a hard limit that shapes everything below.

**Most of what matters happens inside Google Tag Manager, and the HTML cannot
see into it.** A page that loads GTM might fire a GA4 event on every form
submission, or might fire nothing at all, and the served markup is identical
in both cases. Pretending otherwise would be the exact failure this module
exists to catch, so a GTM container produces an explicit "cannot be verified
from here, check these four things in the container" rather than a pass.

What *can* be established from the page:

    the same GA4 measurement ID loaded twice  — every number doubled
    two different measurement IDs             — two properties disagreeing
    no analytics at all on some pages         — a hole in the reporting
    a consent banner                          — conversions lost before consent
    tel:, mailto: and WhatsApp links          — the ones people forget to count

And one check that needs no markup at all: Search Console clicks against GA4
sessions for the same landing page. They measure different things and never
match exactly, but a page with 900 clicks and 40 sessions is not a discrepancy,
it is a page where the tag is not firing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from ..schema import Finding
from .crawl import Crawl, normalise
from .queries import QueryRow

# ═══════════════════════════════════════════════════════
#  Fingerprints
# ═══════════════════════════════════════════════════════

#: A correct GA4 installation names its measurement ID twice — once in the
#: script that loads gtag.js and once in the `config` call. Counting bare
#: mentions would therefore report every properly tagged site on earth as
#: double counting, so the two roles are matched separately and a duplicate
#: means the same ID appearing twice *in the same role*.
_GA4_LOADER = re.compile(r"gtag/js\?id=(G-[A-Z0-9]{6,12})", re.I)
_GA4_CONFIG = re.compile(
    r"""gtag\s*\(\s*["']config["']\s*,\s*["'](G-[A-Z0-9]{6,12})["']""", re.I)
_GA4_MENTION = re.compile(r"\bG-[A-Z0-9]{6,12}\b")
_GTM_ID = re.compile(r"\bGTM-[A-Z0-9]{4,10}\b")
_UA_ID = re.compile(r"\bUA-\d{4,10}-\d{1,4}\b")

#: Consent platforms common enough to name. Their presence is not a problem —
#: the problem is what a page does with a visitor who has not answered yet.
_CONSENT = re.compile(
    r"cookiebot|onetrust|complianz|cookieyes|termly|iubenda|borlabs|"
    r"cookie-?law-?info|usercentrics|klaro|didomi", re.I)

#: Anything a visitor can click that converts without loading a page.
_CONTACT_LINKS = {
    "phone_click": re.compile(r"""href\s*=\s*["']tel:([^"']+)["']""", re.I),
    "email_click": re.compile(r"""href\s*=\s*["']mailto:([^"']+)["']""", re.I),
    "whatsapp_click": re.compile(
        r"""href\s*=\s*["']https?://(?:api\.)?w(?:ho)?a(?:tsapp)?\.?me/[^"']+["']""",
        re.I),
}

_FORM = re.compile(r"<form\b[^>]*>", re.I)
#: A search box is a form and is not a lead.
_SEARCH_FORM = re.compile(r"""role\s*=\s*["']search["']|class\s*=\s*["'][^"']*search""",
                          re.I)

#: URLs that usually mean "the form went through", and are therefore the one
#: conversion a pageview can count on its own.
_THANK_YOU = re.compile(r"/(thank[-_]?you|thanks|success|confirmation|toda)\b", re.I)

#: Below this share of a site's pages carrying analytics, the hole is the
#: finding rather than the exception.
FULL_COVERAGE = 0.95

#: GA4 sessions and GSC clicks measure different things and never match. These
#: are the bounds outside which the difference stops being attribution.
SESSION_FLOOR = 0.5
SESSION_CEILING = 2.0

#: And below this many clicks the ratio is noise whatever it says.
MIN_CLICKS_TO_COMPARE = 100


# ═══════════════════════════════════════════════════════
#  What one page carries
# ═══════════════════════════════════════════════════════

@dataclass
class PageTags:
    url: str
    loaders: list[str] = field(default_factory=list)      # gtag.js script tags
    configs: list[str] = field(default_factory=list)      # gtag('config', …)
    mentions: list[str] = field(default_factory=list)     # the ID anywhere at all
    gtm_ids: list[str] = field(default_factory=list)
    ua_ids: list[str] = field(default_factory=list)
    consent_platform: str = ""
    contact_links: dict[str, int] = field(default_factory=dict)
    forms: int = 0
    is_thank_you: bool = False

    @property
    def has_ga4_config(self) -> bool:
        return bool(self.configs)

    @property
    def has_analytics(self) -> bool:
        return bool(self.mentions or self.gtm_ids)

    @property
    def duplicated_ga4(self) -> list[str]:
        """IDs loaded twice or configured twice — the shape that doubles data.

        One mention in the loader and one in the config is a correct install,
        not a duplicate, which is why each role is counted on its own.
        """
        return sorted(
            {i for i in self.loaders if self.loaders.count(i) > 1}
            | {i for i in self.configs if self.configs.count(i) > 1}
        )

    @property
    def distinct_ga4(self) -> list[str]:
        return sorted(set(self.mentions))

    @property
    def gtm_only(self) -> bool:
        """GTM is loaded and no GA4 configuration is visible in the markup."""
        return bool(self.gtm_ids) and not self.has_ga4_config


def read_page(url: str, html: str) -> PageTags:
    """Everything the served markup says about how this page is measured."""
    contact = {}
    for name, pattern in _CONTACT_LINKS.items():
        found = len(pattern.findall(html))
        if found:
            contact[name] = found

    forms = [m for m in _FORM.findall(html) if not _SEARCH_FORM.search(m)]
    consent = _CONSENT.search(html)

    return PageTags(
        url=url,
        loaders=[i.upper() for i in _GA4_LOADER.findall(html)],
        configs=[i.upper() for i in _GA4_CONFIG.findall(html)],
        mentions=_GA4_MENTION.findall(html),
        gtm_ids=sorted(set(_GTM_ID.findall(html))),
        ua_ids=sorted(set(_UA_ID.findall(html))),
        consent_platform=consent.group(0).lower() if consent else "",
        contact_links=contact,
        forms=len(forms),
        is_thank_you=bool(_THANK_YOU.search(url)),
    )


# ═══════════════════════════════════════════════════════
#  Problems
# ═══════════════════════════════════════════════════════

@dataclass
class Issue:
    kind: str
    detail: str
    urls: list[str] = field(default_factory=list)
    severity: str = "medium"
    verifiable: bool = True          # False = only the GTM container can answer
    evidence: dict[str, Any] = field(default_factory=dict)


#: What has to be checked inside the container, since the page cannot show it.
GTM_CHECKLIST = (
    "האם יש תג GA4 Configuration שנורה בכל דף",
    "האם אירוע ההמרה נורה על **הצלחת** השליחה ולא על לחיצה על הכפתור",
    "האם יש טריגר כפול שיורה את אותו אירוע פעמיים",
    "האם Consent Mode חוסם את האירוע עד שהגולש מאשר",
)


def audit_pages(pages: Iterable[PageTags]) -> list[Issue]:
    """Everything the markup can settle, and the one thing it cannot."""
    pages = [p for p in pages]
    if not pages:
        return []

    issues: list[Issue] = []

    with_analytics = [p for p in pages if p.has_analytics]
    missing = [p.url for p in pages if not p.has_analytics]
    if with_analytics and len(with_analytics) / len(pages) < FULL_COVERAGE:
        issues.append(Issue(
            "analytics_missing",
            f"{len(missing)} מתוך {len(pages)} דפים לא טוענים GA4 או GTM בכלל — "
            "כל מה שקורה בהם לא נספר",
            urls=missing[:20], severity="high",
            evidence={"missing": len(missing), "crawled": len(pages)},
        ))

    duplicated = [p for p in pages if p.duplicated_ga4]
    if duplicated:
        issues.append(Issue(
            "ga4_duplicated",
            f"אותו מזהה GA4 נטען יותר מפעם אחת ב-{len(duplicated)} דפים — "
            "כל צפייה וכל אירוע נספרים כפול",
            urls=[p.url for p in duplicated][:20], severity="high",
            evidence={"ids": duplicated[0].duplicated_ga4},
        ))

    every_id = sorted({i for p in pages for i in p.distinct_ga4})
    if len(every_id) > 1:
        issues.append(Issue(
            "ga4_multiple_properties",
            f"{len(every_id)} מזהי GA4 שונים באתר: {', '.join(every_id)} — "
            "שני דוחות שלא יסכימו זה עם זה",
            urls=[p.url for p in pages if len(p.distinct_ga4) > 1][:20],
            severity="high", evidence={"ids": every_id},
        ))

    legacy = sorted({i for p in pages for i in p.ua_ids})
    if legacy:
        issues.append(Issue(
            "universal_analytics",
            f"עדיין נטען Universal Analytics ({', '.join(legacy)}) — "
            "הוא הפסיק לאסוף נתונים, והנוכחות שלו מסתירה את זה",
            urls=[p.url for p in pages if p.ua_ids][:20],
            severity="medium", evidence={"ids": legacy},
        ))

    gtm_only = [p for p in pages if p.gtm_only]
    if gtm_only:
        issues.append(Issue(
            "gtm_not_inspectable",
            f"{len(gtm_only)} דפים טוענים GTM בלי הגדרת GA4 גלויה ב-HTML. "
            "ייתכן שהכול תקין בתוך המכולה, וייתכן שלא — מהדף אי אפשר לדעת",
            urls=[p.url for p in gtm_only][:20], severity="medium",
            verifiable=False,
            evidence={"containers": sorted({i for p in gtm_only
                                            for i in p.gtm_ids}),
                      "checklist": list(GTM_CHECKLIST)},
        ))

    consent = [p for p in pages if p.consent_platform]
    if consent:
        platform = consent[0].consent_platform
        issues.append(Issue(
            "consent_gate",
            f"באתר יש באנר הסכמה ({platform}). המרה שקורית לפני שהגולש מאשר "
            "לא תיספר, וזה נראה בדוח כמו ירידה בהמרות",
            urls=[consent[0].url], severity="medium", verifiable=False,
            evidence={"platform": platform, "pages": len(consent)},
        ))

    contact_kinds: dict[str, int] = {}
    for page in pages:
        for kind, found in page.contact_links.items():
            contact_kinds[kind] = contact_kinds.get(kind, 0) + found
    if contact_kinds:
        listed = ", ".join(f"{k} ({v})" for k, v in sorted(contact_kinds.items()))
        issues.append(Issue(
            "contact_clicks_unverified",
            f"קישורי יצירת קשר שהמרה בהם לא טוענת דף: {listed}. "
            "האם הם נספרים אפשר לדעת רק מ-GA4 או מהמכולה",
            urls=[p.url for p in pages if p.contact_links][:20],
            severity="high", verifiable=False,
            evidence={"by_kind": contact_kinds},
        ))

    forms = [p for p in pages if p.forms]
    thank_you = [p for p in pages if p.is_thank_you]
    if forms and not thank_you:
        issues.append(Issue(
            "no_success_page",
            f"{len(forms)} דפים עם טופס, ואף דף תודה בסריקה — "
            "אם אין דף תודה, ספירת ההמרות תלויה כולה באירוע מהמכולה",
            urls=[p.url for p in forms][:20], severity="medium",
            verifiable=False, evidence={"forms": len(forms)},
        ))

    return issues


# ═══════════════════════════════════════════════════════
#  Clicks against sessions — the check that needs no markup
# ═══════════════════════════════════════════════════════

@dataclass
class CoverageGap:
    url: str
    clicks: int
    sessions: int

    @property
    def ratio(self) -> float:
        return self.sessions / self.clicks if self.clicks else 0.0

    @property
    def direction(self) -> str:
        return "under" if self.ratio < SESSION_FLOOR else "over"

    def describe(self) -> str:
        if self.direction == "under":
            return (
                f"{self.clicks:,} קליקים מגוגל מול {self.sessions:,} סשנים "
                f"ב-GA4 ({self.ratio:.0%}) — נראה כמו תג שלא נורה, לא כמו ייחוס"
            )
        return (
            f"{self.sessions:,} סשנים מול {self.clicks:,} קליקים "
            f"({self.ratio:.0%}) — ספירה כפולה או תנועה שממוינת לא נכון"
        )


def compare_sessions(rows: Iterable[QueryRow], sessions: dict[str, int]
                     ) -> list[CoverageGap]:
    """Search Console clicks against GA4 sessions, per landing page.

    The two never match: Search Console counts a click, GA4 counts a session,
    and one person can do several of either. Which is why the bounds are wide
    and the floor on volume is high — this is meant to catch a tag that is not
    firing, not to reconcile two tools that were never going to agree.
    """
    clicks: dict[str, int] = {}
    for row in rows:
        url = normalise(row.url)
        clicks[url] = clicks.get(url, 0) + row.clicks

    normalised = {normalise(url): count for url, count in sessions.items()}

    gaps: list[CoverageGap] = []
    for url, total in clicks.items():
        if total < MIN_CLICKS_TO_COMPARE or url not in normalised:
            continue
        gap = CoverageGap(url=url, clicks=total, sessions=normalised[url])
        if gap.ratio < SESSION_FLOOR or gap.ratio > SESSION_CEILING:
            gaps.append(gap)

    return sorted(gaps, key=lambda g: g.clicks, reverse=True)


# ═══════════════════════════════════════════════════════
#  The report
# ═══════════════════════════════════════════════════════

@dataclass
class Audit:
    pages: list[PageTags] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    gaps: list[CoverageGap] = field(default_factory=list)

    @property
    def measurement_ids(self) -> list[str]:
        return sorted({i for p in self.pages for i in p.distinct_ga4})

    @property
    def containers(self) -> list[str]:
        return sorted({i for p in self.pages for i in p.gtm_ids})

    @property
    def trustworthy(self) -> bool:
        """Whether another skill should be allowed to report conversions.

        A hard "no" on anything that makes every number wrong rather than some
        of them: no tag, a doubled tag, or two properties.
        """
        fatal = {"analytics_missing", "ga4_duplicated", "ga4_multiple_properties"}
        return not any(i.kind in fatal for i in self.issues) and not self.gaps

    def summary(self) -> str:
        return (
            f"{len(self.pages)} דפים · {len(self.measurement_ids)} מזהי GA4 · "
            f"{len(self.issues)} בעיות · "
            f"{len([i for i in self.issues if not i.verifiable])} דורשות גישה ל-GTM"
        )


def analyse(crawl: Crawl, html_by_url: dict[str, str],
            rows: list[QueryRow] | None = None,
            sessions: dict[str, int] | None = None) -> Audit:
    pages = [read_page(url, html) for url, html in html_by_url.items()
             if crawl.pages.get(url) is None or crawl.pages[url].is_ok]
    return Audit(
        pages=pages,
        issues=audit_pages(pages),
        gaps=compare_sessions(rows or [], sessions or {}),
    )


def to_findings(audit: Audit, client: str) -> list[Finding]:
    findings: list[Finding] = []

    for issue in audit.issues:
        findings.append(Finding(
            skill="conversion-tracking-audit", client=client, type=issue.kind,
            severity=issue.severity, source="crawl",
            url=issue.urls[0] if issue.urls else None,
            impact_clicks=0.0,
            impact_basis=(
                issue.detail + ". אין אומדן קליקים — מדידה שבורה היא לא "
                "הזדמנות, היא הסיבה שאי אפשר לדרג את כל השאר"
            ),
            confidence="high" if issue.verifiable else "medium",
            confidence_reason=(
                "נקרא מה-HTML שהאתר מגיש בפועל" if issue.verifiable else
                "מה-HTML אפשר לראות שהמצב אפשרי, לא שהוא קורה — "
                "רק המכולה או GA4 יכולים לענות"
            ),
            effort="s",
            evidence={"urls": issue.urls, "verifiable": issue.verifiable,
                      **issue.evidence},
            action={"kind": "fix_measurement", "instruction": issue.detail,
                    "urls": issue.urls},
            baseline={"kind": issue.kind, "pages": len(issue.urls)},
        ))

    for gap in audit.gaps:
        findings.append(Finding(
            skill="conversion-tracking-audit", client=client,
            type="clicks_without_sessions", severity="high", source="derived",
            url=gap.url, impact_clicks=0.0,
            impact_basis=gap.describe(),
            confidence="medium",
            confidence_reason=(
                "קליקים וסשנים לא אמורים להיות זהים, אבל הפער הזה גדול "
                "מכדי להיות ייחוס"
            ),
            effort="s",
            evidence={"clicks": gap.clicks, "sessions": gap.sessions,
                      "ratio": round(gap.ratio, 2), "direction": gap.direction},
            action={"kind": "check_tag_on_page", "url": gap.url,
                    "instruction": "בדוק שהתג נורה בדף הזה, ושהתנועה לא "
                                   "ממוינת לערוץ אחר"},
            baseline={"clicks": gap.clicks, "sessions": gap.sessions},
        ))

    return findings
