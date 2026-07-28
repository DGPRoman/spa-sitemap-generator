"""robots.txt support, built on the stdlib parser.

Fetched with ``urllib`` rather than the browser: it is not a rendered document and
a browser navigation would cost seconds per crawl for no benefit.
"""

from __future__ import annotations

import logging
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urljoin
from urllib.robotparser import RobotFileParser

log = logging.getLogger(__name__)

DEFAULT_USER_AGENT = "spa-sitemap-generator"

Fetcher = Callable[[str], str]
"""Fetches a URL's body as text. Injected so tests need no network."""


@dataclass(frozen=True, slots=True)
class Robots:
    """A parsed robots.txt. ``AllowAll`` semantics when it was missing or unreadable."""

    parser: RobotFileParser | None
    user_agent: str
    crawl_delay: float | None = None

    def allows(self, url: str) -> bool:
        if self.parser is None:
            return True
        return self.parser.can_fetch(self.user_agent, url)


def allow_all(user_agent: str = DEFAULT_USER_AGENT) -> Robots:
    return Robots(parser=None, user_agent=user_agent)


def _urllib_fetch(url: str, *, timeout: float, user_agent: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": user_agent})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        body: bytes = response.read()
        return body.decode(charset, errors="replace")


def load(
    base_url: str,
    *,
    user_agent: str = DEFAULT_USER_AGENT,
    timeout: float = 10.0,
    fetch: Fetcher | None = None,
) -> Robots:
    """Fetch and parse ``/robots.txt`` for ``base_url``.

    A missing or broken robots.txt means "no rules", which is what the standard
    says -- so a failure here must never abort the crawl.
    """
    robots_url = urljoin(base_url, "/robots.txt")
    fetcher = fetch or (lambda url: _urllib_fetch(url, timeout=timeout, user_agent=user_agent))

    try:
        body = fetcher(robots_url)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        log.info("no usable robots.txt at %s (%s); assuming everything is allowed",
                 robots_url, exc)
        return allow_all(user_agent)

    parser = RobotFileParser()
    parser.parse(body.splitlines())

    delay: float | None = None
    try:
        raw_delay = parser.crawl_delay(user_agent)
        delay = float(raw_delay) if raw_delay is not None else None
    except (TypeError, ValueError):
        delay = None

    log.info("loaded %s%s", robots_url, f" (crawl-delay {delay}s)" if delay else "")
    return Robots(parser=parser, user_agent=user_agent, crawl_delay=delay)
