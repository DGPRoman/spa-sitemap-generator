"""Failure classification -- no browser needed.

The interesting renderer failures are the ones that need a dying Chrome to
reproduce: a crashed tab, a killed chromedriver, a host that stops resolving
mid-crawl. Keeping the decision in a pure function over the message text is what
makes them testable at all; the message strings below are real ones taken from
chromedriver.
"""

from __future__ import annotations

import pytest
from urllib3.exceptions import MaxRetryError

from spa_sitemap.renderer import (
    ChromeRenderer,
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


# -- a chromedriver that has exited ------------------------------------------


class _DeadDriver:
    """Stands in for a WebDriver whose chromedriver process is gone.

    Every call raises what urllib3 really raises in that situation, which is
    neither a WebDriverException nor an OSError -- so nothing in the renderer used
    to catch it and the whole crawl died on the first dead driver.
    """

    @property
    def current_url(self) -> str:
        raise MaxRetryError(None, "/session/x/url")  # type: ignore[arg-type]

    def get(self, url: str) -> None:
        raise MaxRetryError(None, "/session/x/url")  # type: ignore[arg-type]

    def get_log(self, kind: str) -> list[dict[str, object]]:
        raise MaxRetryError(None, f"/session/x/log/{kind}")  # type: ignore[arg-type]

    def execute_script(self, script: str) -> object:
        raise MaxRetryError(None, "/session/x/execute/sync")  # type: ignore[arg-type]


@pytest.mark.parametrize("detect_http_errors", [True, False])
def test_a_dead_chromedriver_is_blamed_on_the_browser(detect_http_errors: bool) -> None:
    """Both entry paths: reading the performance log, and navigating.

    The whole recovery mechanism hangs off this classification -- get it wrong and
    the crawler blames the URL, burns its attempts and eventually fails every page
    left in the frontier.
    """
    renderer = ChromeRenderer(detect_http_errors=detect_http_errors)
    renderer._driver = _DeadDriver()  # type: ignore[assignment]

    with pytest.raises(RendererUnavailableError):
        renderer.render("https://example.com/")


def test_a_dead_driver_does_not_disable_status_detection() -> None:
    """Otherwise one crash silently costs 404 detection for the rest of the crawl."""
    renderer = ChromeRenderer(detect_http_errors=True)
    renderer._driver = _DeadDriver()  # type: ignore[assignment]

    with pytest.raises(RendererUnavailableError):
        renderer.render("https://example.com/")

    assert renderer.detect_http_errors is True
