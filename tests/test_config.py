"""Config parsing and validation.

The behaviour under test is mostly "bad input fails loudly, here, with a message
naming the problem" -- the old loader returned {} and let it fail later as a KeyError.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from spa_sitemap.config import Config, ConfigError

MINIMAL = {"base_url": "https://example.com/"}


def write_config(tmp_path: Path, data: object) -> Path:
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


# -- loading -----------------------------------------------------------------


def test_loads_a_minimal_config(tmp_path: Path) -> None:
    config = Config.load(write_config(tmp_path, MINIMAL))
    assert config.base_url == "https://example.com/"
    assert config.delay == 1.0
    assert config.sites_dir == Path("sites")


def test_a_missing_file_names_the_file_and_the_fix(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="config file not found"):
        Config.load(tmp_path / "nope.json")


def test_malformed_json_is_reported_as_such(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ConfigError, match="not valid JSON"):
        Config.load(path)


def test_a_json_list_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="must contain a JSON object"):
        Config.load(write_config(tmp_path, ["https://a/"]))


def test_a_config_without_a_target_is_valid_but_cannot_crawl(tmp_path: Path) -> None:
    """`export` and `status` read a database, so they need no URL; crawling does."""
    config = Config.load(write_config(tmp_path, {"delay": 2}))
    assert config.base_url is None

    with pytest.raises(ConfigError, match=r"--url"):
        config.require_base_url()
    with pytest.raises(ConfigError, match=r"--url"):
        config.policy()


# -- backwards compatibility -------------------------------------------------


def test_the_original_url_key_still_works(tmp_path: Path) -> None:
    """The old config.json was {"url": ..., "delay": ...}; it must keep working."""
    config = Config.load(write_config(tmp_path, {"url": "https://example.com/", "delay": 5}))
    assert config.base_url == "https://example.com/"
    assert config.delay == 5


# -- unknown keys ------------------------------------------------------------


def test_an_unknown_key_is_rejected_not_ignored(tmp_path: Path) -> None:
    """A silently-ignored typo means the limit you set is not in effect."""
    with pytest.raises(ConfigError, match="unknown config key 'maximum_pages'"):
        Config.from_mapping({**MINIMAL, "maximum_pages": 5})


def test_a_near_miss_gets_a_suggestion() -> None:
    with pytest.raises(ConfigError, match="did you mean 'max_pages'"):
        Config.from_mapping({**MINIMAL, "max_page": 5})


# -- validation --------------------------------------------------------------


@pytest.mark.parametrize(
    "base_url",
    ["", "example.com", "/relative", "ftp://example.com", "mailto:a@b.c", "https://"],
)
def test_an_unusable_base_url_is_rejected(base_url: str) -> None:
    with pytest.raises(ConfigError, match="base_url"):
        Config(base_url=base_url)


@pytest.mark.parametrize("delay", [-1, -0.5])
def test_a_negative_delay_is_rejected(delay: float) -> None:
    with pytest.raises(ConfigError, match="delay"):
        Config(**MINIMAL, delay=delay)


def test_a_boolean_delay_is_rejected() -> None:
    """bool is an int in Python; accepting it would silently mean delay=1."""
    with pytest.raises(ConfigError, match="delay"):
        Config(**MINIMAL, delay=True)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field", "value"), [("max_pages", 0), ("max_depth", -1), ("max_runtime", 0)]
)
def test_limits_must_be_positive_or_absent(field: str, value: int) -> None:
    with pytest.raises(ConfigError, match=field):
        Config(**MINIMAL, **{field: value})


@pytest.mark.parametrize("field", ["max_pages", "max_depth", "max_runtime"])
def test_limits_may_be_null(field: str) -> None:
    assert getattr(Config(**MINIMAL, **{field: None}), field) is None


def test_max_attempts_must_allow_at_least_one_try() -> None:
    with pytest.raises(ConfigError, match="max_attempts"):
        Config(**MINIMAL, max_attempts=0)


def test_a_non_boolean_flag_is_rejected() -> None:
    with pytest.raises(ConfigError, match="headless"):
        Config(**MINIMAL, headless="yes")  # type: ignore[arg-type]


def test_an_invalid_exclude_pattern_is_reported_before_the_crawl() -> None:
    with pytest.raises(ConfigError):
        Config(**MINIMAL, exclude_patterns=("[unclosed",))


# -- coercion ----------------------------------------------------------------


def test_json_lists_and_strings_become_tuples_and_paths(tmp_path: Path) -> None:
    config = Config.load(
        write_config(
            tmp_path,
            {
                **MINIMAL,
                "exclude_patterns": ["/logout"],
                "strip_query_params": ["sid"],
                "sites_dir": "crawls",
                "window_size": [800, 600],
            },
        )
    )
    assert config.exclude_patterns == ("/logout",)
    assert config.strip_query_params == ("sid",)
    assert config.sites_dir == Path("crawls")
    assert config.window_size == (800, 600)


def test_a_malformed_window_size_is_rejected() -> None:
    with pytest.raises(ConfigError, match="window_size"):
        Config.from_mapping({**MINIMAL, "window_size": [800]})


# -- overrides ---------------------------------------------------------------


def test_an_override_is_validated_like_any_other_value(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="base_url"):
        Config.from_sources(tmp_path / "absent.json", base_url="not-a-url")


# -- derived policy ----------------------------------------------------------


def test_the_policy_reflects_the_scope_settings() -> None:
    policy = Config(
        base_url="https://example.com/docs/", include_subdomains=True, hash_routing=True
    ).policy()

    assert policy.scope.path_prefix == "/docs/"
    assert policy.scope.include_subdomains is True
    assert policy.hash_routing is True


# -- config file is optional -------------------------------------------------


def test_a_url_alone_needs_no_config_file(tmp_path: Path) -> None:
    """A target URL must never have to be written to a file to be crawlable."""
    config = Config.from_sources(tmp_path / "absent.json", base_url="https://example.com/")
    assert config.base_url == "https://example.com/"
    assert config.delay == 1.0


def test_a_named_config_file_must_exist(tmp_path: Path) -> None:
    """Silently ignoring an explicit -c would hide a typo in the path."""
    with pytest.raises(ConfigError, match="config file not found"):
        Config.from_sources(
            tmp_path / "absent.json", must_exist=True, base_url="https://example.com/"
        )


def test_neither_file_nor_url_names_both_ways_out(tmp_path: Path) -> None:
    config = Config.from_sources(tmp_path / "absent.json")
    with pytest.raises(ConfigError, match=r"--url.*base_url"):
        config.require_base_url()


def test_overrides_win_over_the_file(tmp_path: Path) -> None:
    path = write_config(tmp_path, {"base_url": "https://from-file.test/", "delay": 9})
    config = Config.from_sources(path, base_url="https://from-cli.test/")

    assert config.base_url == "https://from-cli.test/"
    assert config.delay == 9  # untouched by the override


def test_unsupplied_overrides_do_not_erase_file_values(tmp_path: Path) -> None:
    path = write_config(tmp_path, {"base_url": "https://a.test/", "delay": 4})
    config = Config.from_sources(path, base_url=None, delay=None, max_pages=7)

    assert (config.base_url, config.delay, config.max_pages) == ("https://a.test/", 4, 7)


def test_the_url_alias_also_satisfies_the_requirement(tmp_path: Path) -> None:
    path = write_config(tmp_path, {"url": "https://legacy.test/"})
    assert Config.from_sources(path).base_url == "https://legacy.test/"


def test_a_directory_in_place_of_a_config_is_not_read(tmp_path: Path) -> None:
    """is_file() rather than exists(): a directory must not be opened as JSON."""
    (tmp_path / "config.json").mkdir()
    config = Config.from_sources(tmp_path / "config.json", base_url="https://a.test/")
    assert config.base_url == "https://a.test/"


def test_the_example_config_is_actually_usable() -> None:
    """README tells you to `cp config.example.json config.json` and go.

    Nothing else covers that file, so a key renamed in Config or a stale key left
    in the example would break a new user's very first command -- and only theirs.
    """
    config = Config.load(Path(__file__).parent.parent / "config.example.json")
    assert config.base_url == "https://example.com/"
    assert config.max_restarts == 3
    assert config.max_consecutive_failures == 10


def test_every_documented_key_appears_in_the_example(tmp_path: Path) -> None:
    """The example is the discoverable list of settings; drift makes it a liar."""
    from dataclasses import fields

    example = json.loads((Path(__file__).parent.parent / "config.example.json").read_text())
    known = {field.name for field in fields(Config)}
    assert set(example) <= known, f"example has keys Config rejects: {set(example) - known}"
