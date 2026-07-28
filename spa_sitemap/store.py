"""SQLite-backed crawl frontier.

One connection per store, opened once and closed on exit -- the previous version
constructed a ``DatabaseManager`` (and therefore a connection) on every loop
iteration and never closed any of them.

The URL is the primary key, so ``INSERT OR IGNORE`` genuinely deduplicates.
Without a uniqueness constraint that clause is a no-op, which is why duplicates
survived the earlier read-then-filter approach.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from pathlib import Path
from types import TracebackType
from typing import Final, Self


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


SCHEMA_VERSION: Final = 1

_SCHEMA: Final = f"""
CREATE TABLE IF NOT EXISTS pages (
    url           TEXT    PRIMARY KEY,
    status        TEXT    NOT NULL DEFAULT '{Status.QUEUED}'
                          CHECK (status IN {tuple(s.value for s in Status)!r}),
    depth         INTEGER NOT NULL DEFAULT 0,
    attempts      INTEGER NOT NULL DEFAULT 0,
    link_count    INTEGER,
    note          TEXT,
    discovered_at TEXT    NOT NULL DEFAULT (datetime('now')),
    visited_at    TEXT
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_pages_frontier
    ON pages (status, depth, discovered_at);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


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

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._conn: sqlite3.Connection | None = None

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
        self.set_meta("schema_version", str(SCHEMA_VERSION))

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
                "ORDER BY depth, discovered_at, url LIMIT 1",
                (Status.QUEUED, max_attempts),
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
            "UPDATE pages SET status = ?, note = ? WHERE url = ?",
            (Status.QUEUED, note, url),
        )

    def release(self, url: str, note: str) -> None:
        """Requeue a URL *and* refund the attempt ``claim`` charged for it.

        ``claim`` counts the attempt up front, which is what stops a page that
        crashes the browser from being retried forever. When the browser died or
        the site was unreachable, though, that charge is a lie: the URL never got
        a fair navigation, and letting it stand is how an outage burns through
        ``max_attempts`` on pages that were never the problem.
        """
        self.conn.execute(
            "UPDATE pages SET status = ?, note = ?, attempts = MAX(attempts - 1, 0) "
            "WHERE url = ?",
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
