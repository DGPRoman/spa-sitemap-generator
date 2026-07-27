"""Rendering a URL to the list of links it contains.

``Renderer`` is a two-method protocol so the crawl loop can be unit-tested against
an in-memory fake. ``ChromeRenderer`` is the only implementation that needs a
browser, and it is the only place in the package that imports Selenium.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from types import TracebackType
from typing import TYPE_CHECKING, Any, Final, Protocol, Self, cast, runtime_checkable

if TYPE_CHECKING:
    # Imported for types only: Selenium is loaded lazily at `start()` so that the
    # rest of the package -- and most of the test suite -- never pays for it.
    from selenium.webdriver.chrome.webdriver import WebDriver

log = logging.getLogger(__name__)

#: Read every href in one round trip. Doing this in JavaScript rather than
#: iterating WebElements makes StaleElementReferenceException impossible -- an SPA
#: that re-renders while we read the DOM cannot invalidate a list of plain strings.
#: Also picks up <link rel="canonical">, which is how a site tells us that two URLs
#: are the same document -- the one thing that distinguishes /products/ from
#: /products/index.html when both serve identical content.
_COLLECT_PAGE: Final = """
const canonical = document.querySelector('link[rel~="canonical" i][href]');
return {
  links: Array.from(document.querySelectorAll('a[href]')).map(a => a.href),
  canonical: canonical ? canonical.href : null
};
"""
_COUNT_ANCHORS: Final = "return document.querySelectorAll('a[href]').length;"
_READY_STATE: Final = "return document.readyState;"

_HTML_MIME_TYPES: Final = frozenset(
    {"text/html", "application/xhtml+xml", "application/xml", "text/xml", ""}
)


class RenderError(Exception):
    """A page could not be rendered.

    ``retryable`` separates "the network hiccuped, try again" from "this URL is a
    404, stop wasting navigations on it".
    """

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


class NotHtmlError(RenderError):
    """The URL served something that is not a document (so it is not a sitemap entry)."""

    def __init__(self, message: str) -> None:
        super().__init__(message, retryable=False)


@dataclass(frozen=True, slots=True)
class RenderedPage:
    """``url`` is the URL after redirects, which is not always the one requested."""

    url: str
    links: tuple[str, ...]
    canonical: str | None = None


@runtime_checkable
class Renderer(Protocol):
    """Anything that can turn a URL into the links on it."""

    def render(self, url: str) -> RenderedPage:
        """Return the rendered page, or raise ``RenderError``."""
        ...


@dataclass(slots=True)
class ChromeRenderer:
    """Headless Chrome driving a real page load, then reading the rendered DOM.

    Selenium 4.6+ resolves chromedriver itself (Selenium Manager), so nothing has
    to be installed on ``PATH`` manually.

    Waiting strategy, in order: page load (bounded by ``page_load_timeout``),
    ``document.readyState == "complete"``, an optional CSS selector, then polling
    the anchor count until it stops changing. That last step is what replaces the
    old fixed ``time.sleep(delay)``: a slow SPA gets the time it needs and a fast
    one is not punished for it.
    """

    headless: bool = True
    window_size: tuple[int, int] = (1440, 980)
    page_load_timeout: float = 30.0
    settle_timeout: float = 8.0
    settle_interval: float = 0.25
    wait_for_selector: str | None = None
    user_agent: str | None = None
    detect_http_errors: bool = True
    sleep: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = time.monotonic
    _driver: WebDriver | None = field(default=None, init=False, repr=False)

    # -- lifecycle -----------------------------------------------------------

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.stop()

    def start(self) -> None:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options

        options = Options()
        if self.headless:
            options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument(f"--window-size={self.window_size[0]},{self.window_size[1]}")
        if self.user_agent:
            options.add_argument(f"--user-agent={self.user_agent}")
        if self.detect_http_errors:
            # The WebDriver protocol exposes no status codes; the CDP performance
            # log does, and it is the only way to keep 404 pages out of a sitemap.
            options.set_capability("goog:loggingPrefs", {"performance": "ALL"})

        driver = webdriver.Chrome(options=options)
        driver.set_page_load_timeout(self.page_load_timeout)
        self._driver = driver
        log.debug("chrome started (headless=%s)", self.headless)

    def stop(self) -> None:
        if self._driver is not None:
            try:
                self._driver.quit()
            finally:
                self._driver = None

    @property
    def driver(self) -> WebDriver:
        if self._driver is None:
            raise RenderError("renderer is not started", retryable=False)
        return self._driver

    # -- rendering -----------------------------------------------------------

    def render(self, url: str) -> RenderedPage:
        from selenium.common.exceptions import TimeoutException, WebDriverException

        driver = self.driver
        self._drain_performance_log()
        before = self._current_url()

        try:
            driver.get(url)
        except TimeoutException as exc:
            raise RenderError(f"page load timed out after {self.page_load_timeout}s") from exc
        except WebDriverException as exc:
            raise RenderError(f"navigation failed: {_brief(exc)}") from exc

        self._check_navigated(url, before)
        self._check_response(url)

        try:
            self._wait_until_ready()
            if self.wait_for_selector:
                self._wait_for_selector(self.wait_for_selector)
            self._wait_until_links_settle()
            page = driver.execute_script(_COLLECT_PAGE) or {}
            final_url = driver.current_url or url
        except TimeoutException as exc:
            raise RenderError(f"page never settled: {_brief(exc)}") from exc
        except WebDriverException as exc:
            raise RenderError(f"could not read the DOM: {_brief(exc)}") from exc

        canonical = page.get("canonical")
        return RenderedPage(
            url=final_url,
            links=tuple(str(href) for href in page.get("links") or () if href),
            canonical=str(canonical) if canonical else None,
        )

    def _wait_until_ready(self) -> None:
        deadline = self.monotonic() + self.settle_timeout
        while self.monotonic() < deadline:
            if self.driver.execute_script(_READY_STATE) == "complete":
                return
            self.sleep(self.settle_interval)
        log.debug("readyState never reached 'complete'; reading the DOM anyway")

    def _wait_for_selector(self, selector: str) -> None:
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support import expected_conditions as ec
        from selenium.webdriver.support.ui import WebDriverWait

        WebDriverWait(self.driver, self.settle_timeout).until(
            ec.presence_of_element_located((By.CSS_SELECTOR, selector))
        )

    def _wait_until_links_settle(self) -> None:
        """Poll the anchor count until two consecutive samples agree."""
        deadline = self.monotonic() + self.settle_timeout
        previous = -1
        while self.monotonic() < deadline:
            count = int(self.driver.execute_script(_COUNT_ANCHORS) or 0)
            if count == previous:
                return
            previous = count
            self.sleep(self.settle_interval)
        log.debug("anchor count still changing after %.1fs", self.settle_timeout)

    def _current_url(self) -> str:
        from selenium.common.exceptions import WebDriverException

        try:
            return str(self.driver.current_url)
        except WebDriverException:
            return ""

    def _check_navigated(self, requested: str, before: str) -> None:
        """Catch a URL that triggered a download instead of a page load.

        Chrome hands ``application/pdf``, ``application/zip`` and friends to the
        download manager without leaving the current page, so ``driver.get()``
        succeeds, no Document response is logged, and the DOM we would read still
        belongs to the *previous* URL. Left undetected, that content gets
        attributed to the wrong URL.
        """
        if requested == before:
            return  # a retry of the same URL; nothing to compare against
        if self._current_url() == before:
            raise NotHtmlError("no navigation happened (the URL is a download, not a page)")

    # -- HTTP status, via the CDP performance log ----------------------------

    def _drain_performance_log(self) -> None:
        if self.detect_http_errors:
            self._performance_entries()

    def _check_response(self, url: str) -> None:
        """Raise if the main document was an error page or was not HTML.

        Best-effort by design: if the performance log is unavailable the crawl
        continues rather than failing, it just loses 404 detection.
        """
        if not self.detect_http_errors:
            return
        response = self._main_document_response()
        if response is None:
            return
        status, mime_type = response
        if status >= 400:
            raise RenderError(f"HTTP {status}", retryable=status >= 500)
        if mime_type.lower() not in _HTML_MIME_TYPES:
            raise NotHtmlError(f"not a document: {mime_type}")

    def _main_document_response(self) -> tuple[int, str] | None:
        """Status and MIME type of the main-frame document, after any redirects.

        Keyed on the request id of the *first* Document request in the log, which is
        the navigation we just triggered. Matching on "the last Document response"
        instead would pick up whatever document loaded afterwards -- an iframe, or
        Chrome's own PDF viewer -- and report its status as the page's.
        A redirect chain keeps the same request id, so the last response for that
        id is the final one.
        """
        request_id: str | None = None
        result: tuple[int, str] | None = None

        for message in self._cdp_messages():
            params = message.get("params", {})
            method = message.get("method")

            if (
                request_id is None
                and method == "Network.requestWillBeSent"
                and params.get("type") == "Document"
            ):
                request_id = params.get("requestId")
                continue

            if (
                method == "Network.responseReceived"
                and request_id is not None
                and params.get("requestId") == request_id
            ):
                response = params.get("response", {})
                status = response.get("status")
                if isinstance(status, int):
                    result = (status, str(response.get("mimeType", "")))

        return result

    def _cdp_messages(self) -> Iterator[dict[str, Any]]:
        for entry in self._performance_entries():
            try:
                yield json.loads(entry["message"])["message"]
            except (KeyError, TypeError, ValueError):
                continue

    def _performance_entries(self) -> list[dict[str, Any]]:
        try:
            # get_log carries no annotations in selenium itself.
            entries = self.driver.get_log("performance")  # type: ignore[no-untyped-call]
            return cast("list[dict[str, Any]]", entries)
        except Exception:
            log.debug("performance log unavailable; HTTP status detection is off")
            self.detect_http_errors = False
            return []


def _brief(exc: BaseException) -> str:
    """Selenium exception messages carry a stack trace; keep the first line."""
    return str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
