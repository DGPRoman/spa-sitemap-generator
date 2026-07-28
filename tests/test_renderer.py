"""Failure classification -- no browser needed.

The interesting renderer failures are the ones that need a dying Chrome to
reproduce: a crashed tab, a killed chromedriver, a host that stops resolving
mid-crawl. Keeping the decision in a pure function over the message text is what
makes them testable at all; the message strings below are real ones taken from
chromedriver.
"""

from __future__ import annotations

import pytest

from spa_sitemap.renderer import (
    NotHtmlError,
    RenderError,
    RendererUnavailableError,
    SiteUnavailableError,
    classify,
    status_is_retryable,
)

# -- who is at fault ---------------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        "Message: invalid session id",
        "Message: chrome not reachable",
        "Message: disconnected: not connected to DevTools",
        "Message: unknown error: session deleted because of page crash",
        "Message: unknown error: cannot determine loading status from tab crashed",
        "Message: no such window: target window already closed",
    ],
)
def test_a_dead_browser_is_the_browsers_fault(message: str) -> None:
    """These fail *every* subsequent render, so blaming the URL is destructive."""
    assert classify(message) is RendererUnavailableError


@pytest.mark.parametrize(
    "message",
    [
        "Message: unknown error: net::ERR_CONNECTION_REFUSED",
        "Message: unknown error: net::ERR_NAME_NOT_RESOLVED",
        "Message: unknown error: net::ERR_INTERNET_DISCONNECTED",
        "Message: unknown error: net::ERR_CONNECTION_RESET",
        "Message: unknown error: net::ERR_CONNECTION_TIMED_OUT",
        "Message: unknown error: net::ERR_CERT_DATE_INVALID",
        "Message: unknown error: net::ERR_SSL_PROTOCOL_ERROR",
        "Message: unknown error: net::ERR_PROXY_CONNECTION_FAILED",
    ],
)
def test_network_and_certificate_problems_are_the_sites_fault(message: str) -> None:
    """None of these say anything about the URL we asked for."""
    assert classify(message) is SiteUnavailableError


@pytest.mark.parametrize(
    "message",
    [
        "Message: javascript error: something threw",
        "Message: stale element reference",
        "Message: unknown error: something nobody has seen before",
        "",
    ],
)
def test_anything_unrecognised_stays_the_urls_problem(message: str) -> None:
    """An unknown failure must degrade to today's behaviour, not to a restart loop."""
    assert classify(message) is RenderError


def test_classification_is_case_insensitive() -> None:
    assert classify("NET::err_connection_refused") is SiteUnavailableError
    assert classify("Invalid Session Id") is RendererUnavailableError


def test_a_browser_fault_outranks_a_site_fault() -> None:
    """A crashed tab reported alongside a net error is still a crashed tab."""
    message = (
        "Message: unknown error: session deleted because of page crash "
        "net::ERR_EMPTY_RESPONSE"
    )
    assert classify(message) is RendererUnavailableError


def test_every_kind_is_still_a_render_error() -> None:
    """`except RenderError` appears throughout the crawl loop and must keep working."""
    for kind in (NotHtmlError, SiteUnavailableError, RendererUnavailableError):
        assert issubclass(kind, RenderError)


def test_only_not_html_is_non_retryable_by_default() -> None:
    assert NotHtmlError("not a document: application/zip").retryable is False
    assert SiteUnavailableError("net::ERR_CONNECTION_RESET").retryable is True
    assert RendererUnavailableError("invalid session id").retryable is True


# -- HTTP status -------------------------------------------------------------


@pytest.mark.parametrize("status", [408, 425, 429, 500, 502, 503, 504])
def test_a_busy_server_is_worth_another_attempt(status: int) -> None:
    """429 used to be permanent, so a rate-limiting site truncated the sitemap."""
    assert status_is_retryable(status)


@pytest.mark.parametrize("status", [400, 401, 403, 404, 410, 451])
def test_a_bad_request_is_not_worth_another_navigation(status: int) -> None:
    assert not status_is_retryable(status)
