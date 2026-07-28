"""URL canonicalisation and crawl-scope rules.

This module is the pure core of the crawler: no I/O, no browser, no database.
Everything here is a total function over strings, which is why it carries the
majority of the test suite.

Two ideas live here:

``Scope``
    Which URLs belong to the site being crawled.

``UrlPolicy``
    ``Scope`` plus the rewriting rules that turn an arbitrary ``href`` found in a
    page into the single canonical form used as the database key -- or ``None``
    when the link should not be crawled at all.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Final
from urllib.parse import SplitResult, parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

CRAWLABLE_SCHEMES: Final = frozenset({"http", "https"})

_DEFAULT_PORTS: Final = {"http": 80, "https": 443}

#: Query parameters that identify a *visit*, not a *page*. Stripping them is what
#: keeps ``/contact?utm_source=nav`` and ``/contact`` from becoming two sitemap
#: entries for the same document.
TRACKING_PARAMS: Final = frozenset(
    {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "utm_id",
        "gclid",
        "gbraid",
        "wbraid",
        "dclid",
        "fbclid",
        "msclkid",
        "yclid",
        "twclid",
        "igshid",
        "mc_cid",
        "mc_eid",
        "_ga",
        "_gl",
        "ref",
        "ref_src",
        "spm",
    }
)

#: Extensions that are never an HTML page. Following them costs a full browser
#: navigation and pollutes the sitemap, which is meant to list documents.
ASSET_SUFFIXES: Final = frozenset(
    {
        # documents
        ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".odt", ".ods",
        ".rtf", ".csv", ".txt", ".epub",
        # archives
        ".zip", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar", ".tar", ".dmg", ".iso",
        # images
        ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".svg", ".webp", ".avif", ".ico",
        ".tif", ".tiff", ".psd",
        # media
        ".mp3", ".mp4", ".m4a", ".m4v", ".avi", ".mov", ".mkv", ".webm", ".wav",
        ".ogg", ".flac", ".wmv",
        # code & data assets
        ".js", ".mjs", ".css", ".map", ".json", ".xml", ".rss", ".atom", ".wasm",
        # fonts & binaries
        ".woff", ".woff2", ".ttf", ".otf", ".eot", ".exe", ".msi", ".apk", ".deb",
        ".rpm", ".pkg", ".bin",
    }
)


class ScopeError(ValueError):
    """Raised when a base URL cannot define a crawl scope."""


def _normalise_port(scheme: str, port: int | None) -> int | None:
    """Collapse a scheme's default port to ``None`` so ``:443`` == implicit."""
    if port is None or _DEFAULT_PORTS.get(scheme) == port:
        return None
    return port


#: ``SplitResult.port`` raises on garbage like ``http://h:notaport/``. That is a
#: broken URL, which must be distinguished from "no port given" -- otherwise the
#: bad port gets silently dropped and the link is rewritten into a valid one.
INVALID_PORT: Final = -1


def _port_of(parts: SplitResult) -> int | None:
    try:
        return parts.port
    except ValueError:
        return INVALID_PORT


@dataclass(frozen=True, slots=True)
class Scope:
    """The boundary of a crawl: one host (optionally its subdomains), one path prefix.

    ``http`` and ``https`` are treated as the *same* origin and canonicalised to
    the base URL's scheme. Sites mix the two in their markup while serving one set
    of documents, so keeping them apart would either lose pages (strict match) or
    duplicate every one of them (accept both verbatim).
    """

    scheme: str
    host: str
    port: int | None
    path_prefix: str
    include_subdomains: bool = False

    @classmethod
    def from_url(
        cls,
        url: str,
        *,
        include_subdomains: bool = False,
        restrict_to_path: bool = True,
    ) -> Scope:
        parts = urlsplit(url.strip())
        scheme = parts.scheme.lower()
        if scheme not in CRAWLABLE_SCHEMES:
            raise ScopeError(f"base URL must be http(s), got {url!r}")
        host = (parts.hostname or "").lower()
        if not host:
            raise ScopeError(f"base URL has no host: {url!r}")

        port = _normalise_port(scheme, _port_of(parts))
        if port == INVALID_PORT:
            raise ScopeError(f"base URL has an invalid port: {url!r}")

        path = parts.path or "/"
        if restrict_to_path:
            # A base of ".../docs/guide.html" scopes the crawl to ".../docs/".
            prefix = path if path.endswith("/") else path.rsplit("/", 1)[0] + "/"
        else:
            prefix = "/"

        return cls(
            scheme=scheme,
            host=host,
            port=port,
            path_prefix=prefix,
            include_subdomains=include_subdomains,
        )

    @property
    def netloc(self) -> str:
        return self.host if self.port is None else f"{self.host}:{self.port}"

    @property
    def origin(self) -> str:
        return f"{self.scheme}://{self.netloc}"

    def matches_host(self, host: str) -> bool:
        host = host.lower()
        if host == self.host:
            return True
        return self.include_subdomains and host.endswith("." + self.host)


def same_site(one: str, other: str) -> bool:
    """Do two base URLs describe the same crawl target?

    Compares ``Scope`` values rather than the strings themselves, so
    ``https://x`` and ``https://x/`` are one site while ``https://x/docs/`` is a
    different one. That is the question both ``new`` and ``update`` need answered
    before they touch a database: its rows are keyed by URLs canonicalised under a
    scope, so a different scope means the stored keys cannot be reused.

    A URL that cannot define a scope at all compares equal to nothing, including
    an identical unparseable string -- if we cannot tell what a database holds, the
    caller must not be told it matches.
    """
    try:
        return Scope.from_url(one) == Scope.from_url(other)
    except ScopeError:
        return False


@dataclass(frozen=True, slots=True)
class UrlPolicy:
    """Turns a raw ``href`` into the canonical URL to crawl, or ``None`` to drop it."""

    scope: Scope
    strip_query_params: frozenset[str] = TRACKING_PARAMS
    keep_query: bool = True
    hash_routing: bool = False
    asset_suffixes: frozenset[str] = ASSET_SUFFIXES
    exclude: tuple[re.Pattern[str], ...] = field(default=())

    @classmethod
    def build(
        cls,
        base_url: str,
        *,
        include_subdomains: bool = False,
        restrict_to_path: bool = True,
        keep_query: bool = True,
        hash_routing: bool = False,
        strip_query_params: frozenset[str] = TRACKING_PARAMS,
        exclude_patterns: tuple[str, ...] = (),
    ) -> UrlPolicy:
        return cls(
            scope=Scope.from_url(
                base_url,
                include_subdomains=include_subdomains,
                restrict_to_path=restrict_to_path,
            ),
            strip_query_params=frozenset(p.lower() for p in strip_query_params),
            keep_query=keep_query,
            hash_routing=hash_routing,
            exclude=tuple(re.compile(p) for p in exclude_patterns),
        )

    def normalise(self, href: str | None, *, page_url: str) -> str | None:
        """Canonical in-scope URL for ``href`` as seen on ``page_url``, else ``None``.

        Dropped: empty links, non-HTTP schemes (``mailto:``, ``tel:``,
        ``javascript:``), off-scope hosts and paths, asset extensions, and
        anything matching ``exclude``.

        Fragments are normally stripped, so ``/a#top`` and ``/a`` are one page.
        With ``hash_routing`` enabled, a fragment that looks like a route
        (``#/products``, ``#!/products``) is kept instead -- on a hash-routed SPA
        it *is* the page identity, and discarding it collapses the whole site to
        one URL.
        """
        if not href or not (href := href.strip()):
            return None

        try:
            parts = urlsplit(urljoin(page_url, href))
        except ValueError:
            return None

        if parts.scheme.lower() not in CRAWLABLE_SCHEMES:
            return None

        host = (parts.hostname or "").lower()
        if not self.scope.matches_host(host):
            return None

        port = _normalise_port(parts.scheme.lower(), _port_of(parts))
        if port == INVALID_PORT or port != self.scope.port:
            return None

        path = parts.path or "/"
        if not path.startswith(self.scope.path_prefix):
            return None
        if self.is_asset(path):
            return None

        # Subdomains keep their own host; the scheme is normalised to the scope's.
        netloc = host if port is None else f"{host}:{port}"
        canonical = urlunsplit(
            (
                self.scope.scheme,
                netloc,
                path,
                self._clean_query(parts.query),
                self._keep_fragment(parts.fragment),
            )
        )

        if any(pattern.search(canonical) for pattern in self.exclude):
            return None
        return canonical

    def normalise_all(self, hrefs: Iterable[str | None], *, page_url: str) -> list[str]:
        """Deduplicated, order-preserving normalisation of every href on a page."""
        seen: dict[str, None] = {}
        for href in hrefs:
            url = self.normalise(href, page_url=page_url)
            if url is not None:
                seen.setdefault(url, None)
        return list(seen)

    def is_asset(self, path: str) -> bool:
        segment = path.rsplit("/", 1)[-1]
        dot = segment.rfind(".")
        if dot <= 0:
            return False
        return segment[dot:].lower() in self.asset_suffixes

    def _keep_fragment(self, fragment: str) -> str:
        """Keep client-side routes, never plain anchors like ``#top``."""
        if not self.hash_routing or not fragment:
            return ""
        return fragment if fragment.startswith(("/", "!")) else ""

    def _clean_query(self, query: str) -> str:
        if not query or not self.keep_query:
            return ""
        kept = [
            (key, value)
            for key, value in parse_qsl(query, keep_blank_values=True)
            if key.lower() not in self.strip_query_params
        ]
        # Sorted so that ?a=1&b=2 and ?b=2&a=1 are one URL, not two.
        return urlencode(sorted(kept))
