"""
PageSpeed Insights, read honestly.
==================================

PSI returns two different things in one response and they disagree constantly:
`lighthouseResult` is a lab simulation on a throttled connection, and
`loadingExperience` is CrUX — what real Chrome users actually measured over the
trailing 28 days. Only the second one is what Google ranks on, and only the
second one is what a client feels.

So the rule here: **the field decides whether a problem is real, the lab decides
what is causing it.** A page with a poor lab score and good field metrics is not
an emergency, and an opportunity worth 1,800ms in the lab on a metric that is
already good in the field gets discounted rather than topping the list.

Everything is a pure function over the PSI JSON, so the whole module is testable
offline against a saved response.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ..schema import Finding, Result

STRATEGIES = ("mobile", "desktop")

#: Core Web Vitals thresholds, as Google publishes them.
GOOD_THRESHOLD = {"lcp": 2500.0, "inp": 200.0, "cls": 0.10}
POOR_THRESHOLD = {"lcp": 4000.0, "inp": 500.0, "cls": 0.25}

#: An opportunity has to be worth something before it is worth a work order.
MIN_SAVINGS_MS = 150.0

#: Page experience is a real but small ranking input. This is the fraction of a
#: page's current clicks we are willing to attribute to fixing a field failure —
#: deliberately conservative, and only applied when the field data says the page
#: is actually failing.
CWV_CLICK_UPLIFT = 0.03

#: Conversion effect of load time, from Google/Deloitte "Milliseconds Make
#: Millions": roughly 0.8% conversion lift per 100ms of LCP improvement on
#: mobile retail.
#:
#: That study measured *small* deltas. Extrapolating it linearly across two
#: seconds would have this skill claiming a 20% conversion lift and outranking
#: every finding built on measured Search Console data. The cap is what keeps
#: a model from outvoting a measurement.
CONVERSION_UPLIFT_PER_100MS = 0.008
MAX_CONVERSION_UPLIFT = 0.10

_EFFORT_WEIGHT = {"s": 1.0, "m": 2.5, "l": 6.0}

#: Curated, not PSI's raw opportunity list. PSI will happily report a 40ms
#: saving on an audit nobody can act on; what a WordPress site owner needs is
#: the handful that move a metric, and what plugin or setting actually does it.
AUDIT_CATALOG: dict[str, tuple[str, str, str]] = {
    # audit id: (metric it moves, effort, what it means on WordPress)
    "server-response-time": (
        "lcp", "l",
        "TTFB גבוה הוא כמעט תמיד אחסון או היעדר page cache — "
        "בדוק קודם cache ברמת השרת (LiteSpeed/Cloudflare) לפני שמאשימים תוספים",
    ),
    "render-blocking-resources": (
        "lcp", "m",
        "CSS ו-JS של תוספים שנטענים בכל דף. דחיית JS לא-קריטי ו-critical CSS "
        "דרך תוסף cache — בלי לדחות את ה-JS של הטופס",
    ),
    "largest-contentful-paint-element": (
        "lcp", "s",
        "אלמנט ה-LCP צריך preload ובלי lazy-load. בתבניות Elementor זו לרוב "
        "תמונת ה-hero שקיבלה loading=lazy אוטומטית",
    ),
    "lcp-lazy-loaded": (
        "lcp", "s",
        "התמונה הראשית מסומנת lazy — התיקון הוא להחריג אותה בתוסף האופטימיזציה",
    ),
    "modern-image-formats": (
        "lcp", "m",
        "המרה ל-WebP/AVIF דרך תוסף מדיה. לבדוק אחרי ההמרה שהתמונות בגלריות "
        "ובבאנרים של Elementor עדיין נטענות",
    ),
    "uses-responsive-images": (
        "lcp", "m",
        "תמונות שמוגשות גדולות מהתצוגה — לרוב גדלים שנמחקו או תבנית "
        "שמכריחה full size",
    ),
    "offscreen-images": (
        "lcp", "s",
        "lazy-load לתמונות מתחת לקיפול. קיים בליבת ורדפרס — לרוב תוסף מבטל אותו",
    ),
    "unused-css-rules": (
        "lcp", "m",
        "CSS של תוספים שלא בשימוש בדף הזה. Remove Unused CSS בתוסף cache, "
        "עם בדיקה ויזואלית אחרי — זו ההגדרה שהכי שוברת עיצוב",
    ),
    "unused-javascript": (
        "inp", "m",
        "לרוב סליידרים, מפות ו-chat widgets שנטענים בכל דף",
    ),
    "unminified-css": ("lcp", "s", "מיניפיקציה בתוסף ה-cache — הגדרה אחת"),
    "unminified-javascript": ("lcp", "s", "מיניפיקציה בתוסף ה-cache — הגדרה אחת"),
    "uses-text-compression": (
        "lcp", "s", "Gzip/Brotli ברמת השרת — הגדרה אחת אצל המארח",
    ),
    "uses-long-cache-ttl": (
        "lcp", "s", "תפוגת cache לנכסים סטטיים — משפיע על ביקור חוזר",
    ),
    "font-display": (
        "cls", "s",
        "font-display: swap. בלעדיו הטקסט נעלם בטעינה ואז קופץ",
    ),
    "unsized-images": (
        "cls", "s",
        "תמונות בלי width/height — הסיבה הנפוצה ביותר לקפיצות פריסה",
    ),
    "layout-shift-elements": (
        "cls", "m",
        "אלמנטים שזזים אחרי הטעינה — לרוב באנרים, cookie bar או מודעות",
    ),
    "third-party-summary": (
        "inp", "l",
        "סקריפטים חיצוניים: chat, heatmaps, פיקסלים. כל אחד נדון לגופו — "
        "חלקם נמדדים בהמרות ואסור להסיר בלי לשאול",
    ),
    "bootup-time": (
        "inp", "l", "זמן הרצת JS. לרוב מספר תוספים שעושים אותו דבר",
    ),
    "mainthread-work-breakdown": (
        "inp", "l", "עומס על ה-main thread — משפיע ישירות על INP",
    ),
    "duplicated-javascript": (
        "inp", "m", "אותה ספרייה נטענת פעמיים משני תוספים",
    ),
}


# ═══════════════════════════════════════════════════════
#  Metrics
# ═══════════════════════════════════════════════════════

@dataclass
class FieldMetrics:
    """CrUX — what real users measured. This is what Google ranks on."""

    lcp_ms: float | None = None
    inp_ms: float | None = None
    cls: float | None = None
    origin_fallback: bool = False      # data is for the origin, not this URL

    @property
    def has_data(self) -> bool:
        return any(v is not None for v in (self.lcp_ms, self.inp_ms, self.cls))

    def value(self, metric: str) -> float | None:
        return {"lcp": self.lcp_ms, "inp": self.inp_ms, "cls": self.cls}[metric]

    def verdict(self, metric: str) -> str:
        """good · needs-improvement · poor · unknown"""
        value = self.value(metric)
        if value is None:
            return "unknown"
        if value <= GOOD_THRESHOLD[metric]:
            return "good"
        if value <= POOR_THRESHOLD[metric]:
            return "needs-improvement"
        return "poor"

    def headroom_ms(self, metric: str) -> float | None:
        """Milliseconds between where this metric is and where "good" starts.

        Nothing can save more time than that, whatever the lab adds up to.
        `None` means there is no measured field value to cap against.
        """
        value = self.value(metric)
        if value is None or metric == "cls":       # CLS is not a duration
            return None
        return max(0.0, value - GOOD_THRESHOLD[metric])

    @property
    def failing(self) -> list[str]:
        """Metrics that are not in the green. Empty means the page passes CWV."""
        return [m for m in ("lcp", "inp", "cls") if self.verdict(m) in ("needs-improvement", "poor")]

    def summary(self) -> str:
        if not self.has_data:
            return "אין נתוני שדה — הדף לא מקבל מספיק תנועה ב-CrUX"
        parts = []
        for metric, label, fmt in (("lcp", "LCP", "{:.0f}ms"), ("inp", "INP", "{:.0f}ms"),
                                   ("cls", "CLS", "{:.3f}")):
            value = self.value(metric)
            parts.append(f"{label} {fmt.format(value)}" if value is not None else f"{label} —")
        tail = " (נתוני מקור, לא הדף)" if self.origin_fallback else ""
        return " · ".join(parts) + tail


@dataclass
class LabMetrics:
    """Lighthouse — a simulation. Useful for causes, not for verdicts."""

    score: float | None = None          # 0–100
    lcp_ms: float | None = None
    tbt_ms: float | None = None
    cls: float | None = None
    ttfb_ms: float | None = None
    fcp_ms: float | None = None

    def summary(self) -> str:
        score = f"{self.score:.0f}" if self.score is not None else "—"
        return (
            f"ציון {score} · LCP {self.lcp_ms or 0:.0f}ms · "
            f"TBT {self.tbt_ms or 0:.0f}ms · TTFB {self.ttfb_ms or 0:.0f}ms"
        )


@dataclass
class Opportunity:
    """One thing worth doing, already translated into WordPress terms."""

    audit_id: str
    title: str
    metric: str                 # lcp · inp · cls
    effort: str                 # s · m · l
    savings_ms: float
    savings_bytes: float
    hint: str
    field_verdict: str          # the field's opinion on the metric this moves

    @property
    def weight(self) -> float:
        """How much of the lab saving we are willing to believe.

        A saving on a metric the field already passes is mostly theatre; a
        saving with no field data at all is a guess we hold loosely.
        """
        if self.field_verdict == "poor":
            return 1.0
        if self.field_verdict == "needs-improvement":
            return 0.8
        if self.field_verdict == "good":
            return 0.2
        return 0.6              # unknown

    @property
    def expected_gain_ms(self) -> float:
        return self.savings_ms * self.weight

    @property
    def addresses_failure(self) -> bool:
        """Whether this moves a metric the field says is actually failing."""
        return self.field_verdict in ("poor", "needs-improvement")

    @property
    def score(self) -> float:
        """Expected gain over effort — the order *within* a tier."""
        return self.expected_gain_ms / _EFFORT_WEIGHT[self.effort]


@dataclass
class Diagnosis:
    url: str
    strategy: str
    field: FieldMetrics
    lab: LabMetrics
    opportunities: list[Opportunity] = dc_field(default_factory=list)
    fetched_at: str = ""

    @property
    def total_expected_gain_ms(self) -> float:
        """Achievable saving, not the sum of the opportunity list.

        PSI's opportunities overlap heavily — render-blocking CSS, unoptimised
        images and an unminified stylesheet all claim the same seconds of LCP.
        Adding them up produces a number no site has ever reached. So gains are
        pooled per metric and capped at that metric's distance to "good", and
        CLS is left out of a millisecond total because it is not a duration.
        """
        pooled: dict[str, float] = {}
        for opportunity in self.opportunities:
            if opportunity.metric == "cls":
                continue
            pooled[opportunity.metric] = (
                pooled.get(opportunity.metric, 0.0) + opportunity.expected_gain_ms
            )

        total = 0.0
        for metric, gain in pooled.items():
            cap = self.field.headroom_ms(metric)
            total += gain if cap is None else min(gain, cap)
        return total

    def ranked(self) -> list[Opportunity]:
        """Failing metrics first, then by gain over effort.

        Sorting on score alone lets a cheap fix to an already-healthy metric
        outrank an expensive fix to the one Google is actually marking down.
        A metric the field passes has no headroom left, however easy it is.
        """
        return sorted(
            self.opportunities,
            key=lambda o: (o.addresses_failure, o.score),
            reverse=True,
        )


# ═══════════════════════════════════════════════════════
#  Parsing
# ═══════════════════════════════════════════════════════

_CRUX_KEYS = {
    "lcp": "LARGEST_CONTENTFUL_PAINT_MS",
    "inp": "INTERACTION_TO_NEXT_PAINT",
    "cls": "CUMULATIVE_LAYOUT_SHIFT_SCORE",
}


def parse_field(payload: dict[str, Any]) -> FieldMetrics:
    experience = payload.get("loadingExperience") or {}
    metrics = experience.get("metrics") or {}
    if not metrics:
        experience = payload.get("originLoadingExperience") or {}
        metrics = experience.get("metrics") or {}
        fallback = bool(metrics)
    else:
        fallback = bool(experience.get("origin_fallback"))

    def percentile(metric: str) -> float | None:
        entry = metrics.get(_CRUX_KEYS[metric])
        if not entry or "percentile" not in entry:
            return None
        raw = float(entry["percentile"])
        # CrUX reports CLS as an integer hundredth, e.g. 12 means 0.12.
        return raw / 100.0 if metric == "cls" else raw

    return FieldMetrics(
        lcp_ms=percentile("lcp"),
        inp_ms=percentile("inp"),
        cls=percentile("cls"),
        origin_fallback=fallback,
    )


def parse_lab(payload: dict[str, Any]) -> LabMetrics:
    lighthouse = payload.get("lighthouseResult") or {}
    audits = lighthouse.get("audits") or {}
    categories = lighthouse.get("categories") or {}

    def numeric(audit_id: str) -> float | None:
        value = (audits.get(audit_id) or {}).get("numericValue")
        return float(value) if isinstance(value, (int, float)) else None

    raw_score = (categories.get("performance") or {}).get("score")
    return LabMetrics(
        score=float(raw_score) * 100 if isinstance(raw_score, (int, float)) else None,
        lcp_ms=numeric("largest-contentful-paint"),
        tbt_ms=numeric("total-blocking-time"),
        cls=numeric("cumulative-layout-shift"),
        ttfb_ms=numeric("server-response-time"),
        fcp_ms=numeric("first-contentful-paint"),
    )


#: Diagnostics report a total, not a saving. This is the share of that total we
#: treat as realistically reducible — main-thread work never goes to zero.
_DIAGNOSTIC_REDUCIBLE = 0.30


def _diagnostic_ms(audit_id: str, audit: dict[str, Any]) -> float:
    """A saving estimate for audits Lighthouse reports as a total, not a saving."""
    details = audit.get("details") or {}
    if audit_id == "third-party-summary":
        wasted = (details.get("summary") or {}).get("wastedMs")
        return float(wasted or 0)
    if audit_id in ("bootup-time", "mainthread-work-breakdown"):
        total = audit.get("numericValue")
        return float(total or 0) * _DIAGNOSTIC_REDUCIBLE
    return 0.0


def parse_opportunities(payload: dict[str, Any], field: FieldMetrics) -> list[Opportunity]:
    audits = (payload.get("lighthouseResult") or {}).get("audits") or {}
    found: list[Opportunity] = []

    for audit_id, (metric, effort, hint) in AUDIT_CATALOG.items():
        audit = audits.get(audit_id)
        if not audit:
            continue
        # score 1 (or null with no savings) means the audit passed.
        score = audit.get("score")
        details = audit.get("details") or {}
        savings_ms = float(details.get("overallSavingsMs") or 0)
        savings_bytes = float(details.get("overallSavingsBytes") or 0)

        if savings_ms <= 0:
            savings_ms = _diagnostic_ms(audit_id, audit)

        if savings_ms < MIN_SAVINGS_MS:
            # Some audits carry no time estimate but still fail loudly; keep
            # those only when Lighthouse marked them failing outright.
            if score is None or score >= 0.9 or savings_bytes <= 0:
                continue
            savings_ms = MIN_SAVINGS_MS

        found.append(
            Opportunity(
                audit_id=audit_id,
                title=str(audit.get("title") or audit_id),
                metric=metric,
                effort=effort,
                savings_ms=savings_ms,
                savings_bytes=savings_bytes,
                hint=hint,
                field_verdict=field.verdict(metric),
            )
        )
    return found


def parse(payload: dict[str, Any], url: str = "", strategy: str = "mobile") -> Result:
    """Turn one PSI response into a diagnosis."""
    if not isinstance(payload, dict) or "lighthouseResult" not in payload:
        return Result.failure(
            "psi_unusable",
            "התשובה מ-PageSpeed לא מכילה lighthouseResult — "
            "ייתכן שהבדיקה נכשלה או שהדף חסום",
            recoverable=True,
        )
    if strategy not in STRATEGIES:
        return Result.failure("bad_strategy", f"strategy לא מוכר: {strategy!r}")

    lighthouse = payload["lighthouseResult"]
    field = parse_field(payload)
    diagnosis = Diagnosis(
        url=url or payload.get("id") or lighthouse.get("finalUrl", ""),
        strategy=strategy,
        field=field,
        lab=parse_lab(payload),
        opportunities=parse_opportunities(payload, field),
        fetched_at=lighthouse.get("fetchTime") or datetime.now(timezone.utc).isoformat(),
    )
    return Result.success(
        "parsed",
        f"{len(diagnosis.opportunities)} הזדמנויות ב-{strategy}",
        diagnosis=diagnosis,
    )


def load_export(path: Path) -> Result:
    """Load a saved PSI run: {"mobile": {...}, "desktop": {...}} per URL.

    Shape:
        {"url": "...", "mobile": <psi response>, "desktop": <psi response>}
    """
    import json

    if not path.exists():
        return Result.failure(
            "psi_missing",
            f"לא נמצא קובץ נתוני PageSpeed ב-{path}. "
            "הרץ עם --live ומפתח PAGESPEED_API_KEY, או שמור תשובת PSI לקובץ.",
            recoverable=True,
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return Result.failure("psi_corrupt", f"קובץ ה-PSI אינו JSON תקין: {exc}")

    url = raw.get("url", "")
    diagnoses: dict[str, Diagnosis] = {}
    for strategy in STRATEGIES:
        if strategy not in raw:
            continue
        parsed = parse(raw[strategy], url=url, strategy=strategy)
        if not parsed:
            return parsed
        diagnoses[strategy] = parsed.data["diagnosis"]

    if not diagnoses:
        return Result.failure(
            "psi_incomplete",
            "הקובץ לא מכיל אף אחת מהבדיקות mobile/desktop — "
            "מובייל הוא המדד שגוגל מדרג לפיו, אז הוא חובה",
        )
    if "mobile" not in diagnoses:
        return Result.failure(
            "psi_no_mobile",
            "חסרה בדיקת mobile. גוגל מדרג לפי מובייל — דסקטופ לבדו לא מספיק",
        )
    return Result.success("loaded", f"{len(diagnoses)} בדיקות נטענו", diagnoses=diagnoses)


# ═══════════════════════════════════════════════════════
#  Live fetch
# ═══════════════════════════════════════════════════════

API_URL = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
FETCH_TIMEOUT = 120          # שניות — PSI איטי במתכוון


def fetch(url: str, strategy: str = "mobile", api_key: str = "") -> Result:
    """Run PSI live. Without a key the endpoint is heavily rate limited."""
    import requests

    params = {"url": url, "strategy": strategy, "category": "performance"}
    if api_key:
        params["key"] = api_key
    try:
        response = requests.get(API_URL, params=params, timeout=FETCH_TIMEOUT)
    except Exception as exc:
        return Result.failure("psi_unreachable", f"PageSpeed לא נענה: {exc}")

    if response.status_code == 429:
        return Result.failure(
            "psi_rate_limited",
            "PageSpeed החזיר 429 — הוסף PAGESPEED_API_KEY או המתן",
            recoverable=True,
        )
    if response.status_code != 200:
        return Result.failure("psi_error", f"PageSpeed החזיר {response.status_code}")

    try:
        return Result.success("fetched", f"{strategy} נבדק", payload=response.json())
    except Exception:
        return Result.failure("psi_bad_payload", "התשובה מ-PageSpeed אינה JSON")


# ═══════════════════════════════════════════════════════
#  Local Lighthouse — the only option for a site PSI cannot reach
# ═══════════════════════════════════════════════════════

LIGHTHOUSE_TIMEOUT = 180     # שניות

#: Flags that make a local run comparable to what PSI does, and headless so it
#: does not steal focus. The throttling preset is PSI's own.
_LIGHTHOUSE_FLAGS = (
    "--output=json",
    "--only-categories=performance",
    "--quiet",
    "--chrome-flags=--headless=new --no-sandbox",
)

_FORM_FACTOR = {
    "mobile": ("--form-factor=mobile", "--screenEmulation.mobile"),
    "desktop": ("--preset=desktop",),
}


def is_local(url: str) -> bool:
    """Whether PSI could reach this URL at all.

    A site on localhost or a `.local` domain is invisible to Google's servers,
    so a PSI run against it fails no matter what key is supplied.
    """
    host = urlparse(url).hostname or ""
    return (
        host in ("localhost", "127.0.0.1", "::1", "0.0.0.0")
        or host.endswith((".local", ".test", ".localhost"))
        or host.startswith(("192.168.", "10.", "172.16."))
    )


def fetch_local(url: str, strategy: str = "mobile", runner: Any = None) -> Result:
    """Run Lighthouse on this machine and return it in the PSI response shape.

    Lighthouse prints the same report object that PSI nests under
    `lighthouseResult`, so wrapping it lets the rest of this module treat a
    local run and an API run identically.

    What it cannot produce is field data. CrUX comes from real Chrome users,
    and a site nobody has visited has none — so every finding from a local run
    is lab-only and carries low confidence, which `to_finding` already says.
    """
    import json
    import subprocess

    command = [
        "npx", "--yes", "lighthouse", url,
        *_LIGHTHOUSE_FLAGS, *_FORM_FACTOR.get(strategy, ()),
    ]
    run = runner or (lambda argv: subprocess.run(
        argv, capture_output=True, text=True, timeout=LIGHTHOUSE_TIMEOUT
    ))

    try:
        done = run(command)
    except FileNotFoundError:
        return Result.failure(
            "lighthouse_missing",
            "לא נמצא npx. התקן Node.js — Lighthouse הוא הדרך היחידה למדוד "
            "אתר לוקאלי, כי PageSpeed לא יכול להגיע אליו",
            recoverable=True,
        )
    except Exception as exc:
        return Result.failure("lighthouse_failed", f"Lighthouse נכשל: {exc}")

    if done.returncode != 0:
        detail = (done.stderr or done.stdout or "").strip()[:200]
        return Result.failure("lighthouse_failed", f"Lighthouse החזיר שגיאה: {detail}")

    try:
        report = json.loads(done.stdout)
    except json.JSONDecodeError:
        return Result.failure(
            "lighthouse_bad_output", "הפלט של Lighthouse אינו JSON תקין"
        )

    if "audits" not in report:
        return Result.failure(
            "lighthouse_bad_output", "הפלט של Lighthouse לא מכיל audits"
        )

    # Lighthouse 10 renamed finalUrl; keep both so the parser finds one.
    report.setdefault("finalUrl", report.get("finalDisplayedUrl", url))
    return Result.success(
        "fetched", f"{strategy} נמדד מקומית", payload={"lighthouseResult": report}
    )


def measure(url: str, strategy: str = "mobile", api_key: str = "", local: bool = False) -> Result:
    """Pick the right engine: local Lighthouse, or the PSI API.

    A local URL is routed to Lighthouse automatically rather than being sent to
    an API that cannot see it.
    """
    if local or is_local(url):
        return fetch_local(url, strategy)
    return fetch(url, strategy, api_key)


# ═══════════════════════════════════════════════════════
#  Findings
# ═══════════════════════════════════════════════════════

def estimate_impact(
    diagnosis: Diagnosis, traffic: dict[str, Any] | None
) -> tuple[float, float | None, str]:
    """Clicks, conversions, and the sentence explaining both.

    Without traffic data both are zero and the basis says so — a made-up click
    number would rank this page against a decay finding on fiction.
    """
    gain_ms = diagnosis.total_expected_gain_ms
    failing = diagnosis.field.failing

    if not traffic:
        return (
            0.0, None,
            f"חיסכון צפוי {gain_ms:.0f}ms במובייל. בלי נתוני תנועה לדף אי אפשר "
            "לתרגם את זה לקליקים — הדירוג כאן הוא לפי חיסכון חלקי מאמץ",
        )

    clicks = float(traffic.get("clicks_28d") or 0)
    sessions = float(traffic.get("sessions_28d") or 0)
    rate = traffic.get("conversion_rate")

    impact_clicks = 0.0
    if failing and diagnosis.field.has_data:
        impact_clicks = clicks * CWV_CLICK_UPLIFT

    impact_conversions = None
    if sessions and rate:
        uplift = min(gain_ms / 100.0 * CONVERSION_UPLIFT_PER_100MS, MAX_CONVERSION_UPLIFT)
        impact_conversions = sessions * float(rate) * uplift

    if failing:
        basis = (
            f"הדף נכשל בשדה ב-{', '.join(m.upper() for m in failing)}; "
            f"{CWV_CLICK_UPLIFT:.0%} מ-{clicks:.0f} הקליקים כאומדן שמרני ל-page experience"
        )
    else:
        basis = (
            f"הדף עובר Core Web Vitals בשדה, אז אין כאן רווח בדירוג. "
            f"החיסכון של {gain_ms:.0f}ms נמדד בהמרות בלבד"
        )
    if impact_conversions is not None:
        uplift = min(gain_ms / 100.0 * CONVERSION_UPLIFT_PER_100MS, MAX_CONVERSION_UPLIFT)
        basis += (
            f" · המרות: אומדן מודל ולא מדידה — {gain_ms:.0f}ms על "
            f"{sessions:.0f} סשנים בשיעור המרה {float(rate):.1%}, "
            f"{uplift:.1%} לפי 0.8% לכל 100ms וחסום ב-{MAX_CONVERSION_UPLIFT:.0%}"
        )
    return impact_clicks, impact_conversions, basis


def _severity(diagnosis: Diagnosis) -> str:
    poor = [m for m in ("lcp", "inp", "cls") if diagnosis.field.verdict(m) == "poor"]
    if poor:
        return "high"
    if diagnosis.field.failing:
        return "medium"
    if not diagnosis.field.has_data and (diagnosis.lab.score or 100) < 50:
        return "medium"
    return "low"


def _confidence(diagnosis: Diagnosis) -> tuple[str, str]:
    if not diagnosis.field.has_data:
        return "low", (
            "אין נתוני שדה — הכל מבוסס על סימולציית מעבדה, שנוטה להגזים "
            "בבעיות ולהחמיץ אחרות"
        )
    if diagnosis.field.origin_fallback:
        return "medium", (
            "נתוני השדה הם של המקור כולו ולא של הדף הזה — "
            "הכיוון נכון, המספר המדויק פחות"
        )
    if diagnosis.field.failing:
        return "high", (
            f"נתוני CrUX אמיתיים מ-28 יום מראים כישלון ב-"
            f"{', '.join(m.upper() for m in diagnosis.field.failing)}"
        )
    return "medium", (
        "נתוני השדה תקינים — השיפור ישפיע על חוויית המשתמש, לא על הדירוג"
    )


def to_finding(
    diagnosis: Diagnosis, client: str, traffic: dict[str, Any] | None = None
) -> Finding:
    """One finding per page: the whole speed problem, ranked as one job."""
    ranked = diagnosis.ranked()
    impact_clicks, impact_conversions, basis = estimate_impact(diagnosis, traffic)
    confidence, reason = _confidence(diagnosis)

    # The effort of the job is the effort of the work actually worth doing.
    top = ranked[:3]
    effort = max((o.effort for o in top), key=lambda e: _EFFORT_WEIGHT[e], default="m")

    return Finding(
        skill="wordpress-speed-optimizer",
        client=client,
        type="slow_page",
        severity=_severity(diagnosis),
        source="pagespeed",
        url=diagnosis.url,
        impact_clicks=round(impact_clicks, 1),
        impact_conversions=round(impact_conversions, 2) if impact_conversions is not None else None,
        impact_basis=basis,
        confidence=confidence,
        confidence_reason=reason,
        effort=effort,
        evidence={
            "strategy": diagnosis.strategy,
            "field": {
                "lcp_ms": diagnosis.field.lcp_ms,
                "inp_ms": diagnosis.field.inp_ms,
                "cls": diagnosis.field.cls,
                "origin_fallback": diagnosis.field.origin_fallback,
                "failing": diagnosis.field.failing,
            },
            "lab": {
                "score": diagnosis.lab.score,
                "lcp_ms": diagnosis.lab.lcp_ms,
                "tbt_ms": diagnosis.lab.tbt_ms,
                "ttfb_ms": diagnosis.lab.ttfb_ms,
            },
            "expected_gain_ms": round(diagnosis.total_expected_gain_ms),
            "opportunities": [
                {
                    "audit": o.audit_id, "metric": o.metric, "effort": o.effort,
                    "savings_ms": round(o.savings_ms),
                    "expected_gain_ms": round(o.expected_gain_ms),
                    "hint": o.hint,
                }
                for o in ranked
            ],
        },
        action={
            "kind": "speed_work_order",
            "url": diagnosis.url,
            "steps": [f"{o.title} — {o.hint}" for o in top],
            "verify": "הרץ --mode verify אחרי הביצוע: מודד שוב וגם בודק טפסים ועיצוב",
        },
        baseline={
            "field_lcp_ms": diagnosis.field.lcp_ms,
            "field_inp_ms": diagnosis.field.inp_ms,
            "field_cls": diagnosis.field.cls,
            "lab_score": diagnosis.lab.score,
            "measured": diagnosis.fetched_at,
        },
    )


# ═══════════════════════════════════════════════════════
#  Comparing two runs
# ═══════════════════════════════════════════════════════

@dataclass
class Movement:
    metric: str
    before: float | None
    after: float | None

    @property
    def readable(self) -> bool:
        return self.before is not None and self.after is not None

    @property
    def delta(self) -> float | None:
        return None if not self.readable else self.after - self.before

    @property
    def improved(self) -> bool:
        return self.readable and self.after < self.before


def compare(before: Diagnosis, after: Diagnosis) -> dict[str, Any]:
    """Before/after on the metrics that matter, field first.

    Field metrics move over 28 days, not over an afternoon, so a verify run the
    same day will show the lab moving and the field standing still. That is
    expected and is stated rather than hidden.
    """
    field_moves = [
        Movement(metric, before.field.value(metric), after.field.value(metric))
        for metric in ("lcp", "inp", "cls")
    ]
    lab = Movement("lab_score", before.lab.score, after.lab.score)

    field_readable = [m for m in field_moves if m.readable]
    verdict = "inconclusive"
    if field_readable and all(m.improved or (m.delta or 0) == 0 for m in field_readable):
        verdict = "improved" if any(m.improved for m in field_readable) else "unchanged"
    elif field_readable and any((m.delta or 0) > 0 for m in field_readable):
        verdict = "regressed"

    return {
        "field": field_moves,
        "lab_score": lab,
        "verdict": verdict,
        "note": (
            "נתוני CrUX הם חלון נע של 28 יום — מדידה ביום הביצוע עדיין מציגה "
            "את המצב הישן. השוואת שדה אמיתית היא 28 יום אחרי"
        ),
    }
