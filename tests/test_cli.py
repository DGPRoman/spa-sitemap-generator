"""CLI wiring and exit codes -- no browser is started in this module.

The old CLI called `args.func()` with no arguments, so no option could ever reach a
handler, and every path returned exit code 0 regardless of what happened.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from spa_sitemap import cli
from spa_sitemap.cli import EXIT_ERROR, EXIT_OK, build_parser, main
from spa_sitemap.store import UrlStore


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A working directory with a valid config.json."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.json").write_text(
        json.dumps({"base_url": "https://example.com/", "delay": 0}), encoding="utf-8"
    )
    return tmp_path


def crawled(db: Path, urls: dict[str, int]) -> None:
    """Seed a database as though a crawl had visited ``urls``."""
    with UrlStore(db) as store:
        store.set_meta("base_url", "https://example.com/")
        store.enqueue(urls, depth=0)
        for url, link_count in urls.items():
            store.mark_done(url, link_count=link_count)


# -- parsing -----------------------------------------------------------------


def test_a_command_is_required(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        build_parser().parse_args([])
    assert exit_info.value.code == 2


def test_an_unknown_command_exits_with_a_usage_error() -> None:
    with pytest.raises(SystemExit) as exit_info:
        build_parser().parse_args(["frobnicate"])
    assert exit_info.value.code == 2


@pytest.mark.parametrize("command", ["new", "update", "export", "status"])
def test_the_documented_commands_all_exist(command: str) -> None:
    assert build_parser().parse_args([command]).command == command


def test_crawl_options_actually_reach_the_namespace() -> None:
    args = build_parser().parse_args(
        ["new", "--url", "https://x.test/", "--max-pages", "5", "--max-depth", "2",
         "--delay", "0.5", "--no-headless", "--ignore-robots", "--wait-for", "#app"]
    )
    assert args.base_url == "https://x.test/"
    assert (args.max_pages, args.max_depth, args.delay) == (5, 2, 0.5)
    assert args.headless is False
    assert args.respect_robots is False
    assert args.wait_for_selector == "#app"


def test_unsupplied_flags_stay_none_so_the_config_wins() -> None:
    """`None` is what tells `from_sources` to leave the config file's value alone."""
    args = build_parser().parse_args(["new"])
    assert args.headless is None
    assert args.respect_robots is None
    assert args.max_pages is None


def test_verbose_and_quiet_are_mutually_exclusive() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["status", "-v", "-q"])


# -- config errors -----------------------------------------------------------


def test_crawling_without_a_config_or_url_is_an_error_not_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert main(["new", "-y"]) == EXIT_ERROR


def test_an_invalid_config_value_is_an_error(project: Path) -> None:
    (project / "config.json").write_text(json.dumps({"base_url": "nope"}), encoding="utf-8")
    assert main(["status"]) == EXIT_ERROR


def test_an_unknown_config_key_is_an_error(project: Path) -> None:
    (project / "config.json").write_text(
        json.dumps({"base_url": "https://example.com/", "typo": 1}), encoding="utf-8"
    )
    assert main(["status"]) == EXIT_ERROR


# -- status ------------------------------------------------------------------


def test_status_works_on_a_fresh_checkout(project: Path,
                                          capsys: pytest.CaptureFixture[str]) -> None:
    """`status` used to be impossible: only `new` ever created the schema."""
    assert main(["status"]) == EXIT_OK
    assert "known    : 0 URLs" in capsys.readouterr().out


def test_status_reports_counts_and_problems(project: Path,
                                            capsys: pytest.CaptureFixture[str]) -> None:
    db = project / "db" / "sitemap.db"
    crawled(db, {"https://example.com/a": 1})
    with UrlStore(db) as store:
        store.enqueue(["https://example.com/bad"], depth=0)
        store.mark_failed("https://example.com/bad", "HTTP 404")

    assert main(["status"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "done        1" in out
    assert "HTTP 404" in out


# -- export ------------------------------------------------------------------


def test_export_writes_the_visited_urls(project: Path) -> None:
    crawled(project / "db" / "sitemap.db", {"https://example.com/a": 1, "https://example.com/b": 0})

    assert main(["export"]) == EXIT_OK
    root = ET.parse(project / "sitemap.xml").getroot()
    locs = [e.text for e in root.iter("{http://www.sitemaps.org/schemas/sitemap/0.9}loc")]
    assert locs == ["https://example.com/a", "https://example.com/b"]


def test_export_fails_instead_of_publishing_an_empty_sitemap(project: Path) -> None:
    """An empty sitemap tells search engines the site has no pages."""
    assert main(["export"]) == EXIT_ERROR
    assert not (project / "sitemap.xml").exists()


def test_export_can_be_forced_to_write_an_empty_sitemap(project: Path) -> None:
    assert main(["export", "--allow-empty"]) == EXIT_OK
    assert (project / "sitemap.xml").exists()


def test_export_honours_the_output_flag(project: Path) -> None:
    crawled(project / "db" / "sitemap.db", {"https://example.com/a": 0})
    assert main(["export", "-o", "out/custom.xml"]) == EXIT_OK
    assert (project / "out" / "custom.xml").exists()


def test_export_adds_a_per_page_lastmod(project: Path) -> None:
    from datetime import date

    crawled(project / "db" / "sitemap.db", {"https://example.com/a": 0})
    assert main(["export", "--lastmod", "visited"]) == EXIT_OK

    root = ET.parse(project / "sitemap.xml").getroot()
    stamps = [e.text for e in root.iter("{http://www.sitemaps.org/schemas/sitemap/0.9}lastmod")]
    assert stamps == [date.today().isoformat()]


def test_export_rejects_a_malformed_lastmod(project: Path) -> None:
    crawled(project / "db" / "sitemap.db", {"https://example.com/a": 0})
    assert main(["export", "--lastmod", "last-tuesday"]) == EXIT_ERROR


def test_export_warns_that_an_unfinished_crawl_is_incomplete(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = project / "db" / "sitemap.db"
    crawled(db, {"https://example.com/a": 1})
    with UrlStore(db) as store:
        store.enqueue(["https://example.com/queued"], depth=1)

    assert main(["export"]) == EXIT_OK
    # Asserted on stderr rather than via caplog: the package logger does not
    # propagate to root, so what the user sees is the only honest thing to check.
    assert "still queued" in capsys.readouterr().err


# -- update ------------------------------------------------------------------


def test_update_on_an_empty_database_is_an_error(project: Path) -> None:
    assert main(["update"]) == EXIT_ERROR


def test_update_refuses_to_resume_a_different_site(project: Path) -> None:
    crawled(project / "db" / "sitemap.db", {"https://example.com/a": 0})
    assert main(["update", "--url", "https://other.test/"]) == EXIT_ERROR


def test_update_reports_a_finished_crawl_without_starting_a_browser(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    crawled(project / "db" / "sitemap.db", {"https://example.com/a": 0})

    def explode(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("a browser must not be started when nothing is queued")

    monkeypatch.setattr(cli, "ChromeRenderer", explode)
    assert main(["update"]) == EXIT_OK


# -- new ---------------------------------------------------------------------


def test_new_asks_before_discarding_an_existing_crawl(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    crawled(project / "db" / "sitemap.db", {"https://example.com/a": 0})
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")

    def explode(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the crawl must not start after declining")

    monkeypatch.setattr(cli, "ChromeRenderer", explode)
    assert main(["new"]) == EXIT_OK

    with UrlStore(project / "db" / "sitemap.db") as store:
        assert store.total() == 1  # nothing was discarded


def test_new_does_not_prompt_when_there_is_nothing_to_lose(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr(
        "builtins.input", lambda _prompt: pytest.fail("must not prompt on an empty database")
    )

    calls: list[str] = []
    monkeypatch.setattr(cli, "_run_crawl", lambda *a, **k: calls.append("ran") or EXIT_OK)
    assert main(["new"]) == EXIT_OK
    assert calls == ["ran"]


# -- logging -----------------------------------------------------------------


def test_configure_logging_does_not_duplicate_handlers() -> None:
    """The old logger module added a handler on every import."""
    import logging

    cli.configure_logging()
    cli.configure_logging(verbose=True)
    assert len(logging.getLogger("spa_sitemap").handlers) == 1


# -- the config file is optional ---------------------------------------------


def test_url_alone_runs_without_a_config_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The old error told you to "pass --url" while refusing to run without a file."""
    monkeypatch.chdir(tmp_path)
    seen: list[str] = []
    monkeypatch.setattr(
        cli, "_run_crawl", lambda config, store, **kw: seen.append(config.base_url) or EXIT_OK
    )

    assert main(["new", "--url", "https://cli-only.test/", "-y"]) == EXIT_OK
    assert seen == ["https://cli-only.test/"]
    assert not (tmp_path / "config.json").exists()


def test_no_config_and_no_url_explains_both_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    assert main(["new", "-y"]) == EXIT_ERROR
    err = capsys.readouterr().err
    assert "--url" in err and "base_url" in err


def test_export_needs_no_config_and_no_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exporting reads back an existing crawl; demanding the URL again is busywork."""
    monkeypatch.chdir(tmp_path)
    crawled(tmp_path / "db" / "sitemap.db", {"https://recorded.test/a": 0})

    assert main(["export"]) == EXIT_OK
    root = ET.parse(tmp_path / "sitemap.xml").getroot()
    locs = [e.text for e in root.iter("{http://www.sitemaps.org/schemas/sitemap/0.9}loc")]
    assert locs == ["https://recorded.test/a"]


def test_status_needs_no_config_and_reports_the_recorded_site(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    crawled(tmp_path / "db" / "sitemap.db", {"https://example.com/a": 0})

    assert main(["status"]) == EXIT_OK
    assert "https://example.com/" in capsys.readouterr().out


def test_update_takes_the_site_from_the_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    db = tmp_path / "db" / "sitemap.db"
    crawled(db, {"https://example.com/a": 0})
    with UrlStore(db) as store:
        store.enqueue(["https://example.com/queued"], depth=1)

    seen: list[str | None] = []
    monkeypatch.setattr(
        cli, "_run_crawl", lambda config, store, **kw: seen.append(config.base_url) or EXIT_OK
    )
    assert main(["update"]) == EXIT_OK
    assert seen == ["https://example.com/"]


def test_an_explicitly_named_missing_config_is_an_error(project: Path) -> None:
    assert main(["status", "-c", "does-not-exist.json"]) == EXIT_ERROR


def test_an_alternate_config_file_is_honoured(project: Path,
                                              capsys: pytest.CaptureFixture[str]) -> None:
    (project / "other.json").write_text(
        json.dumps({"base_url": "https://other.test/"}), encoding="utf-8"
    )
    assert main(["status", "-c", "other.json"]) == EXIT_OK
    assert "https://other.test/" in capsys.readouterr().out
