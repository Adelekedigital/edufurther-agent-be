"""URL canonicalization.

Ported from Scholarship Finder's `test_urls.py` and extended. Identical
behaviour is load-bearing: the agent computes identity keys the product has
to agree with, and a canonicalizer that differs by a trailing slash produces
two records where there should be one.
"""

import pytest

from app.tools.urls import canonicalize_url, normalize_domain


def test_tracking_parameters_and_fragments_are_removed():
    assert (
        canonicalize_url("HTTPS://Example.ORG/app/?utm_source=x&view=full#section")
        == "https://example.org/app?view=full"
    )


@pytest.mark.parametrize("param", ["utm_source", "utm_medium", "utm_campaign", "fbclid", "gclid"])
def test_every_tracking_parameter_is_dropped(param):
    assert param not in canonicalize_url(f"https://example.org/a?{param}=x&keep=1")


def test_a_meaningful_query_parameter_is_kept():
    """Dropping these would merge genuinely different pages into one."""
    assert canonicalize_url("https://example.org/search?id=7") == "https://example.org/search?id=7"


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "file:///etc/passwd",
        "ftp://example.org/x",
        "//example.org/x",
        "not-a-url",
        "",
    ],
)
def test_anything_that_is_not_absolute_http_is_refused(url):
    """This is a security boundary, not tidiness: it is what stops
    file://, ftp:// and scheme-relative URLs reaching the fetcher."""
    with pytest.raises(ValueError):
        canonicalize_url(url)


def test_the_host_is_lowercased_but_the_path_is_not():
    """Hosts are case-insensitive; paths are not, and folding them would
    merge two genuinely different pages."""
    assert canonicalize_url("https://EXAMPLE.org/Awards/PhD") == "https://example.org/Awards/PhD"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://example.org:443/a", "https://example.org/a"),
        ("http://example.org:80/a", "http://example.org/a"),
        ("https://example.org:8443/a", "https://example.org:8443/a"),
    ],
)
def test_only_the_default_port_for_the_scheme_is_dropped(url, expected):
    assert canonicalize_url(url) == expected


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://example.org/a/", "https://example.org/a"),
        ("https://example.org/", "https://example.org/"),
        ("https://example.org", "https://example.org/"),
    ],
)
def test_a_trailing_slash_is_normalized_except_at_the_root(url, expected):
    assert canonicalize_url(url) == expected


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("Example.ORG", "example.org"),
        (".example.org", "example.org"),
        ("  example.org  ", "example.org"),
    ],
)
def test_domains_are_normalized_for_the_allowlist(given, expected):
    assert normalize_domain(given) == expected


@pytest.mark.parametrize("given", ["", "   ", "."])
def test_a_blank_domain_is_refused(given):
    """An empty allowlist entry would match nothing - or, worse, be treated
    as a wildcard by a careless comparison."""
    with pytest.raises(ValueError):
        normalize_domain(given)
