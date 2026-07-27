"""Command-line interface.

The original three commands are unchanged -- ``new``, ``update``, ``export`` -- with
``status`` added because "how far along is this crawl?" is the question you actually
have while one is running.

Exit codes: 0 success, 1 error, 2 bad usage (argparse), 130 interrupted.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from types import FrameType
from typing import cast

from spa_sitemap import __version__
from spa_sitemap.config import DEFAULT_CONFIG_PATH, Config, ConfigError
from spa_sitemap.crawler import Crawler, Limits
from spa_sitemap.renderer import ChromeRenderer
from spa_sitemap.robots import Robots, allow_all
from spa_sitemap.robots import load as load_robots
from spa_sitemap.sitemap import SitemapError, SitemapUrl, entries, write_sitemap
from spa_sitemap.store import Status, UrlStore

log = logging.getLogger("spa_sitemap")

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_INTERRUPTED = 130


# -- argument parsing --------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="spa-sitemap",
        description="Crawl a JavaScript-rendered site with a real browser and "
        "generate sitemap.xml.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "-c", "--config", type=Path, default=None,
        help=f"config file; must exist if given. Without it, {DEFAULT_CONFIG_PATH} "
             f"is used when present, otherwise --url alone is enough",
    )
    common.add_argument("--database", type=Path, dest="database_path", help="SQLite file to use")
    common.add_argument(
        "--url", dest="base_url",
        help="the site to crawl; overrides base_url from the config file",
    )
    verbosity = common.add_mutually_exclusive_group()
    verbosity.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    verbosity.add_argument("-q", "--quiet", action="store_true", help="warnings and errors only")

    crawl_opts = argparse.ArgumentParser(add_help=False)
    crawl_opts.add_argument("--delay", type=float, help="seconds to wait between pages")
    crawl_opts.add_argument("--max-pages", type=int, dest="max_pages", help="stop after N pages")
    crawl_opts.add_argument("--max-depth", type=int, dest="max_depth", help="stop at depth N")
    crawl_opts.add_argument(
        "--max-runtime", type=float, dest="max_runtime", help="stop after N seconds"
    )
    crawl_opts.add_argument(
        "--no-headless", dest="headless", action="store_false", default=None,
        help="show the browser window (for debugging)",
    )
    crawl_opts.add_argument(
        "--ignore-robots", dest="respect_robots", action="store_false", default=None,
        help="crawl URLs that robots.txt disallows",
    )
    crawl_opts.add_argument(
        "--ignore-canonical", dest="respect_canonical", action="store_false", default=None,
        help="do not collapse duplicates onto their rel=canonical URL",
    )
    crawl_opts.add_argument(
        "--wait-for", dest="wait_for_selector",
        help="CSS selector to wait for on every page before reading links",
    )

    export_opts = argparse.ArgumentParser(add_help=False)
    export_opts.add_argument(
        "-o", "--output", type=Path, dest="output_path", help="sitemap file to write"
    )
    export_opts.add_argument(
        "--lastmod", metavar="WHEN",
        help="add <lastmod>: 'visited' (when each page was crawled), 'today', "
             "or a fixed YYYY-MM-DD",
    )
    export_opts.add_argument(
        "--allow-empty", action="store_true",
        help="write a sitemap even when no pages were crawled (default: fail)",
    )

    commands = parser.add_subparsers(title="commands", dest="command", required=True)
    new_parser = commands.add_parser(
        "new", parents=[common, crawl_opts],
        help="discard existing data and crawl from scratch",
    )
    new_parser.add_argument(
        "-y", "--yes", action="store_true",
        help="do not ask before discarding an existing crawl",
    )
    new_parser.set_defaults(func=cmd_new)
    commands.add_parser(
        "update", parents=[common, crawl_opts],
        help="resume a crawl, visiting whatever is still queued",
    ).set_defaults(func=cmd_update)
    commands.add_parser(
        "export", parents=[common, export_opts],
        help="write the crawled URLs to sitemap.xml",
    ).set_defaults(func=cmd_export)
    commands.add_parser(
        "status", parents=[common], help="show crawl progress"
    ).set_defaults(func=cmd_status)

    return parser


def _config_from_args(args: argparse.Namespace) -> Config:
    overrides = {
        name: getattr(args, name, None)
        for name in (
            "base_url", "delay", "headless", "respect_robots", "respect_canonical",
            "wait_for_selector",
            "max_pages", "max_depth", "max_runtime", "database_path", "output_path",
        )
    }
    # A named -c must exist; the default config.json is used only if it is there,
    # so `--url ...` works on its own with no config file at all.
    return Config.from_sources(args.config, must_exist=args.config is not None, **overrides)


#: Sentinel for ``--lastmod visited``: take the date from each page's own visit.
PER_PAGE_LASTMOD = "visited"


def _parse_lastmod(value: str | None) -> date | str | None:
    """``None`` = no lastmod, ``PER_PAGE_LASTMOD`` = per page, otherwise a fixed date."""
    if value is None:
        return None
    if value.lower() == PER_PAGE_LASTMOD:
        return PER_PAGE_LASTMOD
    if value.lower() == "today":
        return date.today()
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ConfigError(
            f"--lastmod must be 'visited', 'today' or YYYY-MM-DD, got {value!r}"
        ) from exc


# -- commands ----------------------------------------------------------------


def cmd_new(args: argparse.Namespace) -> int:
    config = _config_from_args(args)
    base_url = config.require_base_url()
    with UrlStore(config.database_path) as store:
        known = store.total()
        if known and not args.yes and not _confirm_discard(config, known):
            log.info("cancelled; use `update` to resume the existing crawl")
            return EXIT_OK

        log.info("clearing %s", config.database_path)
        store.reset()
        store.set_meta("base_url", base_url)
        return _run_crawl(config, store, seeds=[base_url])


def _confirm_discard(config: Config, known: int) -> bool:
    """`new` throws away real work, so ask -- unless there is nobody to ask."""
    if not sys.stdin.isatty():
        return True
    answer = input(
        f"{config.database_path} already holds {known} URLs. Discard them? [y/N] "
    )
    return answer.strip().lower() in {"y", "yes"}


def _with_stored_base_url(config: Config) -> Config:
    """Fall back to the site the database was crawled with.

    `update` resumes an existing crawl, so the target is already recorded in the
    database -- asking for it again on the command line would be busywork.
    """
    if config.base_url:
        return config
    with UrlStore(config.database_path) as store:
        stored = store.get_meta("base_url")
    return replace(config, base_url=stored) if stored else config


def cmd_update(args: argparse.Namespace) -> int:
    config = _config_from_args(args)
    # Resuming needs a target, but the database already recorded one, so the
    # config only has to supply it when it disagrees or is absent.
    config = _with_stored_base_url(config)
    base_url = config.require_base_url()
    with UrlStore(config.database_path) as store:
        if store.total() == 0:
            log.error("nothing to resume in %s -- run `new` first", config.database_path)
            return EXIT_ERROR

        previous = store.get_meta("base_url")
        if previous and previous != base_url:
            log.error(
                "%s was crawled with base_url %s but the config says %s. "
                "Run `new` to start over, or point --url at the original site.",
                config.database_path, previous, base_url,
            )
            return EXIT_ERROR

        if not store.has_pending(max_attempts=config.max_attempts):
            # Nothing queued: report and exit without paying for a browser start.
            log.info("frontier is empty; nothing left to crawl. Counts: %s", store.counts())
            log.info("run `export` to write the sitemap, or `new` to crawl again")
            return EXIT_OK

        return _run_crawl(config, store, seeds=[base_url])


def cmd_export(args: argparse.Namespace) -> int:
    config = _config_from_args(args)
    lastmod = _parse_lastmod(args.lastmod)

    with UrlStore(config.database_path) as store:
        entries = _sitemap_entries(store, lastmod)
        pending = store.counts()[Status.QUEUED]

        if not entries and not args.allow_empty:
            # Publishing an empty sitemap tells search engines the site has no
            # pages, so this is a failure, not a success with a warning.
            log.error(
                "no successfully crawled pages in %s -- run `new` first "
                "(or pass --allow-empty to write an empty sitemap anyway)",
                config.database_path,
            )
            return EXIT_ERROR

        if pending:
            log.warning(
                "%d URLs are still queued -- this sitemap is incomplete; "
                "run `update` to finish the crawl",
                pending,
            )

        result = write_sitemap(entries, config.output_path, base_url=config.base_url)

    log.info("export complete: %s", result.describe())
    return EXIT_OK


def _sitemap_entries(store: UrlStore, lastmod: date | str | None) -> list[SitemapUrl]:
    if lastmod == PER_PAGE_LASTMOD:
        return [SitemapUrl(loc=url, lastmod=visited) for url, visited in store.visited_entries()]
    return entries(store.visited_urls(), lastmod if isinstance(lastmod, date) else None)


def cmd_status(args: argparse.Namespace) -> int:
    config = _config_from_args(args)
    with UrlStore(config.database_path) as store:
        counts = store.counts()
        total = store.total()
        problems = store.problems(limit=10)
        crawled_site = store.get_meta("base_url")

    print(f"database : {config.database_path}")
    print(f"base URL : {config.base_url or crawled_site or '(none recorded)'}")
    print(f"known    : {total} URLs")
    for status, count in counts.items():
        print(f"  {status:<11} {count}")
    if problems:
        print("\nrecent problems:")
        for status, url, note in problems:
            print(f"  [{status}] {url} -- {note}")
    return EXIT_OK


# -- crawl wiring ------------------------------------------------------------


def _run_crawl(config: Config, store: UrlStore, *, seeds: Sequence[str]) -> int:
    policy = config.policy()
    robots = _load_robots(config)
    delay = _effective_delay(config, robots)

    limits = Limits(
        max_pages=config.max_pages,
        max_depth=config.max_depth,
        max_runtime=config.max_runtime,
        max_attempts=config.max_attempts,
    )

    renderer = ChromeRenderer(
        headless=config.headless,
        window_size=config.window_size,
        page_load_timeout=config.page_load_timeout,
        settle_timeout=config.settle_timeout,
        wait_for_selector=config.wait_for_selector,
        user_agent=config.user_agent,
    )

    crawler = Crawler(
        store=store,
        renderer=renderer,
        policy=policy,
        limits=limits,
        delay=delay,
        robots=robots if config.respect_robots else None,
        respect_canonical=config.respect_canonical,
    )

    log.info("crawling %s (scope %s%s)", config.require_base_url(), policy.scope.origin,
             policy.scope.path_prefix)
    with renderer, graceful_interrupt(crawler):
        stats = crawler.crawl(seeds)

    log.info("crawl finished: %s", stats.summary())
    log.info("counts: %s", store.counts())
    return EXIT_INTERRUPTED if stats.stop_reason == "interrupted" else EXIT_OK


def _load_robots(config: Config) -> Robots:
    if not config.respect_robots:
        log.warning("robots.txt is being ignored (--ignore-robots)")
        return allow_all(config.user_agent)
    return load_robots(config.require_base_url(), user_agent=config.user_agent)


def _effective_delay(config: Config, robots: Robots) -> float:
    """Honour a Crawl-delay directive when it asks for more patience than we planned."""
    if robots.crawl_delay and robots.crawl_delay > config.delay:
        log.info("robots.txt asks for a %.1fs crawl-delay", robots.crawl_delay)
        return robots.crawl_delay
    return config.delay


@contextmanager
def graceful_interrupt(crawler: Crawler) -> Iterator[None]:
    """First Ctrl-C finishes the current page and commits; a second one aborts."""

    def handler(signum: int, frame: FrameType | None) -> None:
        log.warning("interrupted -- finishing the current page, press Ctrl-C again to abort")
        signal.signal(signal.SIGINT, signal.default_int_handler)
        crawler.request_stop("interrupted")

    try:
        previous = signal.signal(signal.SIGINT, handler)
    except ValueError:
        # Not on the main thread (e.g. under a test runner); no handler to install.
        yield
        return

    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)


# -- entry point -------------------------------------------------------------


def configure_logging(*, verbose: bool = False, quiet: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.WARNING if quiet else logging.INFO
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S"))
    root = logging.getLogger("spa_sitemap")
    root.handlers.clear()  # idempotent: repeated calls must not duplicate output
    root.addHandler(handler)
    root.setLevel(level)
    root.propagate = False


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(verbose=args.verbose, quiet=args.quiet)

    started = time.monotonic()
    try:
        return cast("int", args.func(args))
    except (ConfigError, SitemapError) as exc:
        log.error("%s", exc)
        return EXIT_ERROR
    except KeyboardInterrupt:
        log.warning("aborted")
        return EXIT_INTERRUPTED
    except Exception as exc:
        log.error("unexpected failure: %s", exc, exc_info=args.verbose)
        return EXIT_ERROR
    finally:
        log.info("took %.2fs", time.monotonic() - started)


if __name__ == "__main__":
    sys.exit(main())
