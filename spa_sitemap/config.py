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
from dataclasses import dataclass, fields, replace
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
    """Everything the crawl and the export need to know."""

    base_url: str

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
    def load(cls, path: Path | str = DEFAULT_CONFIG_PATH) -> Self:
        path = Path(path)
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise ConfigError(
                f"config file not found: {path}. Copy config.example.json to {path} "
                f"and set 'base_url', or pass --url."
            ) from exc
        except OSError as exc:
            raise ConfigError(f"cannot read {path}: {exc}") from exc

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"{path} is not valid JSON: {exc}") from exc

        if not isinstance(data, Mapping):
            raise ConfigError(f"{path} must contain a JSON object, got {type(data).__name__}")
        return cls.from_mapping(data)

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

        if "base_url" not in kwargs:
            raise ConfigError("config must set 'base_url' (the URL to start crawling from)")

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

    def with_overrides(self, **overrides: Any) -> Self:
        """Apply CLI overrides, ignoring the ones that were not supplied."""
        supplied = {key: value for key, value in overrides.items() if value is not None}
        return replace(self, **supplied) if supplied else self

    # -- derived -------------------------------------------------------------

    def policy(self) -> UrlPolicy:
        return UrlPolicy.build(
            self.base_url,
            include_subdomains=self.include_subdomains,
            restrict_to_path=self.restrict_to_path,
            keep_query=self.keep_query,
            hash_routing=self.hash_routing,
            strip_query_params=frozenset(self.strip_query_params),
            exclude_patterns=self.exclude_patterns,
        )

    # -- validation ----------------------------------------------------------

    def _validate(self) -> None:
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
            self.policy()
        except (ValueError, re.error) as exc:
            raise ConfigError(str(exc)) from exc


def _closest(name: str, candidates: set[str]) -> str | None:
    """Typo hint for a rejected config key."""
    matches = difflib.get_close_matches(name, sorted(candidates), n=1, cutoff=0.7)
    return matches[0] if matches else None
