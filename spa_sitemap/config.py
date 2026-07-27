"""Configuration: one frozen dataclass, validated at the edge.

The previous loader returned ``{}`` when config.json was missing or malformed, so
the real error surfaced later as a ``KeyError`` in the crawl loop. Here a bad
config raises ``ConfigError`` with a message naming the key, before anything opens
a browser.

Unknown keys are rejected rather than ignored: a silently-ignored ``"max_page"``
typo means the limit you thought you set is not in effect.
"""

from __future__ import annotations

import difflib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, Self
from urllib.parse import urlsplit

from spa_sitemap.robots import DEFAULT_USER_AGENT
from spa_sitemap.urls import CRAWLABLE_SCHEMES, TRACKING_PARAMS, UrlPolicy

DEFAULT_CONFIG_PATH = Path("config.json")

#: Accepted for backwards compatibility with the original config.json.
_ALIASES = {"url": "base_url", "output": "output_path", "database": "database_path"}


class ConfigError(ValueError):
    """Raised for a missing, malformed or invalid configuration."""


@dataclass(frozen=True, slots=True)
class Config:
    """Everything the crawl and the export need to know.

    ``base_url`` is optional because ``export`` and ``status`` operate on an
    existing database, not on a site -- requiring a URL to read back what was
    already crawled would be busywork. The crawl commands ask for it via
    ``policy()``, which fails with a precise message when it is missing.
    """

    base_url: str | None = None

    # politeness & pacing
    delay: float = 1.0
    respect_robots: bool = True
    user_agent: str = DEFAULT_USER_AGENT

    # browser
    headless: bool = True
    window_size: tuple[int, int] = (1440, 980)
    page_load_timeout: float = 30.0
    settle_timeout: float = 8.0
    wait_for_selector: str | None = None

    # scope
    include_subdomains: bool = False
    restrict_to_path: bool = True
    keep_query: bool = True
    hash_routing: bool = False
    respect_canonical: bool = True
    strip_query_params: tuple[str, ...] = tuple(sorted(TRACKING_PARAMS))
    exclude_patterns: tuple[str, ...] = ()

    # limits
    max_pages: int | None = None
    max_depth: int | None = None
    max_runtime: float | None = None
    max_attempts: int = 3

    # paths
    database_path: Path = Path("db/sitemap.db")
    output_path: Path = Path("sitemap.xml")

    def __post_init__(self) -> None:
        self._validate()

    # -- construction --------------------------------------------------------

    @classmethod
    def from_sources(
        cls, path: Path | str | None = None, *, must_exist: bool = False, **overrides: Any
    ) -> Self:
        """Build a config from an optional file plus explicit overrides.

        The config file is genuinely optional: ``--url https://site/`` is enough to
        run, so a target URL never has to be written into a file to be crawlable.
        ``must_exist`` is for when the user named a file explicitly -- silently
        ignoring a missing ``-c other.json`` would be worse than failing.

        Overrides whose value is ``None`` are treated as "not supplied" and leave
        the file's value alone.
        """
        path = Path(path) if path is not None else DEFAULT_CONFIG_PATH
        data: dict[str, Any] = {}

        if path.is_file():
            data.update(cls.read_file(path))
        elif must_exist:
            raise ConfigError(f"config file not found: {path}")

        data.update({key: value for key, value in overrides.items() if value is not None})
        return cls.from_mapping(data)

    @classmethod
    def read_file(cls, path: Path | str) -> Mapping[str, Any]:
        """Parse a config file into a raw mapping, without validating it."""
        path = Path(path)
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise ConfigError(f"config file not found: {path}") from exc
        except OSError as exc:
            raise ConfigError(f"cannot read {path}: {exc}") from exc

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"{path} is not valid JSON: {exc}") from exc

        if not isinstance(data, Mapping):
            raise ConfigError(f"{path} must contain a JSON object, got {type(data).__name__}")
        return data

    @classmethod
    def load(cls, path: Path | str = DEFAULT_CONFIG_PATH) -> Self:
        """Load a config that must exist on disk."""
        return cls.from_mapping(cls.read_file(path))

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> Self:
        known = {field.name for field in fields(cls)}
        kwargs: dict[str, Any] = {}

        for key, value in data.items():
            name = _ALIASES.get(key, key)
            if name not in known:
                suggestion = _closest(name, known)
                hint = f"; did you mean '{suggestion}'?" if suggestion else ""
                raise ConfigError(f"unknown config key '{key}'{hint}")
            kwargs[name] = value

        return cls(**cls._coerce(kwargs))

    @staticmethod
    def _coerce(kwargs: dict[str, Any]) -> dict[str, Any]:
        """JSON gives us lists and strings where we want tuples and paths."""
        for key in ("strip_query_params", "exclude_patterns"):
            if key in kwargs and kwargs[key] is not None:
                kwargs[key] = tuple(str(item) for item in kwargs[key])
        for key in ("database_path", "output_path"):
            if key in kwargs and kwargs[key] is not None:
                kwargs[key] = Path(str(kwargs[key]))
        if kwargs.get("window_size") is not None:
            try:
                width, height = kwargs["window_size"]
                kwargs["window_size"] = (int(width), int(height))
            except (TypeError, ValueError) as exc:
                raise ConfigError("window_size must be [width, height]") from exc
        return kwargs

    # -- derived -------------------------------------------------------------

    def require_base_url(self) -> str:
        """The crawl target, or a ConfigError naming both ways to supply it."""
        if not self.base_url:
            raise ConfigError(
                "no site to crawl: pass --url https://example.com/, "
                f"or set 'base_url' in {DEFAULT_CONFIG_PATH} "
                "(copy config.example.json to start from)"
            )
        return self.base_url

    def policy(self) -> UrlPolicy:
        return UrlPolicy.build(
            self.require_base_url(),
            include_subdomains=self.include_subdomains,
            restrict_to_path=self.restrict_to_path,
            keep_query=self.keep_query,
            hash_routing=self.hash_routing,
            strip_query_params=frozenset(self.strip_query_params),
            exclude_patterns=self.exclude_patterns,
        )

    # -- validation ----------------------------------------------------------

    def _validate(self) -> None:
        if self.base_url is not None:
            parts = urlsplit(self.base_url.strip()) if isinstance(self.base_url, str) else None
            if parts is None or parts.scheme.lower() not in CRAWLABLE_SCHEMES or not parts.netloc:
                raise ConfigError(
                    f"base_url must be an absolute http(s) URL, got {self.base_url!r}"
                )

        for name in ("delay", "page_load_timeout", "settle_timeout"):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
                raise ConfigError(f"{name} must be a non-negative number, got {value!r}")

        for name in ("max_pages", "max_depth", "max_runtime"):
            value = getattr(self, name)
            if value is None:
                continue
            if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
                raise ConfigError(f"{name} must be a positive number or null, got {value!r}")

        if not isinstance(self.max_attempts, int) or self.max_attempts < 1:
            raise ConfigError(f"max_attempts must be an integer >= 1, got {self.max_attempts!r}")

        for name in (
            "respect_robots",
            "headless",
            "include_subdomains",
            "restrict_to_path",
            "keep_query",
            "hash_routing",
            "respect_canonical",
        ):
            if not isinstance(getattr(self, name), bool):
                raise ConfigError(f"{name} must be true or false, got {getattr(self, name)!r}")

        # Building the policy compiles exclude_patterns and parses the scope, so a
        # bad regex or an unusable base URL is reported here rather than mid-crawl.
        try:
            if self.base_url is not None:
                self.policy()
            else:
                tuple(re.compile(pattern) for pattern in self.exclude_patterns)
        except (ValueError, re.error) as exc:
            raise ConfigError(str(exc)) from exc


def _closest(name: str, candidates: set[str]) -> str | None:
    """Typo hint for a rejected config key."""
    matches = difflib.get_close_matches(name, sorted(candidates), n=1, cutoff=0.7)
    return matches[0] if matches else None
