"""Writing sitemaps that comply with the sitemaps.org protocol.

Three things the protocol requires that the previous version did not do:

* a single sitemap holds at most 50 000 URLs and 50 MB uncompressed -- past either
  limit the file must be split and accompanied by a sitemap index;
* URLs must be XML-escaped (``ElementTree`` handles this, string formatting does not);
* the file should not be left half-written if something fails, hence the atomic
  temp-file-then-rename.
"""

from __future__ import annotations

import logging
import os
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Final
from urllib.parse import urljoin, urlsplit

log = logging.getLogger(__name__)

SITEMAP_NS: Final = "http://www.sitemaps.org/schemas/sitemap/0.9"
MAX_URLS_PER_FILE: Final = 50_000
MAX_BYTES_PER_FILE: Final = 50 * 1024 * 1024

#: Per-entry XML overhead (`<url><loc></loc><lastmod/></url>` plus indentation),
#: rounded up, so chunking by size stays on the safe side of the 50 MB limit.
_ENTRY_OVERHEAD: Final = 96


class SitemapError(ValueError):
    """Raised when a sitemap cannot be written correctly."""


@dataclass(frozen=True, slots=True)
class SitemapUrl:
    """One ``<url>`` entry. ``lastmod`` is per-entry because the crawl knows when
    it actually visited each page."""

    loc: str
    lastmod: date | None = None

    @property
    def byte_estimate(self) -> int:
        return len(self.loc.encode("utf-8")) + _ENTRY_OVERHEAD


def entries(urls: Iterable[str], lastmod: date | None = None) -> list[SitemapUrl]:
    """Convenience for the common case: plain URLs sharing one (or no) ``lastmod``."""
    return [SitemapUrl(loc=url, lastmod=lastmod) for url in urls]


@dataclass(frozen=True, slots=True)
class SitemapResult:
    files: tuple[Path, ...]
    index: Path | None
    url_count: int

    @property
    def is_split(self) -> bool:
        return self.index is not None

    def describe(self) -> str:
        if self.index is None:
            return f"{self.url_count} URLs -> {self.files[0]}"
        return (
            f"{self.url_count} URLs -> {len(self.files)} sitemaps "
            f"indexed by {self.index}"
        )


def write_sitemap(
    urls: Iterable[SitemapUrl],
    output: Path | str,
    *,
    base_url: str | None = None,
    max_urls: int = MAX_URLS_PER_FILE,
    max_bytes: int = MAX_BYTES_PER_FILE,
) -> SitemapResult:
    """Write ``urls`` to ``output``, splitting into an indexed set if necessary.

    When a split happens ``output`` becomes the sitemap index and the URL sets go
    to ``<stem>-1.xml``, ``<stem>-2.xml``, ... next to it. ``base_url`` (or, failing
    that, the origin of the first URL) is used to build the absolute child
    locations the index requires.
    """
    output = Path(output)
    if max_urls < 1:
        raise SitemapError("max_urls must be at least 1")

    chunks = list(_chunk(urls, max_urls=max_urls, max_bytes=max_bytes))
    total = sum(len(chunk) for chunk in chunks)

    if not chunks:
        log.warning("no pages to export; writing an empty sitemap to %s", output)
        _write_tree(_urlset_tree([]), output)
        return SitemapResult(files=(output,), index=None, url_count=0)

    if len(chunks) == 1:
        _write_tree(_urlset_tree(chunks[0]), output)
        log.info("wrote %d URLs to %s", total, output)
        return SitemapResult(files=(output,), index=None, url_count=total)

    origin = _origin_for_index(base_url, chunks[0][0].loc, output)
    files: list[Path] = []
    for number, chunk in enumerate(chunks, start=1):
        part = output.with_name(f"{output.stem}-{number}{output.suffix or '.xml'}")
        _write_tree(_urlset_tree(chunk), part)
        files.append(part)

    _write_tree(_index_tree(files, origin), output)
    log.info("wrote %d URLs across %d sitemaps indexed by %s", total, len(files), output)
    return SitemapResult(files=tuple(files), index=output, url_count=total)


# -- internals ---------------------------------------------------------------


def _chunk(
    urls: Iterable[SitemapUrl], *, max_urls: int, max_bytes: int
) -> Iterator[list[SitemapUrl]]:
    """Split URLs into groups that respect both the count and the byte limit."""
    current: list[SitemapUrl] = []
    size = 0
    for url in urls:
        entry_size = url.byte_estimate
        if current and (len(current) >= max_urls or size + entry_size > max_bytes):
            yield current
            current, size = [], 0
        current.append(url)
        size += entry_size
    if current:
        yield current


def _urlset_tree(urls: Iterable[SitemapUrl]) -> ET.ElementTree:
    urlset = ET.Element("urlset", {"xmlns": SITEMAP_NS})
    for url in urls:
        entry = ET.SubElement(urlset, "url")
        ET.SubElement(entry, "loc").text = url.loc
        if url.lastmod is not None:
            ET.SubElement(entry, "lastmod").text = url.lastmod.isoformat()
    return ET.ElementTree(urlset)


def _index_tree(files: Iterable[Path], origin: str) -> ET.ElementTree:
    index = ET.Element("sitemapindex", {"xmlns": SITEMAP_NS})
    for path in files:
        entry = ET.SubElement(index, "sitemap")
        ET.SubElement(entry, "loc").text = urljoin(origin, path.name)
        # The index's lastmod describes the child sitemap file, which we are
        # writing right now.
        ET.SubElement(entry, "lastmod").text = _file_stamp(path).isoformat()
    return ET.ElementTree(index)


def _file_stamp(path: Path) -> date:
    try:
        return date.fromtimestamp(path.stat().st_mtime)
    except OSError:
        return date.today()


def _origin_for_index(base_url: str | None, sample_url: str, output: Path) -> str:
    """Absolute prefix for child sitemap locations in the index."""
    for candidate in (base_url, sample_url):
        if not candidate:
            continue
        parts = urlsplit(candidate)
        if parts.scheme and parts.netloc:
            return f"{parts.scheme}://{parts.netloc}/"
    raise SitemapError(
        f"cannot build a sitemap index for {output}: no base URL and no usable URL"
    )


def _write_tree(tree: ET.ElementTree, path: Path) -> None:
    """Pretty-print and write atomically, so a failure cannot truncate the old file."""
    ET.indent(tree, space="  ")
    path.parent.mkdir(parents=True, exist_ok=True)

    # Write beside the target (same filesystem, so the rename is atomic) and only
    # then replace it. A crash mid-write leaves the previous sitemap intact.
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    temp_path = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            tree.write(handle, encoding="utf-8", xml_declaration=True)
            handle.flush()
            os.fsync(handle.fileno())
        temp_path.replace(path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise
