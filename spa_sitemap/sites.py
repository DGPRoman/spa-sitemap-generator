"""Where each site's crawl lives on disk.

One database holds one site -- its rows are keyed by URLs canonicalised under one
scope, so mixing two sites in one file would mean every read needing a filter it
could forget. Rather than making the user remember a ``--database`` per site, the
path is derived from the URL:

    sites/example.com/sitemap.db
    sites/example.com/sitemap.xml

A directory per site rather than flat ``sites/example.com.db`` files, for a
concrete reason: a split sitemap writes ``sitemap-1.xml`` beside its index, so flat
output would let one site's chunks collide with another site's name. The directory
also keeps the ``-wal`` and ``-shm`` sidecars together, which makes deleting a site
``rm -rf`` and archiving it ``tar``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from spa_sitemap.urls import Scope, ScopeError

DEFAULT_SITES_DIR: Final = Path("sites")

#: Where crawls lived before they were kept per site. Still honoured when it is
#: there, so upgrading does not orphan a crawl somebody is part-way through.
LEGACY_DATABASE: Final = Path("db/sitemap.db")

DATABASE_NAME: Final = "sitemap.db"
OUTPUT_NAME: Final = "sitemap.xml"

_VALID_SLUG: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


class SiteError(ValueError):
    """Raised when a site cannot be identified, named, or told apart from others."""


@dataclass(frozen=True, slots=True)
class Site:
    """One crawl's own corner of the filesystem."""

    slug: str
    directory: Path

    @property
    def database(self) -> Path:
        return self.directory / DATABASE_NAME

    @property
    def output(self) -> Path:
        return self.directory / OUTPUT_NAME


def for_url(
    url: str,
    *,
    sites_dir: Path = DEFAULT_SITES_DIR,
    include_subdomains: bool = False,
    restrict_to_path: bool = True,
) -> Site:
    """The site a URL belongs to, named after its crawl scope."""
    try:
        scope = Scope.from_url(
            url, include_subdomains=include_subdomains, restrict_to_path=restrict_to_path
        )
    except ScopeError as exc:
        raise SiteError(str(exc)) from exc
    return Site(slug=scope.slug, directory=sites_dir / scope.slug)


def named(slug: str, *, sites_dir: Path = DEFAULT_SITES_DIR) -> Site:
    """A site the user named explicitly.

    Validated rather than sanitised: silently rewriting ``--site ../../etc`` into
    something else would leave the user looking for a directory that does not
    exist, and a name is short enough to just retype.
    """
    if not _VALID_SLUG.match(slug):
        raise SiteError(
            f"--site must be letters, digits, dots, dashes or underscores, got {slug!r}"
        )
    return Site(slug=slug, directory=sites_dir / slug)


def existing(sites_dir: Path = DEFAULT_SITES_DIR) -> list[Site]:
    """Every site with a database on disk, in a stable order.

    A directory scan rather than a registry file: a registry is a second copy of
    the truth, and the two drift the first time somebody deletes a directory.
    """
    if not sites_dir.is_dir():
        return []
    return [
        Site(slug=child.name, directory=child)
        for child in sorted(sites_dir.iterdir())
        if (child / DATABASE_NAME).is_file()
    ]
