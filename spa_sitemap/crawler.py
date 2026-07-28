"""The crawl loop.

Every collaborator is injected -- store, renderer, policy, clock, sleep -- so the
whole loop, including its failure and retry paths, is unit-testable with a fake
renderer and an in-memory database, in milliseconds and with no browser.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Final, Protocol

from spa_sitemap.renderer import (
    NotHtmlError,
    Renderer,
    RenderError,
    RendererUnavailableError,
    Restartable,
    SiteUnavailableError,
)
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

    #: Browsers to burn through before giving up. A dead browser is bounded here
    #: rather than by the failure streak above, because a crash tells us nothing
    #: about the site -- but a page that reliably kills Chrome would otherwise
    #: restart it forever, and a Chrome that will not start is a machine problem
    #: that no amount of retrying fixes.
    max_restarts: int = 3

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if self.max_consecutive_failures is not None and self.max_consecutive_failures < 1:
            raise ValueError("max_consecutive_failures must be at least 1, or None to disable")
        if self.max_restarts < 0:
            raise ValueError("max_restarts must be zero or more")


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """How long a failed URL waits before it is offered again.

    Exponential, because a URL that failed twice is unlikely to succeed on the
    third try one millisecond later, and jittered so a whole wave of URLs that
    failed together does not come back in lockstep.

    ``base=0`` disables scheduling entirely, which is what the crawl loop's own
    tests want: without a wait there is nothing to wait for.
    """

    base: float = 2.0
    factor: float = 4.0
    cap: float = 300.0
    jitter: float = 0.25

    def __post_init__(self) -> None:
        if self.base < 0 or self.factor < 1 or self.cap < 0 or not 0 <= self.jitter < 1:
            raise ValueError("retry policy must be base>=0, factor>=1, cap>=0, 0<=jitter<1")

    def delay(self, attempt: int, *, spread: float = 0.5) -> float:
        """Seconds to hold a URL back after its ``attempt``-th failure.

        ``spread`` is a 0..1 sample supplied by the caller rather than drawn here,
        so the curve stays a pure function and a test can pin it exactly.
        """
        if self.base <= 0:
            return 0.0
        raw = min(self.base * self.factor ** max(attempt - 1, 0), self.cap)
        return raw * (1 + self.jitter * (2 * spread - 1))


@dataclass(slots=True)
class CrawlStats:
    visited: int = 0
    failed: int = 0
    skipped: int = 0
    redirected: int = 0
    duplicates: int = 0
    discovered: int = 0
    restarts: int = 0
    elapsed: float = 0.0
    stop_reason: str = "frontier-empty"

    def summary(self) -> str:
        # Restarts are reported because an automatic recovery is a quiet
        # degradation: a crawl that replaced Chrome forty times succeeded, and
        # without saying so it becomes a debugging black hole.
        restarts = f", {self.restarts} browser restarts" if self.restarts else ""
        return (
            f"{self.visited} visited, {self.discovered} discovered, "
            f"{self.failed} failed, {self.skipped} skipped, "
            f"{self.redirected} redirected, {self.duplicates} duplicate"
            f"{restarts} in {self.elapsed:.1f}s "
            f"({self.stop_reason})"
        )


#: Stop reasons that mean the crawl gave up rather than ran out of work. The CLI
#: turns these into a non-zero exit, because whatever is still queued is still
#: queued and a caller must not read exit 0 as "the sitemap is current".
ABORTED: Final = frozenset({"site-unreachable", "renderer-unavailable", "still-backing-off"})


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

    #: Longest single wait when every queued URL is backing off. Bounded so the
    #: termination guards and Ctrl-C stay responsive rather than being buried
    #: inside one long sleep.
    MAX_IDLE_WAIT = 5.0

    #: Consecutive idle waits before giving up on the deferred tail. A backstop
    #: against a clock that never advances -- a suspended machine, or an injected
    #: fake -- which would otherwise spin here for ever.
    MAX_IDLE_WAITS = 120

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
        retry: RetryPolicy | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        spread: Callable[[], float] = random.random,
    ) -> None:
        self.store = store
        self.renderer = renderer
        self.policy = policy
        self.limits = limits or Limits()
        self.delay = delay
        self.robots = robots
        self.respect_canonical = respect_canonical
        self.retry = retry or RetryPolicy()
        self._sleep = sleep
        self._monotonic = monotonic
        self._spread = spread
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
        idle_waits = 0
        while True:
            if (reason := self._should_stop(stats, started)) is not None:
                stats.stop_reason = reason
                break

            page = self.store.claim(max_attempts=self.limits.max_attempts)
            if page is None:
                if idle_waits < self.MAX_IDLE_WAITS and self._wait_for_due_work():
                    idle_waits += 1
                    continue
                stats.stop_reason = "frontier-empty" if idle_waits == 0 else "still-backing-off"
                break
            idle_waits = 0

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

    def _wait_for_due_work(self) -> bool:
        """Wait a little for a backing-off URL, reporting whether there was one.

        ``claim`` handing back nothing does not mean the frontier is empty -- it can
        equally mean every queued URL is serving out a backoff. Calling that
        "frontier-empty" would recreate the trap `export` and `update` fell into,
        where one command insists there is work left and the other insists there
        is none.
        """
        due_in = self.store.seconds_until_due(max_attempts=self.limits.max_attempts)
        if due_in is None:
            return False
        wait = min(due_in, self.MAX_IDLE_WAIT)
        log.info("every queued URL is backing off; waiting %.1fs", wait)
        if wait > 0:
            self._sleep(wait)
        return True

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
        """Charge the failure to whoever is actually responsible for it."""
        if isinstance(exc, NotHtmlError):
            # A definite answer about this URL -- not evidence that anything is
            # broken, so it must not count towards the circuit breaker.
            self.store.mark_skipped(page.url, str(exc))
            stats.skipped += 1
            log.info("skip: %s (%s)", page.url, exc)
            return

        # A dead browser is bounded by max_restarts instead: counting a crash here
        # as well would let one Chrome death trip two independent guards, and the
        # failure streak is meant to measure the *site* going quiet.
        if not isinstance(exc, RendererUnavailableError):
            self._consecutive_failures += 1
        tripped = self._breaker_tripped()

        if isinstance(exc, (SiteUnavailableError, RendererUnavailableError)):
            # Neither the site vanishing nor the browser dying says anything about
            # this URL, so the attempt `claim` charged for it up front is refunded
            # and the row goes back untouched. Letting that charge stand is exactly
            # how an outage exhausts real pages and drops them from the sitemap.
            self.store.release(page.url, str(exc))
            if isinstance(exc, RendererUnavailableError):
                # No backoff: a replacement browser is ready immediately, and this
                # URL has not been given a real chance yet.
                log.warning("not %s's fault, leaving it queued: %s", page.url, exc)
                self._recover_renderer(stats)
            else:
                # Paced, not escalating. Deciding when to give up on a site is the
                # breaker's job, so this only has to avoid hammering -- and an
                # exponential curve here would fight it: measured against a real
                # dead server, backing off by the failure streak hit the 300s cap
                # within a handful of failures and left the run stalling for
                # minutes before abandoning a site it already knew was unreachable.
                self._defer(page.url, exc, attempt=1)
        elif tripped or (exc.retryable and page.attempts < self.limits.max_attempts):
            # On the way out, keep the URL queued rather than failing it: the whole
            # point of stopping is that `update` finds a frontier to resume.
            if tripped:
                self.store.requeue(page.url, str(exc))
            else:
                log.warning(
                    "retry %d/%d: %s (%s)", page.attempts, self.limits.max_attempts, page.url, exc
                )
                self._defer(page.url, exc, attempt=page.attempts)
        else:
            self.store.mark_failed(page.url, str(exc))
            stats.failed += 1
            log.warning("failed: %s (%s)", page.url, exc)

        if tripped:
            log.error(
                "%d failures in a row, the last on %s (%s) -- abandoning this run "
                "with the frontier intact; run `update` to continue",
                self._consecutive_failures, page.url, exc,
            )
            self.request_stop("site-unreachable")

    def _defer(self, url: str, exc: RenderError, *, attempt: int) -> None:
        """Hold a URL back, so the loop moves on instead of retrying it at once."""
        wait = self.retry.delay(attempt, spread=self._spread())
        self.store.defer(url, str(exc), wait)
        if wait > 0:
            log.debug("holding %s back for %.1fs", url, wait)

    def _recover_renderer(self, stats: CrawlStats) -> None:
        """Replace a dead browser, or end the run if that is not possible.

        Without this, one dead chromedriver failed every remaining URL: each got
        ``max_attempts`` futile navigations and then ``failed``, and since only
        ``queued`` rows are ever claimed, `update` could not get them back.
        """
        if not isinstance(self.renderer, Restartable):
            log.error("this renderer cannot be restarted; ending the run")
            self.request_stop("renderer-unavailable")
            return

        if stats.restarts >= self.limits.max_restarts:
            log.error(
                "the browser has died %d times already; ending the run", stats.restarts
            )
            self.request_stop("renderer-unavailable")
            return

        try:
            self.renderer.restart()
        except Exception as exc:
            log.error("could not restart the browser: %s; ending the run", exc)
            self.request_stop("renderer-unavailable")
            return

        stats.restarts += 1
        log.warning("browser restarted (%d/%d)", stats.restarts, self.limits.max_restarts)

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
