"""The crawl loop, driven by a fake renderer.

No browser, no network, no real sleeping -- which is the point of injecting the
renderer and the clock. The old crawl logic could not be exercised at all without
launching Chrome.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from spa_sitemap.crawler import Crawler, Limits
from spa_sitemap.renderer import NotHtmlError, RenderedPage, RenderError
from spa_sitemap.store import Status, UrlStore
from spa_sitemap.urls import UrlPolicy

BASE = "https://example.com/"


class FakeRenderer:
    """Serves a static link graph. ``failures`` maps a URL to an exception to raise."""

    def __init__(
        self,
        graph: dict[str, list[str]],
        *,
        failures: dict[str, BaseException] | None = None,
        redirects: dict[str, str] | None = None,
    ) -> None:
        self.graph = graph
        self.failures = failures or {}
        self.redirects = redirects or {}
        self.rendered: list[str] = []

    def render(self, url: str) -> RenderedPage:
        self.rendered.append(url)
        if (failure := self.failures.get(url)) is not None:
            raise failure
        final = self.redirects.get(url, url)
        return RenderedPage(url=final, links=tuple(self.graph.get(final, [])))


class FlakyRenderer:
    """Fails a URL the first ``fail_times`` times, then succeeds."""

    def __init__(self, url: str, fail_times: int) -> None:
        self.url = url
        self.fail_times = fail_times
        self.calls = 0

    def render(self, url: str) -> RenderedPage:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RenderError("connection reset")
        return RenderedPage(url=url, links=())


@pytest.fixture
def store() -> Iterator[UrlStore]:
    with UrlStore(":memory:") as store:
        yield store


def make_crawler(store: UrlStore, renderer: object, **kwargs: object) -> Crawler:
    """A crawler whose clock and sleep never touch real time."""
    ticks = iter(range(0, 10_000))
    return Crawler(
        store=store,
        renderer=renderer,  # type: ignore[arg-type]
        policy=kwargs.pop("policy", None) or UrlPolicy.build(BASE),
        sleep=lambda _seconds: None,
        monotonic=lambda: float(next(ticks)),
        **kwargs,  # type: ignore[arg-type]
    )


# -- the happy path ----------------------------------------------------------


def test_crawls_a_whole_graph_once_each(store: UrlStore) -> None:
    renderer = FakeRenderer(
        {
            BASE: ["/a", "/b"],
            "https://example.com/a": ["/c", "/"],
            "https://example.com/b": ["/c"],
            "https://example.com/c": ["/a"],
        }
    )
    stats = make_crawler(store, renderer).crawl([BASE])

    assert stats.visited == 4
    assert stats.stop_reason == "frontier-empty"
    assert sorted(renderer.rendered) == sorted(set(renderer.rendered))  # no page twice
    assert set(store.visited_urls()) == {
        BASE, "https://example.com/a", "https://example.com/b", "https://example.com/c"
    }


def test_cycles_terminate(store: UrlStore) -> None:
    renderer = FakeRenderer({BASE: ["/a"], "https://example.com/a": ["/", "/a"]})
    stats = make_crawler(store, renderer).crawl([BASE])
    assert stats.visited == 2


def test_off_scope_and_asset_links_are_never_rendered(store: UrlStore) -> None:
    renderer = FakeRenderer(
        {BASE: ["https://other.example/x", "/doc.pdf", "mailto:a@b.c", "/real"]}
    )
    make_crawler(store, renderer).crawl([BASE])

    assert renderer.rendered == [BASE, "https://example.com/real"]


def test_a_seed_outside_the_scope_is_refused(store: UrlStore) -> None:
    renderer = FakeRenderer({})
    stats = make_crawler(store, renderer).crawl(["https://elsewhere.example/"])

    assert stats.visited == 0
    assert renderer.rendered == []


def test_empty_page_is_still_a_visited_page(store: UrlStore) -> None:
    renderer = FakeRenderer({BASE: []})
    stats = make_crawler(store, renderer).crawl([BASE])
    assert stats.visited == 1
    assert list(store.visited_urls()) == [BASE]


# -- limits ------------------------------------------------------------------


def test_max_pages_stops_the_crawl(store: UrlStore) -> None:
    renderer = FakeRenderer({BASE: ["/a", "/b", "/c"], **{
        f"https://example.com/{n}": ["/"] for n in "abc"
    }})
    stats = make_crawler(store, renderer, limits=Limits(max_pages=2)).crawl([BASE])

    assert stats.visited == 2
    assert stats.stop_reason == "max-pages"
    assert store.counts()[Status.QUEUED] > 0  # the rest stays resumable


def test_max_depth_stops_discovery_but_finishes_the_level(store: UrlStore) -> None:
    renderer = FakeRenderer(
        {
            BASE: ["/a"],
            "https://example.com/a": ["/b"],
            "https://example.com/b": ["/c"],
        }
    )
    stats = make_crawler(store, renderer, limits=Limits(max_depth=1)).crawl([BASE])

    assert stats.visited == 2
    assert set(store.visited_urls()) == {BASE, "https://example.com/a"}
    assert "https://example.com/b" not in set(store.visited_urls())


def test_max_runtime_stops_the_crawl(store: UrlStore) -> None:
    renderer = FakeRenderer({BASE: [f"/{n}" for n in range(50)]})
    stats = make_crawler(store, renderer, limits=Limits(max_runtime=3)).crawl([BASE])

    assert stats.stop_reason == "max-runtime"
    assert stats.visited < 50


def test_request_stop_ends_the_crawl_gracefully(store: UrlStore) -> None:
    """What Ctrl-C does: finish the current page, keep the frontier resumable."""
    renderer = FakeRenderer(
        {BASE: ["/a", "/b"], "https://example.com/a": [], "https://example.com/b": []}
    )
    crawler = make_crawler(store, renderer)
    crawler.request_stop()
    stats = crawler.crawl([BASE])

    assert stats.visited == 0
    assert stats.stop_reason == "interrupted"
    assert store.counts()[Status.QUEUED] == 1


def test_limits_reject_a_nonsensical_attempt_count() -> None:
    with pytest.raises(ValueError, match="max_attempts"):
        Limits(max_attempts=0)


# -- failure handling --------------------------------------------------------


def test_one_failure_does_not_abort_the_crawl(store: UrlStore) -> None:
    """The old loop propagated the first exception and lost the whole frontier."""
    renderer = FakeRenderer(
        {BASE: ["/bad", "/good"], "https://example.com/good": []},
        failures={"https://example.com/bad": RenderError("HTTP 500")},
    )
    stats = make_crawler(store, renderer, limits=Limits(max_attempts=1)).crawl([BASE])

    assert stats.visited == 2  # the seed and /good
    assert stats.failed == 1
    assert "https://example.com/good" in set(store.visited_urls())


def test_a_transient_failure_is_retried_then_succeeds(store: UrlStore) -> None:
    renderer = FlakyRenderer(BASE, fail_times=2)
    stats = make_crawler(store, renderer, limits=Limits(max_attempts=3)).crawl([BASE])

    assert renderer.calls == 3
    assert stats.visited == 1
    assert stats.failed == 0


def test_retries_are_bounded(store: UrlStore) -> None:
    renderer = FlakyRenderer(BASE, fail_times=99)
    stats = make_crawler(store, renderer, limits=Limits(max_attempts=3)).crawl([BASE])

    assert renderer.calls == 3
    assert stats.failed == 1
    assert store.counts()[Status.FAILED] == 1
    assert store.counts()[Status.QUEUED] == 0


def test_a_404_is_not_retried(store: UrlStore) -> None:
    renderer = FakeRenderer(
        {BASE: ["/gone"]},
        failures={"https://example.com/gone": RenderError("HTTP 404", retryable=False)},
    )
    stats = make_crawler(store, renderer, limits=Limits(max_attempts=3)).crawl([BASE])

    assert renderer.rendered.count("https://example.com/gone") == 1
    assert stats.failed == 1
    assert "https://example.com/gone" not in set(store.visited_urls())


def test_a_non_html_response_is_skipped_not_failed(store: UrlStore) -> None:
    renderer = FakeRenderer(
        {BASE: ["/download"]},
        failures={"https://example.com/download": NotHtmlError("not a document: application/zip")},
    )
    stats = make_crawler(store, renderer).crawl([BASE])

    assert stats.skipped == 1
    assert stats.failed == 0
    assert "https://example.com/download" not in set(store.visited_urls())


# -- redirects ---------------------------------------------------------------


def test_a_redirect_attributes_the_page_to_its_target(store: UrlStore) -> None:
    """`/about` -> `/about/` must produce one sitemap entry, not two."""
    renderer = FakeRenderer(
        {"https://example.com/about/": ["/"], BASE: ["/about"]},
        redirects={"https://example.com/about": "https://example.com/about/"},
    )
    stats = make_crawler(store, renderer).crawl([BASE])

    assert stats.redirected == 1
    visited = set(store.visited_urls())
    assert "https://example.com/about/" in visited
    assert "https://example.com/about" not in visited


def test_a_redirect_target_is_not_fetched_again(store: UrlStore) -> None:
    """We already have the target's content, so re-navigating to it is wasted work."""
    renderer = FakeRenderer(
        {BASE: ["/old"], "https://example.com/final": []},
        redirects={"https://example.com/old": "https://example.com/final"},
    )
    make_crawler(store, renderer).crawl([BASE])

    assert renderer.rendered == [BASE, "https://example.com/old"]
    assert "https://example.com/final" in set(store.visited_urls())


def test_a_redirect_off_site_is_recorded_not_followed(store: UrlStore) -> None:
    renderer = FakeRenderer(
        {BASE: ["/out"]},
        redirects={"https://example.com/out": "https://elsewhere.example/landing"},
    )
    stats = make_crawler(store, renderer).crawl([BASE])

    assert stats.redirected == 1
    assert "https://elsewhere.example/landing" not in set(store.visited_urls())
    assert "https://example.com/out" not in set(store.visited_urls())


def test_a_redirect_on_the_seed_does_not_truncate_the_crawl(store: UrlStore) -> None:
    """Apex -> www used to end the crawl at one page with a success message."""
    policy = UrlPolicy.build(BASE, include_subdomains=True)
    renderer = FakeRenderer(
        {
            "https://www.example.com/": ["https://www.example.com/a"],
            "https://www.example.com/a": [],
        },
        redirects={BASE: "https://www.example.com/"},
    )
    stats = make_crawler(store, renderer, policy=policy).crawl([BASE])

    assert stats.visited == 2
    assert set(store.visited_urls()) == {
        "https://www.example.com/", "https://www.example.com/a"
    }


# -- robots.txt --------------------------------------------------------------


class DenyRobots:
    def __init__(self, denied: set[str]) -> None:
        self.denied = denied

    def allows(self, url: str) -> bool:
        return url not in self.denied


def test_disallowed_urls_are_skipped_without_rendering(store: UrlStore) -> None:
    renderer = FakeRenderer({BASE: ["/private", "/public"], "https://example.com/public": []})
    crawler = make_crawler(store, renderer, robots=DenyRobots({"https://example.com/private"}))
    stats = crawler.crawl([BASE])

    assert "https://example.com/private" not in renderer.rendered
    assert stats.skipped == 1
    assert "https://example.com/public" in set(store.visited_urls())


# -- resumption --------------------------------------------------------------


def test_a_second_crawl_resumes_where_the_first_stopped(store: UrlStore) -> None:
    graph = {
        BASE: ["/a", "/b"],
        "https://example.com/a": [],
        "https://example.com/b": [],
    }
    first = FakeRenderer(graph)
    make_crawler(store, first, limits=Limits(max_pages=1)).crawl([BASE])
    assert store.counts()[Status.DONE] == 1

    second = FakeRenderer(graph)
    stats = make_crawler(store, second).crawl([BASE])

    assert stats.visited == 2
    assert store.counts()[Status.DONE] == 3
    assert BASE not in second.rendered  # already done, not re-fetched


# -- pacing ------------------------------------------------------------------


def test_the_delay_is_applied_between_pages_but_not_before_the_first() -> None:
    slept: list[float] = []
    with UrlStore(":memory:") as store:
        renderer = FakeRenderer({BASE: ["/a"], "https://example.com/a": []})
        ticks = iter(range(0, 1000))
        Crawler(
            store=store,
            renderer=renderer,  # type: ignore[arg-type]
            policy=UrlPolicy.build(BASE),
            delay=2.5,
            sleep=slept.append,
            monotonic=lambda: float(next(ticks)),
        ).crawl([BASE])

    assert slept == [2.5]


def test_two_urls_redirecting_to_one_target_count_it_once(store: UrlStore) -> None:
    """Otherwise the summary claims more visited pages than the sitemap contains."""
    renderer = FakeRenderer(
        {BASE: ["/old-a", "/old-b"], "https://example.com/target": []},
        redirects={
            "https://example.com/old-a": "https://example.com/target",
            "https://example.com/old-b": "https://example.com/target",
        },
    )
    stats = make_crawler(store, renderer).crawl([BASE])

    assert stats.redirected == 2
    assert stats.visited == 2  # the seed and the target
    assert stats.visited == len(set(store.visited_urls()))


# -- rel=canonical -----------------------------------------------------------


class CanonicalRenderer:
    """Serves a link graph plus a rel=canonical declaration per URL."""

    def __init__(self, graph: dict[str, list[str]], canonicals: dict[str, str]) -> None:
        self.graph = graph
        self.canonicals = canonicals
        self.rendered: list[str] = []

    def render(self, url: str) -> RenderedPage:
        self.rendered.append(url)
        return RenderedPage(
            url=url,
            links=tuple(self.graph.get(url, [])),
            canonical=self.canonicals.get(url),
        )


def test_a_canonical_url_owns_the_page(store: UrlStore) -> None:
    """/products/index.html and /products/ serve the same document; only one is a page."""
    renderer = CanonicalRenderer(
        {BASE: ["/products/index.html"], "https://example.com/products/index.html": []},
        {"https://example.com/products/index.html": "https://example.com/products/"},
    )
    stats = make_crawler(store, renderer).crawl([BASE])

    visited = set(store.visited_urls())
    assert "https://example.com/products/" in visited
    assert "https://example.com/products/index.html" not in visited
    assert stats.duplicates == 1


def test_a_self_referencing_canonical_changes_nothing(store: UrlStore) -> None:
    renderer = CanonicalRenderer({BASE: []}, {BASE: BASE})
    stats = make_crawler(store, renderer).crawl([BASE])

    assert set(store.visited_urls()) == {BASE}
    assert stats.duplicates == 0


def test_an_out_of_scope_canonical_is_ignored(store: UrlStore) -> None:
    """A wrong canonical must not silently drop the page from the sitemap."""
    renderer = CanonicalRenderer({BASE: []}, {BASE: "https://cdn.other.example/x"})
    stats = make_crawler(store, renderer).crawl([BASE])

    assert set(store.visited_urls()) == {BASE}
    assert stats.duplicates == 0


def test_links_from_a_duplicate_page_are_still_followed(store: UrlStore) -> None:
    renderer = CanonicalRenderer(
        {
            BASE: ["/dup"],
            "https://example.com/dup": ["/discovered"],
            "https://example.com/real": [],
            "https://example.com/discovered": [],
        },
        {"https://example.com/dup": "https://example.com/real"},
    )
    make_crawler(store, renderer).crawl([BASE])

    visited = set(store.visited_urls())
    assert "https://example.com/discovered" in visited
    assert "https://example.com/real" in visited
    assert "https://example.com/dup" not in visited


def test_canonical_handling_can_be_turned_off(store: UrlStore) -> None:
    renderer = CanonicalRenderer(
        {BASE: []}, {BASE: "https://example.com/elsewhere"}
    )
    stats = make_crawler(store, renderer, respect_canonical=False).crawl([BASE])

    assert set(store.visited_urls()) == {BASE}
    assert stats.duplicates == 0
