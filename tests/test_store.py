"""Frontier behaviour, against a real SQLite database in memory."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest
from conftest import FakeClock

from spa_sitemap.store import SCHEMA_VERSION, Status, StoreError, UrlStore


@pytest.fixture
def store() -> Iterator[UrlStore]:
    with UrlStore(":memory:") as store:
        yield store


# -- lifecycle ---------------------------------------------------------------


def test_using_a_closed_store_is_a_clear_error() -> None:
    store = UrlStore(":memory:")
    with pytest.raises(StoreError, match="not open"):
        store.total()


def test_schema_is_created_on_open_not_only_by_new(tmp_path) -> None:
    """`update`/`export` on a fresh checkout used to die with 'no such table'."""
    path = tmp_path / "nested" / "sitemap.db"
    with UrlStore(path) as store:
        assert store.counts() == dict.fromkeys((s.value for s in Status), 0)
    assert path.exists()


def test_close_is_idempotent() -> None:
    store = UrlStore(":memory:")
    store.open()
    store.close()
    store.close()


# -- enqueue / dedup ---------------------------------------------------------


def test_enqueue_reports_only_new_urls(store: UrlStore) -> None:
    assert store.enqueue(["https://a/1", "https://a/2"], depth=0) == 2
    assert store.enqueue(["https://a/2", "https://a/3"], depth=1) == 1
    assert store.total() == 3


def test_enqueue_dedupes_within_one_batch(store: UrlStore) -> None:
    """The URL is the primary key, so this is enforced by the database itself."""
    assert store.enqueue(["https://a/1"] * 5, depth=0) == 1
    assert store.total() == 1


def test_requeueing_a_known_url_does_not_reset_it(store: UrlStore) -> None:
    store.enqueue(["https://a/1"], depth=0)
    page = store.claim(max_attempts=3)
    assert page is not None
    store.mark_done(page.url, link_count=2)

    assert store.enqueue(["https://a/1"], depth=5) == 0
    assert store.counts()[Status.DONE] == 1
    assert list(store.visited_urls()) == ["https://a/1"]


def test_enqueue_of_nothing_is_free(store: UrlStore) -> None:
    assert store.enqueue([], depth=0) == 0


# -- claiming ----------------------------------------------------------------


def test_claim_is_breadth_first_and_deterministic(store: UrlStore) -> None:
    store.enqueue(["https://a/deep"], depth=2)
    store.enqueue(["https://a/mid"], depth=1)
    store.enqueue(["https://a/b", "https://a/a"], depth=0)

    order = []
    while (page := store.claim(max_attempts=3)) is not None:
        order.append(page.url)
        store.mark_done(page.url, link_count=0)

    assert order == ["https://a/a", "https://a/b", "https://a/mid", "https://a/deep"]


def test_claim_counts_the_attempt(store: UrlStore) -> None:
    store.enqueue(["https://a/1"], depth=0)
    assert store.claim(max_attempts=3).attempts == 1
    store.requeue("https://a/1", "boom")
    assert store.claim(max_attempts=3).attempts == 2


def test_claim_stops_handing_out_an_exhausted_url(store: UrlStore) -> None:
    """A page that kills the browser must not be retried forever across runs."""
    store.enqueue(["https://a/1"], depth=0)
    for _ in range(2):
        page = store.claim(max_attempts=2)
        assert page is not None
        store.requeue(page.url, "boom")

    assert store.claim(max_attempts=2) is None
    assert store.has_pending(max_attempts=2) is False
    assert store.has_pending(max_attempts=5) is True


def test_claim_on_an_empty_frontier_returns_none(store: UrlStore) -> None:
    assert store.claim(max_attempts=3) is None


def test_claim_preserves_depth(store: UrlStore) -> None:
    store.enqueue(["https://a/x"], depth=7)
    assert store.claim(max_attempts=3).depth == 7


# -- outcomes ----------------------------------------------------------------


def test_outcomes_are_recorded_and_counted(store: UrlStore) -> None:
    store.enqueue(
        ["https://a/ok", "https://a/bad", "https://a/skip", "https://a/moved", "https://a/dup"],
        depth=0,
    )
    store.mark_done("https://a/ok", link_count=3)
    store.mark_failed("https://a/bad", "HTTP 500")
    store.mark_skipped("https://a/skip", "robots.txt")
    store.mark_redirected("https://a/moved", "https://a/ok")
    store.mark_duplicate("https://a/dup", "https://a/ok")

    assert store.counts() == {
        Status.QUEUED: 0, Status.DONE: 1, Status.FAILED: 1, Status.SKIPPED: 1,
        Status.REDIRECTED: 1, Status.DUPLICATE: 1,
    }
    assert list(store.visited_urls()) == ["https://a/ok"]


def test_only_done_pages_reach_the_sitemap(store: UrlStore) -> None:
    """A 404 or a redirect must never appear as a <loc>."""
    store.enqueue(["https://a/ok", "https://a/404", "https://a/moved"], depth=0)
    store.mark_done("https://a/ok", link_count=0)
    store.mark_failed("https://a/404", "HTTP 404")
    store.mark_redirected("https://a/moved", "https://a/ok")

    assert list(store.visited_urls()) == ["https://a/ok"]


def test_mark_done_clears_a_previous_error_note(store: UrlStore) -> None:
    store.enqueue(["https://a/1"], depth=0)
    store.requeue("https://a/1", "timed out")
    store.mark_done("https://a/1", link_count=1)
    assert store.problems() == []


def test_problems_reports_failures_and_skips(store: UrlStore) -> None:
    store.enqueue(["https://a/bad", "https://a/skip"], depth=0)
    store.mark_failed("https://a/bad", "HTTP 404")
    store.mark_skipped("https://a/skip", "robots.txt")

    assert store.problems() == [
        (Status.FAILED, "https://a/bad", "HTTP 404"),
        (Status.SKIPPED, "https://a/skip", "robots.txt"),
    ]


def test_visited_entries_carry_the_visit_date(store: UrlStore) -> None:
    store.enqueue(["https://a/1"], depth=0)
    store.mark_done("https://a/1", link_count=0)

    (url, visited), = store.visited_entries()
    assert url == "https://a/1"
    assert visited == date.today()


def test_queued_pages_have_no_visit_date(store: UrlStore) -> None:
    store.enqueue(["https://a/1"], depth=0)
    assert list(store.visited_entries()) == []


def test_release_refunds_the_attempt_the_claim_charged(store: UrlStore) -> None:
    """`max_attempts=1` leaves no slack: without the refund the row is gone.

    `claim` counts the attempt up front so a page that kills the browser cannot be
    retried for ever. When the browser or the site was at fault, that charge is a
    lie, and leaving it in place is how an outage exhausts pages that were fine.
    """
    store.enqueue(["https://a/1"], depth=0)
    claimed = store.claim(max_attempts=1)
    assert claimed is not None
    assert claimed.attempts == 1
    assert store.claim(max_attempts=1) is None  # charged, so nothing left to hand out

    store.release("https://a/1", "net::ERR_CONNECTION_REFUSED")

    again = store.claim(max_attempts=1)
    assert again is not None
    assert again.url == "https://a/1"


def test_release_keeps_the_reason_and_the_queued_status(store: UrlStore) -> None:
    store.enqueue(["https://a/1"], depth=0)
    store.claim(max_attempts=3)
    store.release("https://a/1", "net::ERR_NAME_NOT_RESOLVED")

    assert store.counts()[Status.QUEUED] == 1
    assert store.has_pending(max_attempts=3)


def test_release_never_drives_attempts_below_zero(store: UrlStore) -> None:
    """Releasing without a matching claim must not make a URL immortal."""
    store.enqueue(["https://a/1"], depth=0)
    store.release("https://a/1", "browser died")
    store.release("https://a/1", "browser died again")

    page = store.claim(max_attempts=1)
    assert page is not None
    assert page.attempts == 1


# -- metadata & reset --------------------------------------------------------


def test_meta_round_trips_and_overwrites(store: UrlStore) -> None:
    assert store.get_meta("base_url") is None
    store.set_meta("base_url", "https://a/")
    store.set_meta("base_url", "https://b/")
    assert store.get_meta("base_url") == "https://b/"


def test_reset_clears_pages_and_meta(store: UrlStore) -> None:
    store.enqueue(["https://a/1"], depth=0)
    store.set_meta("base_url", "https://a/")
    store.reset()

    assert store.total() == 0
    assert store.get_meta("base_url") is None
    assert store.get_meta("schema_version") is not None


def test_reset_leaves_a_usable_store(store: UrlStore) -> None:
    store.reset()
    assert store.enqueue(["https://a/1"], depth=0) == 1


def test_mark_done_reports_whether_it_was_the_first_time(store: UrlStore) -> None:
    store.enqueue(["https://a/1"], depth=0)
    assert store.mark_done("https://a/1", link_count=2) is True
    assert store.mark_done("https://a/1", link_count=9) is False


# -- the retry schedule ------------------------------------------------------


def test_a_deferred_url_is_withheld_until_it_is_due() -> None:
    clock = FakeClock()
    with UrlStore(":memory:", now=clock) as store:
        store.enqueue(["https://a/1"], depth=0)
        store.claim(max_attempts=3)
        store.defer("https://a/1", "HTTP 503", 30.0)

        assert store.claim(max_attempts=3) is None
        assert store.seconds_until_due(max_attempts=3) == 30.0
        assert store.has_pending(max_attempts=3)  # deferred work is still pending

        clock.advance(30)
        page = store.claim(max_attempts=3)
        assert page is not None
        assert page.url == "https://a/1"


def test_a_deferral_does_not_hide_the_rest_of_the_frontier() -> None:
    """The point of scheduling rather than sleeping: other work carries on."""
    clock = FakeClock()
    with UrlStore(":memory:", now=clock) as store:
        store.enqueue(["https://a/1", "https://a/2"], depth=0)
        store.claim(max_attempts=3)
        store.defer("https://a/1", "HTTP 503", 30.0)

        page = store.claim(max_attempts=3)
        assert page is not None
        assert page.url == "https://a/2"


def test_no_deferrals_means_an_empty_frontier_really_is_empty(store: UrlStore) -> None:
    assert store.seconds_until_due(max_attempts=3) is None
    store.enqueue(["https://a/1"], depth=0)
    assert store.seconds_until_due(max_attempts=3) is None


def test_a_zero_deferral_is_just_a_requeue() -> None:
    clock = FakeClock()
    with UrlStore(":memory:", now=clock) as store:
        store.enqueue(["https://a/1"], depth=0)
        store.claim(max_attempts=3)
        store.defer("https://a/1", "HTTP 503", 0.0)

        assert store.seconds_until_due(max_attempts=3) is None
        assert store.claim(max_attempts=3) is not None


def test_requeue_and_release_clear_a_pending_deferral(store: UrlStore) -> None:
    """A URL held back and then re-decided must not stay stuck in the future."""
    for url, put_back in (("https://a/1", "requeue"), ("https://a/2", "release")):
        store.enqueue([url], depth=0)
        store.claim(max_attempts=3)
        store.defer(url, "HTTP 503", 300.0)
        getattr(store, put_back)(url, "changed our mind")

    assert store.seconds_until_due(max_attempts=3) is None


# -- schema versions ---------------------------------------------------------


def _make_v1_database(path: Path) -> None:
    """The v1 schema, byte for byte: no next_attempt_at column."""
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE pages (
            url           TEXT    PRIMARY KEY,
            status        TEXT    NOT NULL DEFAULT 'queued',
            depth         INTEGER NOT NULL DEFAULT 0,
            attempts      INTEGER NOT NULL DEFAULT 0,
            link_count    INTEGER,
            note          TEXT,
            discovered_at TEXT    NOT NULL DEFAULT (datetime('now')),
            visited_at    TEXT
        ) WITHOUT ROWID;
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO meta VALUES ('schema_version', '1');
        INSERT INTO meta VALUES ('base_url', 'https://old.test/');
        INSERT INTO pages (url, status) VALUES ('https://old.test/kept', 'done');
        """
    )
    conn.commit()
    conn.close()


def test_an_older_database_is_migrated_in_place(tmp_path: Path) -> None:
    """Someone's half-finished crawl must survive the upgrade untouched."""
    path = tmp_path / "old.db"
    _make_v1_database(path)

    with UrlStore(path) as store:
        assert store.get_meta("schema_version") == str(SCHEMA_VERSION)
        assert store.get_meta("base_url") == "https://old.test/"
        assert list(store.visited_urls()) == ["https://old.test/kept"]
        # The new column exists and every existing row is due now.
        store.enqueue(["https://old.test/new"], depth=0)
        assert store.claim(max_attempts=3) is not None


def test_migrating_twice_is_harmless(tmp_path: Path) -> None:
    """ALTER TABLE cannot add a column twice, so the step must be idempotent."""
    path = tmp_path / "old.db"
    _make_v1_database(path)
    with UrlStore(path):
        pass
    with UrlStore(path) as store:
        assert store.get_meta("schema_version") == str(SCHEMA_VERSION)


def test_a_newer_database_is_refused_rather_than_downgraded(tmp_path: Path) -> None:
    """create_schema used to stamp the version unconditionally.

    An older build opening a newer file left the newer tables in place and rewrote
    the marker *down*, after which the next upgrade re-ran a migration that had
    already happened.
    """
    path = tmp_path / "future.db"
    with UrlStore(path) as store:
        store.set_meta("schema_version", "99")

    with pytest.raises(StoreError, match="newer spa-sitemap"), UrlStore(path):
        pass

    # ...and the marker was left exactly as it was found.
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "99"
    conn.close()


def test_an_unreadable_schema_version_is_an_error(tmp_path: Path) -> None:
    path = tmp_path / "bad.db"
    with UrlStore(path) as store:
        store.set_meta("schema_version", "banana")

    with pytest.raises(StoreError, match="unreadable schema version"), UrlStore(path):
        pass
