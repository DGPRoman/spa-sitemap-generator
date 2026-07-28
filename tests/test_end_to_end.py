"""End-to-end: real Chrome, real HTTP server, real SQLite, real sitemap.xml.

Marked ``browser`` so it can be deselected with ``-m 'not browser'``. Everything else
in the suite runs without a browser; this is the one place the Selenium layer is
actually exercised.

The fixture site injects every link with JavaScript after load, so a crawler that
only read the HTML source would find zero links. That is the premise of the whole
project, and ``test_the_fixture_site_needs_javascript`` pins it down.
"""

from __future__ import annotations

import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from pathlib import Path

import pytest

from spa_sitemap.crawler import Crawler, Limits
from spa_sitemap.renderer import ChromeRenderer, NotHtmlError, RenderError
from spa_sitemap.sitemap import SITEMAP_NS, entries, write_sitemap
from spa_sitemap.store import Status, UrlStore
from spa_sitemap.urls import UrlPolicy

pytestmark = pytest.mark.browser


@pytest.fixture(scope="module")
def renderer() -> Iterator[ChromeRenderer]:
    """One browser for the whole module -- starting Chrome is the expensive part."""
    with ChromeRenderer(headless=True, page_load_timeout=20, settle_timeout=5) as renderer:
        yield renderer


def test_the_fixture_site_needs_javascript(site_url: str) -> None:
    with urllib.request.urlopen(f"{site_url}/index.html", timeout=10) as response:
        html = response.read().decode()
    assert "<a " not in html


def test_the_renderer_sees_links_that_only_exist_after_js(
    site_url: str, renderer: ChromeRenderer
) -> None:
    page = renderer.render(f"{site_url}/index.html")
    assert len(page.links) >= 10
    assert f"{site_url}/about.html" in page.links


def test_the_renderer_reports_a_404_instead_of_returning_an_empty_page(
    site_url: str, renderer: ChromeRenderer
) -> None:
    """Without this, every 404 page lands in the sitemap as a real URL."""
    if not renderer.detect_http_errors:
        pytest.skip("this Chrome build does not expose the CDP performance log")

    with pytest.raises(RenderError) as failure:
        renderer.render(f"{site_url}/missing.html")
    assert "404" in str(failure.value)
    assert failure.value.retryable is False


def test_a_full_crawl_produces_the_expected_sitemap(
    site_url: str, renderer: ChromeRenderer, tmp_path: Path
) -> None:
    seed = f"{site_url}/index.html"
    policy = UrlPolicy.build(seed)

    with UrlStore(tmp_path / "sitemap.db") as store:
        stats = Crawler(
            store=store,
            renderer=renderer,
            policy=policy,
            limits=Limits(max_attempts=1),
            delay=0,
        ).crawl([seed])

        visited = set(store.visited_urls())
        counts = store.counts()
        output = write_sitemap(entries(store.visited_urls()), tmp_path / "sitemap.xml")

    expected = {
        f"{site_url}{path}"
        for path in (
            "/index.html", "/about.html", "/contact.html",
            # /products/index.html declares /products/ as its canonical URL, so the
            # canonical form is the page and the duplicate is not.
            "/products/", "/products/a.html", "/products/b.html",
            "/deep/1.html", "/deep/2.html", "/deep/3.html",
        )
    }
    assert visited == expected
    assert stats.stop_reason == "frontier-empty"
    assert stats.visited == len(expected)

    # The 404 was recorded as a failure, never as a page.
    assert counts[Status.FAILED] == 1
    assert f"{site_url}/missing.html" not in visited

    # Off-site, mailto:, tel:, javascript: and the .pdf were all filtered out.
    assert not any("example.com" in url for url in visited)
    assert not any(url.endswith(".pdf") for url in visited)

    # ?utm_source=nav collapsed onto the plain /contact.html rather than duplicating it.
    assert not any("utm_source" in url for url in visited)

    # Both 301s were recorded as redirects, and neither redirecting URL is a page:
    # /products must not appear alongside /products/index.html.
    assert counts[Status.REDIRECTED] == 2
    assert f"{site_url}/moved.html" not in visited
    assert f"{site_url}/products" not in visited

    # rel=canonical collapsed /products/index.html onto /products/.
    assert counts[Status.DUPLICATE] >= 1
    assert f"{site_url}/products/index.html" not in visited

    root = ET.parse(output.files[0]).getroot()
    locs = [e.text for e in root.findall(f"{{{SITEMAP_NS}}}url/{{{SITEMAP_NS}}}loc")]
    assert sorted(locs) == sorted(expected)
    assert len(locs) == len(set(locs))  # no duplicate <loc>, the original bug


def test_a_download_url_is_skipped_not_attributed_to_the_previous_page(
    site_url: str, renderer: ChromeRenderer
) -> None:
    """Chrome downloads a PDF without navigating, leaving the previous page in the DOM.

    The extension filter normally stops this URL being queued at all; this covers
    the case where an asset is served from an extension-less URL.
    """
    renderer.render(f"{site_url}/about.html")  # establish a known current page

    with pytest.raises(NotHtmlError):
        renderer.render(f"{site_url}/brochure.pdf")


def test_max_pages_limits_a_real_crawl(
    site_url: str, renderer: ChromeRenderer, tmp_path: Path
) -> None:
    seed = f"{site_url}/index.html"
    with UrlStore(tmp_path / "limited.db") as store:
        stats = Crawler(
            store=store,
            renderer=renderer,
            policy=UrlPolicy.build(seed),
            limits=Limits(max_pages=3, max_attempts=1),
            delay=0,
        ).crawl([seed])

        assert stats.visited == 3
        assert stats.stop_reason == "max-pages"
        assert store.has_pending(max_attempts=1) is True


def test_a_crawl_resumes_after_being_stopped(
    site_url: str, renderer: ChromeRenderer, tmp_path: Path
) -> None:
    seed = f"{site_url}/index.html"
    policy = UrlPolicy.build(seed)
    database = tmp_path / "resume.db"

    def crawl(**limits: object) -> object:
        with UrlStore(database) as store:
            return Crawler(
                store=store, renderer=renderer, policy=policy,
                limits=Limits(max_attempts=1, **limits), delay=0,  # type: ignore[arg-type]
            ).crawl([seed])

    first = crawl(max_pages=2)
    assert first.visited == 2  # type: ignore[attr-defined]

    second = crawl()
    assert second.stop_reason == "frontier-empty"  # type: ignore[attr-defined]

    with UrlStore(database) as store:
        assert len(set(store.visited_urls())) == 9
