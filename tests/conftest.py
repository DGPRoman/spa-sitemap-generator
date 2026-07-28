"""Shared fixtures: a throwaway HTTP server for the JS-rendered fixture site."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


class FakeClock:
    """A wall clock that a fake sleep drives forward.

    Retries are scheduled against wall time in the database, so a sleep that does
    nothing would leave every deferred URL permanently in the future. Wiring the
    two together lets the real, shipped RetryPolicy be exercised by the ordinary
    tests -- in microseconds, and without opting them out of what users get.
    """

    def __init__(self) -> None:
        self.now = datetime(2026, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)

FIXTURE_SITE = Path(__file__).parent / "fixtures" / "site"


#: Paths the fixture server answers specially, so the crawler's redirect handling
#: can be exercised against a real browser rather than only a fake renderer.
REDIRECTS = {
    "/moved.html": "/about.html",       # permanent redirect to another page
    "/products": "/products/index.html",  # the classic missing-trailing-slash case
}


class _QuietHandler(SimpleHTTPRequestHandler):
    """SimpleHTTPRequestHandler with redirects, and without the per-request spam."""

    def do_GET(self) -> None:
        if (target := REDIRECTS.get(self.path)) is not None:
            self.send_response(301)
            self.send_header("Location", target)
            self.end_headers()
            return
        super().do_GET()

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass


@pytest.fixture(scope="session")
def site_url() -> Iterator[str]:
    """Serve tests/fixtures/site on a random port; yield its base URL."""
    handler = partial(_QuietHandler, directory=str(FIXTURE_SITE))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
