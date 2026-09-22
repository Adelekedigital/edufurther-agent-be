"""The outbound fetch guard is this service's SSRF boundary.

Ported from Scholarship Finder's `test_source_fetch_guard.py` and extended
to cover the fetch path itself, not just URL validation. The extensions
matter: the original proved the validator rejects a bad URL, but not that
the fetcher re-validates after a redirect - which is where an allowlist is
most easily escaped.
"""

import httpx
import pytest

from app.tools import direct_fetch
from app.tools.direct_fetch import _is_public_address, fetch_source, validate_source_url

APPROVED = ["example.test"]


@pytest.fixture
def public_dns(monkeypatch):
    """Resolve every hostname to a public address.

    Keeps these tests off the network and deterministic; the resolution
    behaviour itself is covered separately below.
    """
    monkeypatch.setattr(direct_fetch, "_is_public_host", lambda hostname: True)


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",  # loopback
        "10.0.0.5",  # RFC 1918
        "192.168.1.1",
        "172.16.0.1",
        "169.254.169.254",  # cloud instance metadata
        "100.64.0.4",  # RFC 6598, the platform proxy's own network
        "100.127.255.254",
        "224.0.0.1",  # multicast
        "0.0.0.0",  # unspecified
        "::1",
        "fd00::1",  # unique local
    ],
)
def test_non_routable_addresses_are_refused(address):
    assert _is_public_address(address) is False


@pytest.mark.parametrize(
    "address", ["93.184.216.34", "8.8.8.8", "2606:2800:220:1:248:1893:25c8:1946"]
)
def test_globally_routable_addresses_are_allowed(address):
    assert _is_public_address(address) is True


@pytest.mark.parametrize(
    "url",
    [
        "https://attacker.test/page",
        "https://example.test.attacker.test/page",
        "https://notexample.test/page",
    ],
)
def test_urls_outside_the_approved_domains_are_refused(url):
    """The middle case is the one worth keeping: a suffix match without the
    dot boundary would accept `example.test.attacker.test`."""
    with pytest.raises(ValueError, match="not approved"):
        validate_source_url(url, APPROVED)


def test_subdomains_of_an_approved_domain_are_allowed(public_dns):
    assert validate_source_url("https://awards.example.test/page", APPROVED).startswith(
        "https://awards.example.test/page"
    )


def test_a_private_approved_host_is_still_refused(monkeypatch):
    """Domain approval must not override the network guard."""
    monkeypatch.setattr(direct_fetch, "_is_public_host", lambda hostname: False)
    with pytest.raises(ValueError, match="Private or reserved"):
        validate_source_url("https://example.test/page", APPROVED)


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.test/x", "//example.test/x"])
def test_non_http_schemes_are_refused(url):
    with pytest.raises(ValueError):
        validate_source_url(url, APPROVED)


def test_a_host_resolving_to_both_public_and_private_is_refused(monkeypatch):
    """`all`, not `any`. A host that answers with one public and one private
    address is a rebinding attempt, not a partially valid target."""
    monkeypatch.setattr(
        direct_fetch.socket,
        "getaddrinfo",
        lambda *a, **k: [
            (None, None, None, None, ("93.184.216.34", 0)),
            (None, None, None, None, ("127.0.0.1", 0)),
        ],
    )
    with pytest.raises(ValueError, match="Private or reserved"):
        validate_source_url("https://example.test/page", APPROVED)


def test_an_unresolvable_host_is_refused_rather_than_attempted(monkeypatch):
    import socket as socket_module

    def boom(*args, **kwargs):
        raise socket_module.gaierror("no such host")

    monkeypatch.setattr(direct_fetch.socket, "getaddrinfo", boom)
    with pytest.raises(ValueError, match="could not be resolved"):
        validate_source_url("https://example.test/page", APPROVED)


# --- the fetch path ---------------------------------------------------


def transport(monkeypatch, handler):
    original = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original(*args, **kwargs)

    monkeypatch.setattr(direct_fetch.httpx, "AsyncClient", patched)


async def test_a_page_is_fetched_and_returned(public_dns, monkeypatch):
    transport(
        monkeypatch,
        lambda request: httpx.Response(
            200, text="a scholarship", headers={"content-type": "text/html"}
        ),
    )

    page = await fetch_source("https://example.test/award", APPROVED)

    assert page.status_code == 200
    assert page.text == "a scholarship"
    assert page.content_type == "text/html"
    assert page.url == "https://example.test/award"


async def test_a_redirect_to_an_unapproved_host_is_refused(public_dns, monkeypatch):
    """The reason `follow_redirects=False` is not negotiable: httpx's own
    redirect following would take this hop without re-checking it."""
    transport(
        monkeypatch,
        lambda request: (
            httpx.Response(302, headers={"location": "https://attacker.test/x"})
            if "example.test" in str(request.url)
            else httpx.Response(200, text="stolen")
        ),
    )

    with pytest.raises(ValueError, match="not approved"):
        await fetch_source("https://example.test/award", APPROVED)


async def test_a_redirect_within_the_allowlist_is_followed_once(public_dns, monkeypatch):
    seen = []

    def handler(request):
        seen.append(str(request.url))
        if request.url.path == "/award":
            return httpx.Response(301, headers={"location": "https://example.test/award-2026"})
        return httpx.Response(200, text="the real page")

    transport(monkeypatch, handler)

    page = await fetch_source("https://example.test/award", APPROVED)

    assert page.text == "the real page"
    assert page.url == "https://example.test/award-2026"
    assert len(seen) == 2


async def test_only_one_redirect_hop_is_taken(public_dns, monkeypatch):
    """A redirect chain is not followed indefinitely; the second hop's own
    3xx is returned as-is rather than pursued."""
    transport(
        monkeypatch,
        lambda request: httpx.Response(302, headers={"location": "https://example.test/next"}),
    )

    page = await fetch_source("https://example.test/award", APPROVED)

    assert 300 <= page.status_code < 400


async def test_a_relative_redirect_is_resolved_rather_than_rejected(public_dns, monkeypatch):
    """Extends the original, which required an absolute Location and so
    failed on a very common real-world response. Resolving it loses no
    safety - the result is still validated against the allowlist."""

    def handler(request):
        if request.url.path == "/award":
            return httpx.Response(302, headers={"location": "/award/2026"})
        return httpx.Response(200, text="resolved")

    transport(monkeypatch, handler)

    page = await fetch_source("https://example.test/award", APPROVED)

    assert page.text == "resolved"
    assert page.url == "https://example.test/award/2026"


async def test_a_relative_redirect_cannot_escape_the_allowlist(public_dns, monkeypatch):
    """Resolution happens against the current URL, so a relative Location
    always stays on the same host - but the result is validated anyway."""

    def handler(request):
        if request.url.path == "/award":
            return httpx.Response(302, headers={"location": "//attacker.test/x"})
        return httpx.Response(200, text="stolen")

    transport(monkeypatch, handler)

    with pytest.raises(ValueError, match="not approved"):
        await fetch_source("https://example.test/award", APPROVED)


async def test_a_redirect_without_a_destination_is_refused(public_dns, monkeypatch):
    transport(monkeypatch, lambda request: httpx.Response(302))

    with pytest.raises(ValueError, match="no destination"):
        await fetch_source("https://example.test/award", APPROVED)


async def test_an_oversized_response_is_refused(public_dns, monkeypatch):
    transport(monkeypatch, lambda request: httpx.Response(200, content=b"x" * 5_000))

    with pytest.raises(ValueError, match="exceeds maximum size"):
        await fetch_source("https://example.test/award", APPROVED, max_bytes=1_000)


async def test_a_response_at_the_limit_is_accepted(public_dns, monkeypatch):
    transport(monkeypatch, lambda request: httpx.Response(200, content=b"x" * 1_000))

    page = await fetch_source("https://example.test/award", APPROVED, max_bytes=1_000)

    assert len(page.content) == 1_000


async def test_the_size_limit_stops_reading_rather_than_measuring_afterwards(
    public_dns, monkeypatch
):
    """These are arbitrary third-party pages: the limit has to bound the
    work done, not just reject the result once it is all in memory."""
    delivered = 0

    def handler(request):
        async def stream():
            nonlocal delivered
            for _ in range(100):
                delivered += 1_000
                yield b"x" * 1_000

        return httpx.Response(200, content=stream())

    transport(monkeypatch, handler)

    with pytest.raises(ValueError, match="exceeds maximum size"):
        await fetch_source("https://example.test/award", APPROVED, max_bytes=2_000)

    assert delivered < 100_000, "the whole body was downloaded before being rejected"


async def test_the_user_agent_identifies_this_service(public_dns, monkeypatch):
    """An operator reading their own access log should be able to tell who
    is fetching, and a blocked crawler should be attributable."""
    seen = {}

    def handler(request):
        seen["ua"] = request.headers.get("user-agent")
        return httpx.Response(200, text="ok")

    transport(monkeypatch, handler)
    await fetch_source("https://example.test/award", APPROVED)

    assert seen["ua"] == "EdufurtherAgent/0.1"
