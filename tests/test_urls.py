"""Canonicalisation and scope rules -- pure functions, so these are the cheap tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from spa_sitemap.urls import Scope, ScopeError, UrlPolicy, same_site

BASE = "https://example.com/"


@pytest.fixture
def policy() -> UrlPolicy:
    return UrlPolicy.build(BASE)


# -- scope construction ------------------------------------------------------


def test_scope_defaults_to_the_whole_host() -> None:
    scope = Scope.from_url("https://example.com")
    assert (scope.scheme, scope.host, scope.port, scope.path_prefix) == (
        "https", "example.com", None, "/",
    )


def test_scope_narrows_to_the_directory_of_a_file_base() -> None:
    assert Scope.from_url("https://example.com/docs/guide.html").path_prefix == "/docs/"


def test_scope_keeps_an_explicit_directory_base() -> None:
    assert Scope.from_url("https://example.com/docs/").path_prefix == "/docs/"


def test_restrict_to_path_off_widens_to_the_host() -> None:
    scope = Scope.from_url("https://example.com/docs/", restrict_to_path=False)
    assert scope.path_prefix == "/"


@pytest.mark.parametrize("base", ["ftp://example.com", "mailto:a@b.c", "not a url", "/relative"])
def test_scope_rejects_bases_that_cannot_anchor_a_crawl(base: str) -> None:
    with pytest.raises(ScopeError):
        Scope.from_url(base)


@pytest.mark.parametrize(
    ("url", "port"),
    [("https://example.com", None), ("https://example.com:443", None),
     ("http://example.com:80", None), ("http://example.com:8080", 8080)],
)
def test_default_ports_collapse(url: str, port: int | None) -> None:
    assert Scope.from_url(url).port == port


# -- links that must be followed ---------------------------------------------


@pytest.mark.parametrize(
    ("href", "expected"),
    [
        ("/about", "https://example.com/about"),
        ("about", "https://example.com/about"),
        ("./about", "https://example.com/about"),
        ("https://example.com/about", "https://example.com/about"),
        ("//example.com/about", "https://example.com/about"),
        ("  /about  ", "https://example.com/about"),
        ("/a/../b", "https://example.com/b"),
        ("/dir/", "https://example.com/dir/"),
    ],
)
def test_relative_and_absolute_hrefs_resolve(policy: UrlPolicy, href: str, expected: str) -> None:
    assert policy.normalise(href, page_url=BASE) == expected


def test_relative_href_resolves_against_the_page_not_the_base(policy: UrlPolicy) -> None:
    got = policy.normalise("b.html", page_url="https://example.com/docs/a.html")
    assert got == "https://example.com/docs/b.html"


def test_http_link_is_canonicalised_to_the_scope_scheme(policy: UrlPolicy) -> None:
    """Sites mix http and https in markup while serving one set of documents."""
    assert policy.normalise("http://example.com/a", page_url=BASE) == "https://example.com/a"


def test_uppercase_host_is_lowercased(policy: UrlPolicy) -> None:
    assert policy.normalise("https://EXAMPLE.com/a", page_url=BASE) == "https://example.com/a"


def test_path_case_is_preserved(policy: UrlPolicy) -> None:
    """Hosts are case-insensitive; paths are not."""
    assert policy.normalise("/About", page_url=BASE) == "https://example.com/About"


# -- links that must be dropped ----------------------------------------------


@pytest.mark.parametrize(
    "href",
    [
        None, "", "   ",
        "mailto:hi@example.com",
        "tel:+1234567890",
        "javascript:void(0)",
        "data:text/html,<p>x",
        "https://other.example/a",          # different host
        "https://evil-example.com/a",       # look-alike suffix
        "https://example.com.evil.net/a",   # look-alike prefix
        "https://sub.example.com/a",        # subdomain, not enabled
        "http://example.com:8080/a",        # different port
        "/brochure.pdf", "/img/logo.PNG", "/app.js", "/style.css", "/data.json",
    ],
)
def test_uncrawlable_hrefs_are_dropped(policy: UrlPolicy, href: str | None) -> None:
    assert policy.normalise(href, page_url=BASE) is None


def test_lookalike_host_would_pass_a_naive_prefix_check(policy: UrlPolicy) -> None:
    """The regression this replaces: `startswith(base)` let the crawler off-site."""
    href = "https://example.com.evil.net/phish"
    assert href.startswith("https://example.com")  # what the old check tested
    assert policy.normalise(href, page_url=BASE) is None


def test_out_of_scope_path_is_dropped() -> None:
    policy = UrlPolicy.build("https://example.com/docs/")
    assert policy.normalise("/blog/post", page_url="https://example.com/docs/") is None
    assert policy.normalise("/docs/x", page_url="https://example.com/docs/") is not None


def test_subdomains_can_be_opted_into() -> None:
    policy = UrlPolicy.build(BASE, include_subdomains=True)
    assert policy.normalise("https://sub.example.com/a", page_url=BASE) == (
        "https://sub.example.com/a"
    )
    assert policy.normalise("https://evil-example.com/a", page_url=BASE) is None


def test_exclude_patterns_drop_matching_urls() -> None:
    policy = UrlPolicy.build(BASE, exclude_patterns=(r"/logout\b", r"\?action=delete"))
    assert policy.normalise("/logout", page_url=BASE) is None
    assert policy.normalise("/x?action=delete", page_url=BASE) is None
    assert policy.normalise("/login", page_url=BASE) is not None


def test_malformed_port_does_not_raise(policy: UrlPolicy) -> None:
    assert policy.normalise("https://example.com:notaport/a", page_url=BASE) is None


# -- fragments ---------------------------------------------------------------


@pytest.mark.parametrize("href", ["#top", "/a#top", "#", "/a#"])
def test_fragments_are_stripped_by_default(policy: UrlPolicy, href: str) -> None:
    got = policy.normalise(href, page_url=BASE)
    assert got is not None
    assert "#" not in got


def test_anchor_only_link_normalises_to_the_page_itself(policy: UrlPolicy) -> None:
    assert policy.normalise("#section", page_url="https://example.com/a") == (
        "https://example.com/a"
    )


class TestHashRouting:
    """The old code dropped every href containing '#', which made a hash-routed
    SPA -- the case this tool exists for -- uncrawlable."""

    @pytest.fixture
    def policy(self) -> UrlPolicy:
        return UrlPolicy.build(BASE, hash_routing=True)

    @pytest.mark.parametrize(
        ("href", "expected"),
        [
            ("#/products", "https://example.com/#/products"),
            ("#!/products", "https://example.com/#!/products"),
            ("/#/products/1", "https://example.com/#/products/1"),
        ],
    )
    def test_route_fragments_are_kept(self, policy: UrlPolicy, href: str, expected: str) -> None:
        assert policy.normalise(href, page_url=BASE) == expected

    def test_plain_anchors_are_still_stripped(self, policy: UrlPolicy) -> None:
        assert policy.normalise("#top", page_url=BASE) == BASE

    def test_routes_resolve_against_the_current_page(self, policy: UrlPolicy) -> None:
        got = policy.normalise("#/b", page_url="https://example.com/#/a")
        assert got == "https://example.com/#/b"


# -- query strings -----------------------------------------------------------


def test_tracking_parameters_are_stripped(policy: UrlPolicy) -> None:
    got = policy.normalise("/c?utm_source=nav&utm_medium=cpc&id=7", page_url=BASE)
    assert got == "https://example.com/c?id=7"


def test_meaningful_parameters_survive(policy: UrlPolicy) -> None:
    assert policy.normalise("/c?page=2", page_url=BASE) == "https://example.com/c?page=2"


def test_parameter_order_does_not_create_duplicates(policy: UrlPolicy) -> None:
    first = policy.normalise("/c?b=2&a=1", page_url=BASE)
    second = policy.normalise("/c?a=1&b=2", page_url=BASE)
    assert first == second == "https://example.com/c?a=1&b=2"


def test_only_tracking_parameters_leaves_a_bare_url(policy: UrlPolicy) -> None:
    assert policy.normalise("/c?fbclid=abc", page_url=BASE) == "https://example.com/c"


def test_keep_query_off_discards_every_parameter() -> None:
    policy = UrlPolicy.build(BASE, keep_query=False)
    assert policy.normalise("/c?page=2", page_url=BASE) == "https://example.com/c"


def test_custom_strip_list_replaces_the_default() -> None:
    policy = UrlPolicy.build(BASE, strip_query_params=frozenset({"sid"}))
    assert policy.normalise("/c?sid=1&utm_source=x", page_url=BASE) == (
        "https://example.com/c?utm_source=x"
    )


def test_blank_valued_parameters_are_preserved(policy: UrlPolicy) -> None:
    assert policy.normalise("/c?flag=", page_url=BASE) == "https://example.com/c?flag="


# -- batch normalisation -----------------------------------------------------


def test_normalise_all_dedupes_and_keeps_order(policy: UrlPolicy) -> None:
    hrefs = [
        "/b", "/a", "/b",                     # duplicate
        "/a?utm_source=x",                    # duplicate after stripping
        "mailto:x@y.z", None, "#top",         # dropped or folded into the page
        "https://other.example/z",            # off-site
    ]
    assert policy.normalise_all(hrefs, page_url="https://example.com/a") == [
        "https://example.com/b",
        "https://example.com/a",
    ]


def test_normalise_all_on_an_empty_page(policy: UrlPolicy) -> None:
    assert policy.normalise_all([], page_url=BASE) == []


# -- comparing two base URLs -------------------------------------------------


@pytest.mark.parametrize(
    ("one", "other"),
    [
        ("https://example.com/", "https://example.com"),        # trailing slash
        ("https://example.com/", "https://EXAMPLE.com/"),       # host case
        ("https://example.com/", "https://example.com:443/"),   # default port
        ("https://example.com/docs/", "https://example.com/docs/guide.html"),
    ],
)
def test_the_same_site_written_differently(one: str, other: str) -> None:
    assert same_site(one, other)
    assert same_site(other, one)


@pytest.mark.parametrize(
    ("one", "other"),
    [
        ("https://example.com/", "https://other.test/"),        # different host
        ("https://example.com/", "https://sub.example.com/"),   # a subdomain is not the site
        ("https://example.com/", "http://example.com/"),        # different key space
        ("https://example.com/", "https://example.com:8080/"),  # different port
        ("https://example.com/docs/", "https://example.com/blog/"),  # scope is not the host
    ],
)
def test_sites_that_must_not_be_treated_as_one(one: str, other: str) -> None:
    assert not same_site(one, other)
    assert not same_site(other, one)


def test_an_unusable_url_matches_nothing_including_itself() -> None:
    """If we cannot tell what a database holds, nobody may be told it matches."""
    assert not same_site("not a url", "not a url")
    assert not same_site("https://example.com/", "mailto:x@y.z")


# -- naming a scope's own directory ------------------------------------------


@pytest.mark.parametrize(
    ("url", "slug"),
    [
        ("https://example.com/", "example.com"),
        ("https://EXAMPLE.com/", "example.com"),
        ("https://example.com:8080/", "example.com_8080"),
        ("https://example.com:443/", "example.com"),        # default port folds away
        ("http://127.0.0.1:48801/index.html", "127.0.0.1_48801"),
        # Two scopes on one host must not share a directory -- the reason a slug
        # cannot simply be the host name.
        ("https://example.com/docs/", "example.com_docs"),
        ("https://example.com/blog/guide.html", "example.com_blog"),
        ("https://example.com/a/b/", "example.com_a_b"),
    ],
)
def test_a_scope_names_its_own_directory(url: str, slug: str) -> None:
    assert Scope.from_url(url).slug == slug


def test_an_international_host_becomes_punycode() -> None:
    """A directory name has to be ASCII to be portable across filesystems."""
    assert Scope.from_url("https://приклад.укр/").slug.startswith("xn--")


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/../x/",
        "https://example.com/.hidden/",
        "https://example.com/..%2F..%2F/",
        "https://example.com/a b/c'd/",
        "https://xn--/",
    ],
)
def test_a_slug_is_always_one_harmless_directory_name(url: str) -> None:
    """The slug becomes a path, so the invariant is that it cannot mean anything
    but a single child directory: never a separator, never `.` or `..`, and never
    a leading dot that would hide it."""
    slug = Scope.from_url(url).slug
    assert Path(slug).parts == (slug,)
    assert slug not in {".", ".."}
    assert not slug.startswith(".")
