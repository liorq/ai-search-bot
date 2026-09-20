"""
What an answer engine can see, take, and cite.
===============================================

Three separate questions, and conflating them is how sites end up blocking the
crawler that would have cited them while leaving open the one that only trains
on them:

    * are the AI crawlers allowed in at all (robots.txt, per agent);
    * is there an `llms.txt` telling them what the site is;
    * does a page carry the structured data and the short direct answer that
      make it quotable.

Nothing here judges whether a site *should* allow these crawlers. That is the
owner's call, and the two decisions — being trained on, and being cited —
travel under different agent names, so the report keeps them apart instead of
recommending a blanket answer.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Callable

from ..schema import Result
from .crawl import parse_robots

#: The agents worth asking about, and what allowing each one actually means.
AI_AGENTS = {
    "GPTBot": "OpenAI — אימון מודלים",
    "OAI-SearchBot": "OpenAI — הפניות מ-ChatGPT Search",
    "ChatGPT-User": "OpenAI — גלישה לבקשת משתמש",
    "ClaudeBot": "Anthropic — אימון מודלים",
    "Claude-SearchBot": "Anthropic — חיפוש",
    "PerplexityBot": "Perplexity — אינדוקס וציטוט",
    "Google-Extended": "Google — אימון Gemini (לא משפיע על חיפוש)",
    "Bingbot": "Bing — חיפוש, וגם מה ש-Copilot מצטט",
    "Applebot-Extended": "Apple — אימון",
}

#: Agents whose job is to send traffic back. Blocking these costs citations.
CITING_AGENTS = ("OAI-SearchBot", "ChatGPT-User", "Claude-SearchBot",
                 "PerplexityBot", "Bingbot")

#: A direct answer that an engine can lift whole. Longer than this and it gets
#: summarised into something the site did not write.
ANSWER_WORDS = (40, 60)

_JSONLD = re.compile(r"""<script[^>]+type\s*=\s*["']application/ld\+json["'][^>]*>(.*?)</script>""",
                     re.I | re.S)
_HEADING = re.compile(r"<h([1-3])\b[^>]*>(.*?)</h\1>", re.I | re.S)
_TAG = re.compile(r"<[^>]+>")
_QUESTION = re.compile(r"^(how|what|why|when|where|which|who|is|are|can|does|do|should"
                       r"|מה|איך|למה|מתי|כמה|האם|מי|איזה)\b", re.I)

Fetcher = Callable[[str], tuple[int, str, str]]


@dataclass
class AgentRule:
    agent: str
    purpose: str
    allowed: bool


@dataclass
class PageAnswer:
    """Whether one page is shaped so an engine can quote it."""

    url: str
    question_headings: int = 0
    answered_directly: int = 0        # question heading followed by a short answer
    schema_types: list[str] = field(default_factory=list)
    has_faq: bool = False

    @property
    def quotable(self) -> bool:
        return self.answered_directly > 0 and bool(self.schema_types)


@dataclass
class Surface:
    robots_found: bool = False
    agents: list[AgentRule] = field(default_factory=list)
    llms_txt: bool = False
    llms_txt_bytes: int = 0
    pages: list[PageAnswer] = field(default_factory=list)

    @property
    def blocked_citers(self) -> list[str]:
        return [r.agent for r in self.agents if r.agent in CITING_AGENTS and not r.allowed]

    @property
    def blocked_trainers(self) -> list[str]:
        return [r.agent for r in self.agents
                if r.agent not in CITING_AGENTS and not r.allowed]


def read_robots(text: str | None) -> list[AgentRule]:
    """How robots.txt answers each AI agent, one line per agent.

    Asked per agent rather than read as a whole, because `Disallow: /` under
    `User-agent: GPTBot` and the same line under `*` mean different things to
    a site owner deciding what to change.
    """
    if text is None:
        return []
    rules = []
    for agent, purpose in AI_AGENTS.items():
        parsed = parse_robots(text, agent=agent)
        rules.append(AgentRule(agent=agent, purpose=purpose, allowed=parsed.allows("/")))
    return rules


def read_llms_txt(status: int, body: str) -> tuple[bool, int]:
    """Present only when it is really there: a 404 page is not an llms.txt."""
    if status != 200 or not body.strip():
        return False, 0
    if body.lstrip().lower().startswith("<!doctype") or "<html" in body[:400].lower():
        return False, 0
    return True, len(body.encode("utf-8"))


def _words(fragment: str) -> list[str]:
    return _TAG.sub(" ", fragment).split()


def read_page(url: str, html: str) -> PageAnswer:
    """Does this page answer a question in a form an engine can take whole?"""
    page = PageAnswer(url=url)

    for raw in _JSONLD.findall(html):
        try:
            data = json.loads(raw.strip())
        except json.JSONDecodeError:
            continue
        for block in (data if isinstance(data, list) else [data]):
            if not isinstance(block, dict):
                continue
            kind = block.get("@type")
            for name in (kind if isinstance(kind, list) else [kind]):
                if name:
                    page.schema_types.append(str(name))
                    if str(name).lower() == "faqpage":
                        page.has_faq = True

    matches = list(_HEADING.finditer(html))
    for index, match in enumerate(matches):
        heading = _TAG.sub("", match.group(2)).strip()
        if not _QUESTION.match(heading):
            continue
        page.question_headings += 1
        end = matches[index + 1].start() if index + 1 < len(matches) else len(html)
        count = len(_words(html[match.end():end]))
        if ANSWER_WORDS[0] <= count <= ANSWER_WORDS[1] * 3:
            page.answered_directly += 1
    return page


def inspect(base_url: str, fetch: Fetcher, pages: dict[str, str] | None = None) -> Result:
    """Read robots.txt, llms.txt and any page HTML already in hand."""
    root = base_url.rstrip("/")
    surface = Surface()

    try:
        status, body, _ = fetch(f"{root}/robots.txt")
        if status == 200:
            surface.robots_found = True
            surface.agents = read_robots(body)
    except Exception as exc:
        return Result.failure("ai_surface_unreachable", f"לא הצלחתי לקרוא robots.txt: {exc}")

    try:
        status, body, _ = fetch(f"{root}/llms.txt")
        surface.llms_txt, surface.llms_txt_bytes = read_llms_txt(status, body)
    except Exception:
        surface.llms_txt = False

    for url, html in (pages or {}).items():
        surface.pages.append(read_page(url, html))

    return Result.success("inspected", "נבדק", surface=surface)
