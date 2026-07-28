"""Sitemap XML generation, including the sitemaps.org limits the old version ignored."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path

import pytest

from spa_sitemap.sitemap import (
    SITEMAP_NS,
    SitemapError,
    SitemapUrl,
    entries,
    write_sitemap,
)

NS = {"s": SITEMAP_NS}


def locs(path: Path) -> list[str]:
    root = ET.parse(path).getroot()
    return [element.text or "" for element in root.findall("s:url/s:loc", NS)]


def lastmods(path: Path) -> list[str]:
    root = ET.parse(path).getroot()
    return [element.text or "" for element in root.findall("s:url/s:lastmod", NS)]


# -- structure ---------------------------------------------------------------


def test_writes_a_valid_urlset(tmp_path: Path) -> None:
    out = tmp_path / "sitemap.xml"
    result = write_sitemap(entries(["https://a/1", "https://a/2"]), out)

    root = ET.parse(out).getroot()
    assert root.tag == f"{{{SITEMAP_NS}}}urlset"
    assert locs(out) == ["https://a/1", "https://a/2"]
    assert result.url_count == 2
    assert result.is_split is False


def test_declares_the_xml_prolog_and_encoding(tmp_path: Path) -> None:
    out = tmp_path / "sitemap.xml"
    write_sitemap(entries(["https://a/1"]), out)
    assert out.read_text(encoding="utf-8").startswith("<?xml version='1.0' encoding='utf-8'?>")


def test_no_lastmod_element_when_none_is_given(tmp_path: Path) -> None:
    out = tmp_path / "sitemap.xml"
    write_sitemap(entries(["https://a/1"]), out)
    assert lastmods(out) == []


def test_a_shared_lastmod_is_written_for_every_entry(tmp_path: Path) -> None:
    out = tmp_path / "sitemap.xml"
    write_sitemap(entries(["https://a/1", "https://a/2"], date(2026, 7, 27)), out)
    assert lastmods(out) == ["2026-07-27", "2026-07-27"]


def test_per_entry_lastmod_is_respected(tmp_path: Path) -> None:
    out = tmp_path / "sitemap.xml"
    write_sitemap(
        [
            SitemapUrl("https://a/1", date(2026, 1, 2)),
            SitemapUrl("https://a/2", None),
            SitemapUrl("https://a/3", date(2026, 3, 4)),
        ],
        out,
    )
    assert lastmods(out) == ["2026-01-02", "2026-03-04"]
    assert len(locs(out)) == 3


# -- escaping ----------------------------------------------------------------


def test_special_characters_are_escaped(tmp_path: Path) -> None:
    """String-formatted XML would emit this raw and produce an unparseable file."""
    out = tmp_path / "sitemap.xml"
    url = "https://a/search?q=cats&size=1<2>3'\"end"
    write_sitemap(entries([url]), out)

    assert "&amp;" in out.read_text(encoding="utf-8")
    assert locs(out) == [url]  # round-trips exactly


def test_non_ascii_urls_round_trip(tmp_path: Path) -> None:
    out = tmp_path / "sitemap.xml"
    url = "https://a/статті/ünïcode"
    write_sitemap(entries([url]), out)
    assert locs(out) == [url]


# -- splitting ---------------------------------------------------------------


def test_splits_past_the_url_limit_and_writes_an_index(tmp_path: Path) -> None:
    out = tmp_path / "sitemap.xml"
    urls = [f"https://a/{n}" for n in range(10)]
    result = write_sitemap(entries(urls), out, max_urls=4, base_url="https://a/")

    assert result.is_split
    assert result.url_count == 10
    assert [p.name for p in result.files] == [
        "sitemap-1.xml", "sitemap-2.xml", "sitemap-3.xml"
    ]
    assert [len(locs(p)) for p in result.files] == [4, 4, 2]

    index = ET.parse(out).getroot()
    assert index.tag == f"{{{SITEMAP_NS}}}sitemapindex"
    assert [e.text for e in index.findall("s:sitemap/s:loc", NS)] == [
        "https://a/sitemap-1.xml", "https://a/sitemap-2.xml", "https://a/sitemap-3.xml"
    ]


def test_every_url_survives_a_split(tmp_path: Path) -> None:
    urls = [f"https://a/{n}" for n in range(23)]
    result = write_sitemap(entries(urls), tmp_path / "sitemap.xml", max_urls=5)

    written = [loc for path in result.files for loc in locs(path)]
    assert written == urls


def test_the_byte_limit_also_forces_a_split(tmp_path: Path) -> None:
    """A 50k-URL file can still blow the 50 MB limit if the URLs are long."""
    urls = [f"https://a/{'x' * 400}/{n}" for n in range(10)]
    result = write_sitemap(entries(urls), tmp_path / "sitemap.xml", max_bytes=2_000)

    assert result.is_split
    assert sum(len(locs(p)) for p in result.files) == 10


def test_the_index_origin_falls_back_to_the_first_url(tmp_path: Path) -> None:
    result = write_sitemap(
        entries([f"https://fallback.example/{n}" for n in range(4)]),
        tmp_path / "sitemap.xml",
        max_urls=2,
    )
    index = ET.parse(result.index).getroot()  # type: ignore[arg-type]
    assert index.findall("s:sitemap/s:loc", NS)[0].text == (
        "https://fallback.example/sitemap-1.xml"
    )


def test_index_entries_carry_a_lastmod(tmp_path: Path) -> None:
    result = write_sitemap(entries(["https://a/1", "https://a/2"]), tmp_path / "s.xml", max_urls=1)
    index = ET.parse(result.index).getroot()  # type: ignore[arg-type]
    stamps = [e.text for e in index.findall("s:sitemap/s:lastmod", NS)]
    assert stamps == [date.today().isoformat()] * 2


def test_exactly_at_the_limit_does_not_split(tmp_path: Path) -> None:
    result = write_sitemap(entries(["https://a/1", "https://a/2"]), tmp_path / "s.xml", max_urls=2)
    assert result.is_split is False


def test_a_nonsensical_limit_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(SitemapError, match="max_urls"):
        write_sitemap(entries(["https://a/1"]), tmp_path / "s.xml", max_urls=0)


# -- writing -----------------------------------------------------------------


def test_the_previous_sitemap_is_replaced_atomically(tmp_path: Path) -> None:
    out = tmp_path / "sitemap.xml"
    write_sitemap(entries(["https://a/old"]), out)
    write_sitemap(entries(["https://a/new"]), out)

    assert locs(out) == ["https://a/new"]
    assert list(tmp_path.iterdir()) == [out]  # no temp files left behind


def test_missing_directories_are_created(tmp_path: Path) -> None:
    out = tmp_path / "deep" / "nested" / "sitemap.xml"
    write_sitemap(entries(["https://a/1"]), out)
    assert out.exists()


def test_an_empty_sitemap_is_still_well_formed(tmp_path: Path) -> None:
    out = tmp_path / "sitemap.xml"
    result = write_sitemap([], out)

    assert result.url_count == 0
    assert ET.parse(out).getroot().tag == f"{{{SITEMAP_NS}}}urlset"
    assert locs(out) == []


def test_output_is_indented_for_humans(tmp_path: Path) -> None:
    out = tmp_path / "sitemap.xml"
    write_sitemap(entries(["https://a/1"]), out)
    assert "\n  <url>" in out.read_text(encoding="utf-8")
