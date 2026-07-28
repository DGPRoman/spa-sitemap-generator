"""robots.txt handling, with the fetch injected so no network is involved."""

from __future__ import annotations

import urllib.error

import pytest

from spa_sitemap import robots

BASE = "https://example.com/docs/"

RULES = """
User-agent: *
Disallow: /private/
Crawl-delay: 4

User-agent: spa-sitemap-generator
Disallow: /admin/
"""


def test_rules_for_our_user_agent_are_applied() -> None:
    policy = robots.load(BASE, fetch=lambda _url: RULES)

    assert policy.allows("https://example.com/docs/page") is True
    assert policy.allows("https://example.com/admin/panel") is False


def test_robots_is_fetched_from_the_origin_root() -> None:
    """It lives at /robots.txt even when the crawl starts deeper in the site."""
    requested: list[str] = []

    def fetch(url: str) -> str:
        requested.append(url)
        return RULES

    robots.load(BASE, fetch=fetch)
    assert requested == ["https://example.com/robots.txt"]


def test_a_crawl_delay_is_exposed() -> None:
    policy = robots.load(BASE, user_agent="*", fetch=lambda _url: RULES)
    assert policy.crawl_delay == 4


def test_no_crawl_delay_is_none() -> None:
    policy = robots.load(BASE, fetch=lambda _url: "User-agent: *\nDisallow:\n")
    assert policy.crawl_delay is None


@pytest.mark.parametrize(
    "failure",
    [
        urllib.error.URLError("unreachable"),
        urllib.error.HTTPError("u", 404, "Not Found", {}, None),  # type: ignore[arg-type]
        OSError("connection reset"),
    ],
)
def test_an_unreachable_robots_txt_allows_everything(failure: Exception) -> None:
    """The standard says no robots.txt means no restrictions -- never abort the crawl."""

    def fetch(_url: str) -> str:
        raise failure

    policy = robots.load(BASE, fetch=fetch)
    assert policy.allows("https://example.com/anything") is True


def test_an_empty_robots_txt_allows_everything() -> None:
    assert robots.load(BASE, fetch=lambda _url: "").allows("https://example.com/x") is True


def test_a_disallow_all_blocks_everything() -> None:
    policy = robots.load(BASE, fetch=lambda _url: "User-agent: *\nDisallow: /\n")
    assert policy.allows("https://example.com/") is False


def test_allow_all_needs_no_fetch() -> None:
    assert robots.allow_all().allows("https://example.com/private/") is True
