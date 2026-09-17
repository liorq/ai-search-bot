"""
URL questions both the sources and the registry need answered.
==============================================================

One question, asked in two places for two reasons: PageSpeed needs to know
whether Google's servers could reach a URL, and the client registry needs to
know whether to expect a Search Console property. A site on `localhost` fails
both for the same reason, so the test lives in one place.
"""

from __future__ import annotations

from urllib.parse import urlparse

LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1", "0.0.0.0")
LOCAL_SUFFIXES = (".local", ".test", ".localhost", ".localdomain")
PRIVATE_PREFIXES = ("192.168.", "10.", "172.16.", "172.17.", "172.18.")


def is_local(url: str) -> bool:
    """Whether this URL is a development site nobody outside can reach.

    It decides two things: that PageSpeed cannot measure it, and that it will
    never have a Search Console property — so neither is treated as an error.
    """
    if not url:
        return False
    host = (urlparse(url).hostname or urlparse(f"//{url}").hostname or "").lower()
    return (
        host in LOCAL_HOSTS
        or host.endswith(LOCAL_SUFFIXES)
        or host.startswith(PRIVATE_PREFIXES)
    )
