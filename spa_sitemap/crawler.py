"""The crawl loop.

Every collaborator is injected -- store, renderer, policy, clock, sleep -- so the
whole loop, including its failure and retry paths, is unit-testable with a fake
renderer and an in-memory database, in milliseconds and with no browser.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Protocol

from spa_sitemap.renderer import NotHtmlError, Renderer, RenderError
from spa_sitemap.store import Page, UrlStore
from spa_sitemap.urls import UrlPolicy

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Limits:
    """Termination guards. Without these a crawl of a calendar widget never ends."""

    max_pages: int | None = None
    max_depth: int | None = None
    max_runtime: float | None = None
    max_attempts: int = 3

    #: Render failures in a row, with no success in between, before the whole run
    #: is abandoned. ``max_attempts`` bounds how often we retry *one* URL; this
    #: bounds how long we keep trying when the problem is not the URL at all. A
    #: dead browser or an unreachable site fails every page it is handed, so
    #: without this guard an outage walks the entire frontier and converts it into
    #: permanent failures at full speed. ``None`` disables the breaker.
    max_consecutive_failures: int | None = 10

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if self.max_consecutive_failures is not None and self.max_consecutive_failures < 1:
            raise ValueError("max_consecutive_failures must be at least 1, or None to disable")


@dataclass(slots=True)
class CrawlStats:
    visited: int = 0
    failed: int = 0
    skipped: int = 0
    redirected: int = 0
    duplicates: int = 0
    discovered: int = 0
    elapsed: float = 0.0
    stop_reason: str = "frontier-empty"

    def summary(self) -> str:
        return (
            f"{self.visited} visited, {self.discovered} discovered, "
            f"{self.failed} failed, {self.skipped} skipped, "
            f"{self.redirected} redirected, {self.duplicates} duplicate in "
            f"{self.elapsed:.1f}s "
            f"({self.stop_reason})"
        )


class RobotsPolicy(Protocol):
    """Minimal robots.txt interface (see ``spa_sitemap.robots``)."""

    def allows(self, url: str) -> bool: ...


class Crawler:
    """Walks the frontier one page at a time with a single browser.

    Sequential and single-browser on purpose: the bottleneck is page *rendering*,
    parallel Chrome instances multiply memory by N, and a polite crawler wants a
    delay between requests anyway -- which is the opposite of concurrency.
    """

    PROGRESS_EVERY = 25

    def __init__(
        self,
        *,
        store: UrlStore,
        renderer: Renderer,
        policy: UrlPolicy,
        limits: Limits | None = None,
        delay: float = 0.0,
        robots: RobotsPolicy | None = None,
        respect_canonical: bool = True,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.store = store
        self.renderer = renderer
        self.policy = policy
        self.limits = limits or Limits()
        self.delay = delay
        self.robots = robots
        self.respect_canonical = respect_canonical
        self._sleep = sleep
        self._monotonic = monotonic
        self._stop_reason: str | None = None
        self._consecutive_failures = 0

    def request_stop(self, reason: str = "interrupted") -> None:
        """Ask the loop to finish after the current page. Safe from a signal handler."""
        if self._stop_reason is None:
            self._stop_reason = reason

    def crawl(self, seeds: Iterable[str] = ()) -> CrawlStats:
        stats = CrawlStats()
        started = self._monotonic()

        seed_urls = self._canonical_seeds(seeds)
        if seed_urls:
            stats.discovered += self.store.enqueue(seed_urls, depth=0)

        first = True
        while True:
            if (reason := self._should_stop(stats, started)) is not None:
                stats.stop_reason = reason
                break

            page = self.store.claim(max_attempts=self.limits.max_attempts)
            if page is None:
                stats.stop_reason = "frontier-empty"
                break

            if self.robots is not None and not self.robots.allows(page.url):
                self.store.mark_skipped(page.url, "disallowed by robots.txt")
                stats.skipped += 1
                log.info("skip (robots.txt): %s", page.url)
                continue

            if not first and self.delay > 0:
                self._sleep(self.delay)
            first = False

            self._visit(page, stats)

            if stats.visited and stats.visited % self.PROGRESS_EVERY == 0:
                log.info("progress: %s", self.store.counts())

        stats.elapsed = self._monotonic() - started
        return stats

    # -- one page ------------------------------------------------------------

    def _visit(self, page: Page, stats: CrawlStats) -> None:
        log.info("render [depth %d] %s", page.depth, page.url)
        try:
            rendered = self.renderer.render(page.url)
        except RenderError as exc:
            self._handle_failure(page, exc, stats)
            return

        # One page rendering proves the browser is alive and the site is answering,
        # which is precisely what the breaker below is counting the absence of.
        self._consecutive_failures = 0

        owner = self._resolve_redirect(page, rendered.url, stats)
        if owner is None:
            return

        # Links are harvested regardless of which URL ends up owning the page, so a
        # non-canonical duplicate still contributes its discoveries to the frontier.
        links = self.policy.normalise_all(rendered.links, page_url=rendered.url)
        if self.limits.max_depth is None or page.depth < self.limits.max_depth:
            stats.discovered += self.store.enqueue(links, depth=page.depth + 1)

        owner = self._resolve_canonical(owner, rendered.canonical, page.depth, stats)

        if self.store.mark_done(owner, link_count=len(links)):
            stats.visited += 1

    def _resolve_canonical(
        self, owner: str, canonical: str | None, depth: int, stats: CrawlStats
    ) -> str:
        """Honour ``<link rel="canonical">`` when it names a different in-scope URL.

        This is the only way to tell that ``/products/`` and ``/products/index.html``
        are one document: the server returns 200 for both, so nothing in the HTTP
        exchange reveals the duplication. Self-referencing and out-of-scope canonicals
        are ignored.
        """
        if not self.respect_canonical or not canonical:
            return owner

        target = self.policy.normalise(canonical, page_url=owner)
        if target is None or target == owner:
            return owner

        log.info("canonical: %s -> %s", owner, target)
        self.store.mark_duplicate(owner, target)
        stats.duplicates += 1
        self.store.enqueue([target], depth=depth)
        return target

    def _resolve_redirect(self, page: Page, final_url: str, stats: CrawlStats) -> str | None:
        """Return the URL the rendered content belongs to, or ``None`` to stop here.

        A redirect means the requested URL is not itself a page. Recording it as
        ``redirected`` and attributing the content to the target is what keeps
        ``/about`` and ``/about/`` from both appearing in the sitemap.
        """
        if final_url == page.url:
            return page.url

        target = self.policy.normalise(final_url, page_url=page.url)
        self.store.mark_redirected(page.url, target or final_url)
        stats.redirected += 1

        if target is None or target == page.url:
            log.info("redirect out of scope: %s -> %s", page.url, final_url)
            return None

        log.info("redirect: %s -> %s", page.url, target)
        # Make sure the target has a row, then let it own the content we already
        # rendered -- re-fetching it would be a wasted browser navigation.
        self.store.enqueue([target], depth=page.depth)
        return target

    def _handle_failure(self, page: Page, exc: RenderError, stats: CrawlStats) -> None:
        if isinstance(exc, NotHtmlError):
            # A definite answer about this URL -- not evidence that anything is
            # broken, so it must not count towards the circuit breaker.
            self.store.mark_skipped(page.url, str(exc))
            stats.skipped += 1
            log.info("skip: %s (%s)", page.url, exc)
            return

        self._consecutive_failures += 1
        if self._breaker_tripped():
            # Leave this URL queued rather than failing it: the point of stopping
            # is that the frontier survives the outage intact, so `update` has
            # something to resume once the site (or the browser) comes back.
            self.store.requeue(page.url, str(exc))
            log.error(
                "%d failures in a row, the last on %s (%s) -- abandoning this run "
                "with the frontier intact; run `update` to continue",
                self._consecutive_failures, page.url, exc,
            )
            self.request_stop("site-unreachable")
            return

        if exc.retryable and page.attempts < self.limits.max_attempts:
            self.store.requeue(page.url, str(exc))
            log.warning(
                "retry %d/%d: %s (%s)", page.attempts, self.limits.max_attempts, page.url, exc
            )
        else:
            self.store.mark_failed(page.url, str(exc))
            stats.failed += 1
            log.warning("failed: %s (%s)", page.url, exc)

    def _breaker_tripped(self) -> bool:
        limit = self.limits.max_consecutive_failures
        return limit is not None and self._consecutive_failures >= limit

    # -- control -------------------------------------------------------------

    def _should_stop(self, stats: CrawlStats, started: float) -> str | None:
        if self._stop_reason is not None:
            return self._stop_reason
        limits = self.limits
        if limits.max_pages is not None and stats.visited >= limits.max_pages:
            return "max-pages"
        if limits.max_runtime is not None and self._monotonic() - started >= limits.max_runtime:
            return "max-runtime"
        return None

    def _canonical_seeds(self, seeds: Iterable[str]) -> list[str]:
        urls: list[str] = []
        for seed in seeds:
            canonical = self.policy.normalise(seed, page_url=seed)
            if canonical is None:
                log.warning("seed is outside the crawl scope, ignoring: %s", seed)
                continue
            urls.append(canonical)
        return urls
