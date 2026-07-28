"""SQLite-backed crawl frontier.

One connection per store, opened once and closed on exit -- the previous version
constructed a ``DatabaseManager`` (and therefore a connection) on every loop
iteration and never closed any of them.

The URL is the primary key, so ``INSERT OR IGNORE`` genuinely deduplicates.
Without a uniqueness constraint that clause is a no-op, which is why duplicates
survived the earlier read-then-filter approach.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from types import TracebackType
from typing import Final, Self

log = logging.getLogger(__name__)


class Status(StrEnum):
    """Lifecycle of a URL.

    ``queued`` -> ``done``        rendered successfully
    ``queued`` -> ``queued``      transient failure, attempts left (requeue)
    ``queued`` -> ``failed``      permanent failure or attempts exhausted
    ``queued`` -> ``skipped``     disallowed by robots.txt, or not an HTML document
    ``queued`` -> ``redirected``  server sent us elsewhere; the target carries the page
    ``queued`` -> ``duplicate``   rel=canonical names another URL as the real page
    """

    QUEUED = "queued"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"
    REDIRECTED = "redirected"
    DUPLICATE = "duplicate"


SCHEMA_VERSION: Final = 2

_SCHEMA: Final = f"""
CREATE TABLE IF NOT EXISTS pages (
    url             TEXT    PRIMARY KEY,
    status          TEXT    NOT NULL DEFAULT '{Status.QUEUED}'
                            CHECK (status IN {tuple(s.value for s in Status)!r}),
    depth           INTEGER NOT NULL DEFAULT 0,
    attempts        INTEGER NOT NULL DEFAULT 0,
    link_count      INTEGER,
    note            TEXT,
    discovered_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    visited_at      TEXT,
    -- NULL means "claimable now". A timestamp defers the URL, so a failure backs
    -- off in the frontier instead of in a sleep() -- backoff held in memory is
    -- erased by exactly the kill it exists to survive, and sleeping would block
    -- the loop on one bad URL while thousands of good ones wait.
    next_attempt_at TEXT
) WITHOUT ROWID;

-- Deliberately not extended with next_attempt_at: that is an inequality, and
-- putting it between `status` and the ORDER BY columns would stop SQLite using
-- this index to satisfy `ORDER BY depth, discovered_at, url`. Deferred rows are
-- rare, so filtering them during the ordered scan is cheaper than losing the sort.
CREATE INDEX IF NOT EXISTS idx_pages_frontier
    ON pages (status, depth, discovered_at);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _migrate_to_2(conn: sqlite3.Connection) -> None:
    """Add the retry schedule. NULL means due now, so every existing row is due."""
    if not _has_column(conn, "pages", "next_attempt_at"):
        conn.execute("ALTER TABLE pages ADD COLUMN next_attempt_at TEXT")


#: Applied in key order to bring an older file up to ``SCHEMA_VERSION``. Add-column
#: only, so a migration never rewrites a table full of a user's crawl.
_MIGRATIONS: Final[dict[int, Callable[[sqlite3.Connection], None]]] = {2: _migrate_to_2}


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """Migrations must be safe to re-run: ALTER TABLE cannot add a column twice."""
    return any(row["name"] == column for row in conn.execute(f"PRAGMA table_info({table})"))


def _stamp(moment: datetime) -> str:
    """SQLite's own ``datetime('now')`` text format.

    Written the same way so ``next_attempt_at`` can be compared as text -- the
    format sorts lexicographically, which is why it is safe.
    """
    return moment.strftime("%Y-%m-%d %H:%M:%S")


@dataclass(frozen=True, slots=True)
class Page:
    """A URL claimed from the frontier. ``attempts`` includes the current one."""

    url: str
    depth: int
    attempts: int


class StoreError(RuntimeError):
    """Raised when the database contradicts the requested operation."""


class UrlStore:
    """The crawl frontier and its results.

    Usable as a context manager, which is how the connection gets closed::

        with UrlStore("db/sitemap.db") as store:
            store.enqueue(["https://example.com/"], depth=0)
    """

    def __init__(self, path: Path | str, *, now: Callable[[], datetime] | None = None) -> None:
        self.path = Path(path)
        self._conn: sqlite3.Connection | None = None
        # Injected so retry scheduling is testable without waiting: a fake clock
        # driven by the crawler's fake sleep resolves deferrals instantly and
        # deterministically. UTC because that is what SQLite's datetime('now') is.
        self._now = now or (lambda: datetime.now(UTC))

    # -- lifecycle -----------------------------------------------------------

    def __enter__(self) -> Self:
        self.open()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def open(self) -> None:
        if self._conn is not None:
            return
        if self.path.parent != Path():
            self.path.parent.mkdir(parents=True, exist_ok=True)
        # isolation_level=None: we manage transactions explicitly where batching
        # matters, instead of relying on the implicit-BEGIN legacy behaviour.
        conn = sqlite3.connect(self.path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        self._conn = conn
        self.create_schema()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise StoreError("store is not open; use `with UrlStore(path) as store:`")
        return self._conn

    # -- schema --------------------------------------------------------------

    def create_schema(self) -> None:
        self.conn.executescript(_SCHEMA)
        self._reconcile_version()

    def _reconcile_version(self) -> None:
        """Migrate an older file forward, or refuse to touch a newer one.

        This used to stamp ``schema_version`` unconditionally, so an older build
        opening a newer database left the newer tables in place and rewrote the
        marker *down* -- after which the next upgrade would re-run a migration that
        had already happened. Read before writing, and never assume.
        """
        stored = self.get_meta("schema_version")
        if stored is None:
            self.set_meta("schema_version", str(SCHEMA_VERSION))
            return

        try:
            version = int(stored)
        except ValueError as exc:
            raise StoreError(
                f"{self.path} has an unreadable schema version {stored!r}"
            ) from exc

        if version > SCHEMA_VERSION:
            raise StoreError(
                f"{self.path} was written by a newer spa-sitemap (schema v{version}; "
                f"this build understands v{SCHEMA_VERSION}). Upgrade, or use a different "
                "--database rather than risking the crawl in this one."
            )
        if version == SCHEMA_VERSION:
            return

        self.conn.execute("BEGIN IMMEDIATE")
        try:
            for step in range(version + 1, SCHEMA_VERSION + 1):
                if (migration := _MIGRATIONS.get(step)) is not None:
                    migration(self.conn)
            self.set_meta("schema_version", str(SCHEMA_VERSION))
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        log.info("migrated %s from schema v%d to v%d", self.path, version, SCHEMA_VERSION)

    def reset(self) -> None:
        """Drop everything and start over (the `new` command)."""
        self.conn.executescript("DROP TABLE IF EXISTS pages; DROP TABLE IF EXISTS meta;")
        self.create_schema()

    # -- metadata ------------------------------------------------------------

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    # -- frontier ------------------------------------------------------------

    def enqueue(self, urls: Iterable[str], *, depth: int) -> int:
        """Add URLs at ``depth``, ignoring any already known. Returns how many were new."""
        rows = [(url, depth) for url in dict.fromkeys(urls)]
        if not rows:
            return 0
        before = self.conn.total_changes
        self.conn.execute("BEGIN")
        try:
            self.conn.executemany(
                f"INSERT OR IGNORE INTO pages (url, status, depth) "
                f"VALUES (?, '{Status.QUEUED}', ?)",
                rows,
            )
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        return self.conn.total_changes - before

    def claim(self, *, max_attempts: int) -> Page | None:
        """Hand out the next queued URL, counting the attempt.

        Incrementing ``attempts`` at claim time (not at failure time) is what makes
        a page that crashes the browser mid-render eventually give up instead of
        being retried forever after every restart.

        Ordering is ``depth, discovered_at, url`` -- breadth-first and
        deterministic. The old ``ORDER BY RANDOM()`` sorted the whole table on
        every single page fetch.
        """
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            row = self.conn.execute(
                "SELECT url, depth, attempts FROM pages "
                "WHERE status = ? AND attempts < ? "
                "AND (next_attempt_at IS NULL OR next_attempt_at <= ?) "
                "ORDER BY depth, discovered_at, url LIMIT 1",
                (Status.QUEUED, max_attempts, _stamp(self._now())),
            ).fetchone()
            if row is None:
                self.conn.execute("COMMIT")
                return None
            self.conn.execute(
                "UPDATE pages SET attempts = attempts + 1 WHERE url = ?", (row["url"],)
            )
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        return Page(url=row["url"], depth=row["depth"], attempts=row["attempts"] + 1)

    # -- outcomes ------------------------------------------------------------

    def mark_done(self, url: str, *, link_count: int) -> bool:
        """Record a successful render. Returns ``False`` if it was already done.

        Idempotent because two URLs can redirect to the same target: the second one
        must not be counted as another visited page, or the summary reports more
        pages than the sitemap contains.
        """
        cursor = self.conn.execute(
            "UPDATE pages SET status = ?, link_count = ?, note = NULL, "
            "visited_at = datetime('now') WHERE url = ? AND status != ?",
            (Status.DONE, link_count, url, Status.DONE),
        )
        return cursor.rowcount > 0

    def mark_failed(self, url: str, note: str) -> None:
        self._finish(url, Status.FAILED, note)

    def mark_skipped(self, url: str, note: str) -> None:
        self._finish(url, Status.SKIPPED, note)

    def mark_redirected(self, url: str, target: str) -> None:
        self._finish(url, Status.REDIRECTED, f"-> {target}")

    def mark_duplicate(self, url: str, canonical: str) -> None:
        """Not a page in its own right: rel=canonical points at ``canonical``."""
        self._finish(url, Status.DUPLICATE, f"canonical -> {canonical}")

    def requeue(self, url: str, note: str) -> None:
        """Leave the URL queued after a transient failure, recording why."""
        self.conn.execute(
            "UPDATE pages SET status = ?, note = ?, next_attempt_at = NULL WHERE url = ?",
            (Status.QUEUED, note, url),
        )

    def defer(self, url: str, note: str, seconds: float) -> None:
        """Requeue the URL but hold it back for ``seconds``.

        The wait lives in the row rather than in a ``sleep``, for two reasons. A
        sleep would block the loop on one sick URL while the rest of the frontier
        waits, and an in-memory delay is erased by exactly the crash it exists to
        survive -- resuming would hammer the same URL immediately.
        """
        if seconds <= 0:
            self.requeue(url, note)
            return
        due = _stamp(self._now() + timedelta(seconds=seconds))
        self.conn.execute(
            "UPDATE pages SET status = ?, note = ?, next_attempt_at = ? WHERE url = ?",
            (Status.QUEUED, note, due, url),
        )

    def seconds_until_due(self, *, max_attempts: int) -> float | None:
        """How long until the earliest deferred URL can be claimed.

        ``None`` means there is nothing merely waiting -- so a ``claim`` that
        returned nothing really is an empty frontier. Distinguishing the two keeps
        the crawl from reporting "frontier-empty" while work is still pending,
        which is the same trap `export` and `update` fell into.
        """
        row = self.conn.execute(
            "SELECT MIN(next_attempt_at) AS due FROM pages "
            "WHERE status = ? AND attempts < ? AND next_attempt_at IS NOT NULL",
            (Status.QUEUED, max_attempts),
        ).fetchone()
        if row is None or row["due"] is None:
            return None
        try:
            due = datetime.strptime(row["due"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
        except ValueError:
            return 0.0  # unparseable: treat as due rather than waiting for ever
        return max((due - self._now()).total_seconds(), 0.0)

    def release(self, url: str, note: str) -> None:
        """Requeue a URL *and* refund the attempt ``claim`` charged for it.

        ``claim`` counts the attempt up front, which is what stops a page that
        crashes the browser from being retried forever. When the browser died or
        the site was unreachable, though, that charge is a lie: the URL never got
        a fair navigation, and letting it stand is how an outage burns through
        ``max_attempts`` on pages that were never the problem.
        """
        self.conn.execute(
            "UPDATE pages SET status = ?, note = ?, attempts = MAX(attempts - 1, 0), "
            "next_attempt_at = NULL WHERE url = ?",
            (Status.QUEUED, note, url),
        )

    def _finish(self, url: str, status: Status, note: str) -> None:
        self.conn.execute(
            "UPDATE pages SET status = ?, note = ?, visited_at = datetime('now') "
            "WHERE url = ?",
            (status, note, url),
        )

    # -- reads ---------------------------------------------------------------

    def counts(self) -> dict[str, int]:
        rows = self.conn.execute("SELECT status, COUNT(*) AS n FROM pages GROUP BY status")
        counts = {status.value: 0 for status in Status}
        for row in rows:
            counts[row["status"]] = row["n"]
        return counts

    def total(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) AS n FROM pages").fetchone()["n"])

    def has_pending(self, *, max_attempts: int) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM pages WHERE status = ? AND attempts < ? LIMIT 1",
            (Status.QUEUED, max_attempts),
        ).fetchone()
        return row is not None

    def visited_urls(self) -> Iterator[str]:
        """Successfully rendered URLs, sorted -- the sitemap contents."""
        cursor = self.conn.execute(
            "SELECT url FROM pages WHERE status = ? ORDER BY url", (Status.DONE,)
        )
        for row in cursor:
            yield row["url"]

    def visited_entries(self) -> Iterator[tuple[str, date | None]]:
        """``(url, visited_on)`` for rendered pages -- the source of ``<lastmod>``."""
        cursor = self.conn.execute(
            "SELECT url, visited_at FROM pages WHERE status = ? ORDER BY url",
            (Status.DONE,),
        )
        for row in cursor:
            yield row["url"], _as_date(row["visited_at"])

    def problems(self, limit: int = 20) -> list[tuple[str, str, str]]:
        """``(status, url, note)`` for pages that did not render, for reporting."""
        rows = self.conn.execute(
            "SELECT status, url, COALESCE(note, '') AS note FROM pages "
            "WHERE status IN (?, ?) ORDER BY status, url LIMIT ?",
            (Status.FAILED, Status.SKIPPED, limit),
        )
        return [(row["status"], row["url"], row["note"]) for row in rows]


def _as_date(value: str | None) -> date | None:
    """SQLite stores ``datetime('now')`` as ``YYYY-MM-DD HH:MM:SS`` text."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).date()
    except ValueError:
        return None
