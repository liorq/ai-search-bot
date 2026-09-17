"""
The client registry.
====================

One `clients.json` holds everything the skills need to know about each site:
which Search Console property, which CMS, which language the *content* is
written in, what counts as a conversion, and who the competitors are.

Without it, every skill re-asks the same questions on every run.

Content language vs. interface language
---------------------------------------
`content_language` is the language of text written *to the client's site* —
usually "en", since most of the book is US. It has nothing to do with the
language the skills speak to Lior, which is always Hebrew. Conflating the two
is how a US client ends up with a Hebrew paragraph on their service page.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import paths
from .urls import is_local

#: Snapshot taken at import. `load_all` resolves the path freshly so that
#: SEO_HOME relocates the registry, the .env and the per-client data
#: together, whenever it happens to be set.
DEFAULT_REGISTRY = paths.registry_path()

#: DataForSEO location codes for the markets we actually serve.
LOCATION_CODES = {"us": 2840, "il": 2376}

SUPPORTED_CMS = ("wordpress", "manual")

#: Conversion events we know how to read out of GA4. A client may name others;
#: these are the ones with built-in validation logic.
KNOWN_CONVERSIONS = (
    "form_submit", "phone_click", "whatsapp_click", "email_click",
    "chat_open", "booking_complete", "purchase",
)


class ClientConfigError(Exception):
    """Raised when the registry is missing or malformed. Never swallowed —
    running a live-write skill against a half-configured client is worse than
    not running it."""


@dataclass
class CMSConfig:
    type: str = "manual"
    base_url: str = ""
    username: str = ""
    auth_env: str = ""                 # name of the env var holding the app password
    staging_url: str = ""              # where write/restore is rehearsed

    @property
    def is_writable(self) -> bool:
        """Whether we could write at all. Capability is probed separately —
        this only says the config claims a writable CMS."""
        return self.type == "wordpress" and bool(self.base_url and self.auth_env)


@dataclass
class Client:
    domain: str
    market: str = "us"
    content_language: str = "en"
    gsc_property: str = ""
    cms: CMSConfig = field(default_factory=CMSConfig)
    ga4_property_id: str = ""
    clarity_project_id: str = ""
    localo_project: str = ""
    gbp_location_id: str = ""
    conversions: list[str] = field(default_factory=list)
    competitors: list[str] = field(default_factory=list)
    conversion_value: float | None = None   # avg value of one conversion, if known
    notes: str = ""

    @property
    def location_code(self) -> int:
        return LOCATION_CODES.get(self.market, LOCATION_CODES["us"])

    @property
    def language_code(self) -> str:
        return self.content_language

    @property
    def has_conversion_data(self) -> bool:
        """True when impact can be expressed in conversions rather than clicks."""
        return bool(self.ga4_property_id and self.conversions)

    @property
    def data_dir(self) -> Path:
        return paths.data_dir(self.domain)

    def secret(self) -> str:
        """The CMS password, read from the environment at the moment of use.

        Deliberately not stored on the object: a config dumped to a log or a
        report should never be able to carry a credential with it.
        """
        if not self.cms.auth_env:
            raise ClientConfigError(f"ללקוח {self.domain} לא מוגדר auth_env")
        value = os.environ.get(self.cms.auth_env, "")
        if not value:
            raise ClientConfigError(
                f"משתנה הסביבה {self.cms.auth_env} ריק — "
                f"הוסף אותו ל-~/.claude/seo/.env"
            )
        return value


def _parse(domain: str, raw: dict[str, Any]) -> Client:
    cms_raw = raw.get("cms", {}) or {}
    cms = CMSConfig(
        type=cms_raw.get("type", "manual"),
        base_url=cms_raw.get("base_url", "").rstrip("/"),
        username=cms_raw.get("username", ""),
        auth_env=cms_raw.get("auth_env", ""),
        staging_url=cms_raw.get("staging_url", "").rstrip("/"),
    )
    return Client(
        domain=domain,
        market=raw.get("market", "us"),
        content_language=raw.get("content_language", "en"),
        gsc_property=raw.get("gsc_property", ""),
        cms=cms,
        ga4_property_id=str(raw.get("ga4_property_id", "")),
        clarity_project_id=raw.get("clarity_project_id", ""),
        localo_project=raw.get("localo_project", ""),
        gbp_location_id=raw.get("gbp_location_id", ""),
        conversions=list(raw.get("conversions", [])),
        competitors=list(raw.get("competitors", [])),
        conversion_value=raw.get("conversion_value"),
        notes=raw.get("notes", ""),
    )


def validate(client: Client) -> list[str]:
    """Return human-readable problems. Empty list means the client is usable."""
    problems: list[str] = []

    # A development site can never have a Search Console property, so demanding
    # one would block the rehearsal — the one thing a local site is for.
    if not client.gsc_property and not is_local(client.cms.base_url):
        problems.append("חסר gsc_property — בלעדיו אין נתוני חיפוש")
    if client.market not in LOCATION_CODES:
        problems.append(
            f"market לא מוכר: {client.market!r} (נתמכים: {', '.join(LOCATION_CODES)})"
        )
    if client.cms.type not in SUPPORTED_CMS:
        problems.append(f"סוג CMS לא נתמך: {client.cms.type!r}")
    if client.cms.type == "wordpress":
        if not client.cms.base_url:
            problems.append("CMS מסוג wordpress בלי base_url")
        if not client.cms.auth_env:
            problems.append("CMS מסוג wordpress בלי auth_env")
    if client.ga4_property_id and not client.conversions:
        problems.append(
            "יש ga4_property_id אבל אין conversions — לא נדע מה למדוד כהמרה"
        )
    for event in client.conversions:
        if event not in KNOWN_CONVERSIONS:
            problems.append(
                f"אירוע המרה לא מוכר: {event!r} — יימדד, אבל בלי ולידציה אוטומטית"
            )
    return problems


def load_all(path: Path | None = None) -> dict[str, Client]:
    registry = path or paths.registry_path()
    if not registry.exists():
        raise ClientConfigError(
            f"לא נמצא רישום לקוחות ב-{registry}. "
            "העתק את clients.example.json והשלם את הפרטים."
        )
    try:
        with open(registry, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except json.JSONDecodeError as exc:
        raise ClientConfigError(f"clients.json אינו JSON תקין: {exc}") from exc

    return {domain: _parse(domain, cfg) for domain, cfg in raw.items()}


def load(domain: str, path: Path | None = None) -> Client:
    """Load one client, refusing to return a client that cannot be used safely."""
    clients = load_all(path)
    key = domain.lower().removeprefix("www.")
    for candidate, client in clients.items():
        if candidate.lower().removeprefix("www.") == key:
            problems = validate(client)
            if problems:
                raise ClientConfigError(
                    f"ההגדרות של {candidate} אינן תקינות:\n  - "
                    + "\n  - ".join(problems)
                )
            return client

    known = ", ".join(sorted(clients)) or "(ריק)"
    raise ClientConfigError(f"לקוח {domain!r} לא נמצא ברישום. קיימים: {known}")
