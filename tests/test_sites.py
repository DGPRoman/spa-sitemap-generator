"""Where a crawl lives on disk.

One database holds one site, so the path is derived from the URL rather than typed
by the user -- which is what stops a second crawl from either colliding with the
first or needing a --database nobody wants to remember.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from spa_sitemap import sites
from spa_sitemap.sites import SiteError


def test_a_url_names_its_own_files() -> None:
    site = sites.for_url("https://example.com/", sites_dir=Path("sites"))
    assert site.slug == "example.com"
    assert site.database == Path("sites/example.com/sitemap.db")
    assert site.output == Path("sites/example.com/sitemap.xml")


def test_two_urls_never_share_a_file() -> None:
    a = sites.for_url("https://a.test/")
    b = sites.for_url("https://b.test/")
    assert a.database != b.database
    assert a.output != b.output


def test_the_output_sits_beside_its_database() -> None:
    """Split sitemaps write `sitemap-1.xml` next to the index.

    Flat `sites/<slug>.db` files would let one site's chunks collide with another
    site's name, which is why a site gets a directory rather than a filename.
    """
    site = sites.for_url("https://example.com/")
    assert site.output.parent == site.database.parent


def test_an_unusable_url_is_a_clear_error() -> None:
    with pytest.raises(SiteError):
        sites.for_url("not a url")


@pytest.mark.parametrize("slug", ["..", "../etc", "a/b", "", ".hidden", "x" * 101])
def test_a_dangerous_site_name_is_refused(slug: str) -> None:
    """Validated, not sanitised: quietly rewriting the name the user typed would
    leave them looking for a directory that does not exist."""
    with pytest.raises(SiteError, match="--site"):
        sites.named(slug)


def test_a_reasonable_site_name_is_accepted() -> None:
    assert sites.named("my-client_2", sites_dir=Path("s")).directory == Path("s/my-client_2")


def test_existing_sites_are_those_with_a_database(tmp_path: Path) -> None:
    (tmp_path / "b.test").mkdir(parents=True)
    (tmp_path / "b.test" / "sitemap.db").touch()
    (tmp_path / "a.test").mkdir()
    (tmp_path / "a.test" / "sitemap.db").touch()
    (tmp_path / "not-a-site").mkdir()  # no database, so not a crawl

    assert [site.slug for site in sites.existing(tmp_path)] == ["a.test", "b.test"]


def test_no_sites_directory_is_not_an_error(tmp_path: Path) -> None:
    assert sites.existing(tmp_path / "nothing-here") == []
