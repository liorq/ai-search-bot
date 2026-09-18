"""
Core data structures shared by every SEO skill.
=======================================================

Three types travel between skills:

    Finding       one actionable observation, backed by real numbers
    Result        the outcome of an operation that can fail
    ChangeRecord  an audit entry for something written to a live site

`Finding` validates itself on construction. A finding with no evidence, no
concrete action, no stated basis for its impact estimate, or no reason for
its confidence level raises ValueError. That rule is the mechanism that keeps
skills from emitting generic advice like "consider improving the content" —
it is enforced in code, not in a style guide.

`Result` replaces the bare status strings used elsewhere in this toolkit's
ancestry. Once a skill writes to a live WordPress site, the caller has to be
able to tell a recoverable failure from an unrecoverable one before deciding
whether an automatic rollback is safe, and a string cannot carry that.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

# ═══════════════════════════════════════════════════════
#  Vocabularies
# ═══════════════════════════════════════════════════════

Severity   = Literal["blocker", "high", "medium", "low"]
Confidence = Literal["high", "medium", "low"]
Effort     = Literal["s", "m", "l"]

SEVERITIES  = ("blocker", "high", "medium", "low")
CONFIDENCES = ("high", "medium", "low")
EFFORTS     = ("s", "m", "l")

#: Where a finding's numbers came from. Unknown sources are rejected so a
#: skill cannot quietly invent a provenance label.
#:
#: This is a vocabulary, not an inventory. Live today — something in the
#: repo stamps them on a finding: gsc_wizard, pagespeed, crawl, derived.
#: The rest are reserved for sources that are planned and not yet wired.
SOURCES = (
    "gsc_wizard",      # GSC Wizard MCP — the data is pasted from it, never fetched by us
    "gsc_api",         # Google Search Console API — planned; no engine exists yet
    "ga4",             # GA4 Data API — conversions, landing-page level
    "clarity",         # Microsoft Clarity
    "pagespeed",       # PageSpeed Insights / CrUX
    "dataforseo",      # SERP results
    "localo",          # Localo MCP — local grid, reviews
    "semrush",         # Semrush — backlinks, keyword data
    "crawl",           # our own crawl of the site
    "wordpress",       # read from the WP REST API
    "derived",         # computed by combining two or more of the above
)

#: Effort is a rough cost, used only to rank. These weights turn it into a
#: divisor so that priority() stays a plain number.
_EFFORT_WEIGHT = {"s": 1.0, "m": 2.5, "l": 6.0}

#: A conversion is worth far more than a click, but how much more is
#: client-specific. Until a client supplies a real lead value, this is the
#: multiplier used to rank a conversion against a click.
DEFAULT_CONVERSION_WEIGHT = 25.0

#: Minimum impressions over the trailing 28 days for a query-level finding to
#: be worth raising at all. Skills may raise this; they may not lower it.
#: Below this, month-to-month noise is larger than any effect we could detect.
MIN_IMPRESSIONS_28D = 50

#: Phrases that mean nothing. If an impact basis or confidence reason is one
#: of these and little else, the finding is not actually justified.
_EMPTY_PHRASES = {
    "important", "significant", "best practice", "recommended", "seo",
    "improves seo", "good for seo", "n/a", "none", "unknown", "tbd",
    "חשוב", "מומלץ", "כדאי", "טוב לקידום",
}

_MIN_BASIS_CHARS = 12


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _is_vacuous(text: str) -> bool:
    """True if `text` carries no actual justification."""
    stripped = text.strip().rstrip(".").lower()
    return len(stripped) < _MIN_BASIS_CHARS or stripped in _EMPTY_PHRASES


# ═══════════════════════════════════════════════════════
#  Result — the outcome of an operation that can fail
# ═══════════════════════════════════════════════════════

@dataclass(frozen=True)
class Result:
    """Outcome of an operation, structured so a caller can act on the reason.

    `recoverable` answers exactly one question: is it safe to undo this
    automatically? A write that never landed is recoverable. A write that
    landed while someone else was editing the same post is not — rolling it
    back would destroy their work too.
    """

    ok: bool
    code: str                                  # machine-readable, e.g. "conflict"
    detail: str                                # one human sentence (Hebrew, user-facing)
    recoverable: bool = True
    data: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def success(cls, code: str, detail: str, **data: Any) -> "Result":
        return cls(ok=True, code=code, detail=detail, recoverable=True, data=data)

    @classmethod
    def failure(
        cls, code: str, detail: str, *, recoverable: bool = True, **data: Any
    ) -> "Result":
        return cls(ok=False, code=code, detail=detail, recoverable=recoverable, data=data)

    def __bool__(self) -> bool:
        return self.ok


# ═══════════════════════════════════════════════════════
#  Finding — one actionable observation
# ═══════════════════════════════════════════════════════

@dataclass
class Finding:
    """One thing worth doing, with the numbers that justify it.

    Impact is deliberately split. Clicks are always estimable from Search
    Console; conversions are only estimable when the client has GA4 wired up,
    and are None otherwise rather than being guessed. Ranking a finding on
    invented conversion data would be worse than ranking it on clicks alone.
    """

    skill: str
    client: str
    type: str                                  # e.g. "decayed_page", "missing_h2"
    severity: Severity
    source: str

    impact_clicks: float                       # estimated clicks at stake, 28d
    impact_basis: str                          # how that number was reached
    confidence: Confidence
    confidence_reason: str                     # why not higher
    effort: Effort

    evidence: dict[str, Any]                   # the numbers themselves
    action: dict[str, Any]                     # exactly what to do

    baseline: dict[str, Any]                   # state before, to compare against later
    measured_at: datetime = field(default_factory=_utcnow)

    url: str | None = None
    query: str | None = None
    impact_conversions: float | None = None    # None = client has no conversion data

    def __post_init__(self) -> None:
        self._validate()

    # ---------- validation ----------

    def _validate(self) -> None:
        missing: list[str] = []

        if not self.evidence:
            missing.append("evidence — ממצא חייב לשאת את המספרים שמצדיקים אותו")
        if not self.action:
            missing.append("action — ממצא חייב לשאת פעולה מדויקת, לא המלצה כללית")
        if not self.baseline:
            missing.append("baseline — בלי מצב התחלתי אי אפשר למדוד אם השינוי עבד")
        if _is_vacuous(self.impact_basis):
            missing.append("impact_basis — צריך להסביר איך חושב האומדן, לא 'חשוב'")
        if _is_vacuous(self.confidence_reason):
            missing.append("confidence_reason — צריך להסביר למה רמת הביטחון היא זו")

        if missing:
            raise ValueError(
                f"Finding {self.skill}/{self.type} אינו שלם:\n  - "
                + "\n  - ".join(missing)
            )

        if self.severity not in SEVERITIES:
            raise ValueError(f"severity לא חוקי: {self.severity!r}")
        if self.confidence not in CONFIDENCES:
            raise ValueError(f"confidence לא חוקי: {self.confidence!r}")
        if self.effort not in EFFORTS:
            raise ValueError(f"effort לא חוקי: {self.effort!r}")
        if self.source not in SOURCES:
            raise ValueError(f"source לא מוכר: {self.source!r}")

        if self.impact_clicks < 0:
            raise ValueError("impact_clicks לא יכול להיות שלילי")
        if self.impact_conversions is not None and self.impact_conversions < 0:
            raise ValueError("impact_conversions לא יכול להיות שלילי")

        # An action has to say what kind of change it is *and* carry the payload.
        if "kind" not in self.action:
            raise ValueError("action חייב לכלול 'kind' — איזה סוג שינוי זה")
        if len(self.action) < 2:
            raise ValueError(
                f"action מסוג {self.action['kind']!r} ריק מתוכן — "
                "צריך את הטקסט/הכתובת/האנקור המדויק"
            )

    # ---------- ranking ----------

    def priority(self, conversion_weight: float = DEFAULT_CONVERSION_WEIGHT) -> float:
        """Impact over effort, discounted by how sure we are.

        Conversions dominate clicks when we have them. When we do not, the
        finding is ranked on clicks alone rather than on a guess.
        """
        value = self.impact_clicks
        if self.impact_conversions is not None:
            value += self.impact_conversions * conversion_weight

        discount = {"high": 1.0, "medium": 0.7, "low": 0.4}[self.confidence]
        return (value * discount) / _EFFORT_WEIGHT[self.effort]

    # ---------- serialisation ----------

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["measured_at"] = self.measured_at.isoformat()
        d["priority"] = round(self.priority(), 2)
        return d

    @property
    def dedupe_key(self) -> str:
        """Identity for merging findings that two skills both raised."""
        return f"{self.client}|{self.url or ''}|{self.query or ''}|{self.type}"


def save_findings(findings: list[Finding], path: Path) -> Path:
    """Write findings to JSON, highest priority first."""
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(findings, key=lambda f: f.priority(), reverse=True)
    payload = {
        "generated_at": _utcnow().isoformat(),
        "count": len(ordered),
        "findings": [f.to_dict() for f in ordered],
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    return path


def dedupe(findings: list[Finding]) -> list[Finding]:
    """Collapse findings that describe the same problem, keeping the strongest."""
    best: dict[str, Finding] = {}
    for f in findings:
        current = best.get(f.dedupe_key)
        if current is None or f.priority() > current.priority():
            best[f.dedupe_key] = f
    return sorted(best.values(), key=lambda f: f.priority(), reverse=True)


# ═══════════════════════════════════════════════════════
#  ChangeRecord — the audit trail for a live write
# ═══════════════════════════════════════════════════════

ChangeStatus = Literal[
    "planned",       # approved, not yet written
    "applied",       # written, technical checks passed
    "rolled_back",   # undone after a technical failure
    "verified",      # held up at the 28/56-day check
    "declined",      # measured worse; Lior chose not to revert
    "inconclusive",  # no readable signal yet — keep watching
]


@dataclass
class ChangeRecord:
    """Everything needed to understand, measure, and undo one change.

    `before_hash` is what makes concurrent-edit detection possible. The post's
    `modified_gmt` is not enough on its own: it does not move for some
    meta-only updates and can be stale, so we hash the content we actually
    read and compare that hash immediately before writing.
    """

    change_id: str
    plan_id: str
    skill: str
    client: str
    url: str
    post_id: int | None

    before_hash: str                           # hash of content+meta as read
    inverse: dict[str, Any]                    # everything needed to undo
    backup_ref: str | None                     # where the backup lives
    backup_verified: bool                      # did we prove it restores?

    status: ChangeStatus = "planned"
    after_hash: str | None = None
    revision_id: int | None = None
    applied_at: datetime | None = None
    checkpoints: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def safe_to_auto_rollback(self) -> bool:
        """An automatic rollback needs a verified backup and no one else's edits.

        Missing either, we stop and tell Lior rather than guess. Restoring from
        a backup we never proved restorable is how a bad afternoon becomes a
        bad week.
        """
        return self.backup_verified and self.inverse != {}

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["applied_at"] = self.applied_at.isoformat() if self.applied_at else None
        return d


def content_hash(*parts: Any) -> str:
    """Stable hash over post content and the meta fields we intend to touch."""
    blob = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]
