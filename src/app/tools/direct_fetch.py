"""Outbound page fetching, and the SSRF boundary around it.

Ported from Scholarship Finder's `infra/source_fetch.py`. The subtleties
here are deliberate and every one of them must survive the move:

* an **allowlist**, not a blocklist - anything not globally routable is
  refused, rather than enumerating non-routable ranges and missing one;
* `all()`, not `any()`, across resolved addresses - a hostname that
  resolves to one public and one private address is refused;
* carrier-grade NAT excluded explicitly, because CPython stopped reporting
  it as private in 3.12.4 and it is exactly where the platform proxy and
  the sibling services live;
* `follow_redirects=False` with exactly one manually re-validated hop -
  httpx's own redirect following would bypass the allowlist entirely.

Two deliberate changes from the original, both noted at their call sites:
a relative `Location` is now resolved rather than rejected, and the size
limit is enforced while streaming rather than after the whole body has
already been downloaded.
"""

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from app.tools.urls import canonicalize_url

USER_AGENT = "EdufurtherAgent/0.1"

DEFAULT_MAX_BYTES = 2_000_000
DEFAULT_TIMEOUT_SECONDS = 15.0
DEFAULT_CONNECT_TIMEOUT_SECONDS = 5.0

#: Shared address space (RFC 6598). CPython stopped reporting this as
#: private in 3.12.4, and it is exactly where this deployment's platform
#: proxy and sibling services live, so it must be excluded explicitly.
_CARRIER_GRADE_NAT = ipaddress.ip_network("100.64.0.0/10")


@dataclass(frozen=True)
class FetchedSource:
    url: str
    status_code: int
    content: bytes
    content_type: str

    @property
    def text(self) -> str:
        return self.content.decode(errors="ignore")


def _is_public_address(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    if ip.version == 4 and ip in _CARRIER_GRADE_NAT:
        return False
    # is_global still reports multicast as global, so exclude it explicitly.
    if ip.is_multicast or ip.is_unspecified:
        return False
    # Otherwise allowlist: anything not globally routable is refused, rather
    # than enumerating the non-routable ranges and missing one.
    return ip.is_global


def _is_public_host(hostname: str) -> bool:
    try:
        addresses = {str(item[4][0]) for item in socket.getaddrinfo(hostname, None)}
    except socket.gaierror as exc:
        raise ValueError("Source hostname could not be resolved") from exc
    # `all`, not `any`: a host resolving to both a public and a private
    # address is a rebinding attempt, not a partially valid target.
    return bool(addresses) and all(_is_public_address(address) for address in addresses)


def validate_source_url(
    url: str, approved_domains: list[str], *, allow_any_public_domain: bool = False
) -> str:
    """Canonicalize and vet a target URL.

    `allow_any_public_domain` relaxes the *domain allowlist* only, and has
    to be asked for explicitly. Some fetches are genuinely open-ended - a
    scholarship's official page is on the provider's domain, not the
    aggregator's, so no useful allowlist exists for it. Saying so at the
    call site is honest; the alternative seen in the wild is passing an
    allowlist derived from the target itself, which reads like a check and
    can never fail.

    The network guard is never relaxed, whatever this is set to. A
    non-public host is refused either way.
    """
    normalized = canonicalize_url(url)
    hostname = urlsplit(normalized).hostname or ""
    if not allow_any_public_domain:
        allowed = {domain.lower().lstrip(".") for domain in approved_domains}
        if not any(hostname == domain or hostname.endswith(f".{domain}") for domain in allowed):
            raise ValueError("Source URL domain is not approved")
    if not _is_public_host(hostname):
        raise ValueError("Private or reserved source host is not allowed")
    return _restore_trailing_slash(url, normalized)


def _restore_trailing_slash(original: str, normalized: str) -> str:
    """Put back a trailing slash the canonicalizer removed.

    `canonicalize_url` strips it because it computes *identity*, where
    `/a` and `/a/` are one page - and it is ported verbatim from the
    product so the two services agree about that. But identity is not a
    fetch target. A site that 301s `/a` to `/a/` - the WordPress default,
    so a large share of the web - sent us into a loop: hop one redirects,
    hop two is canonicalized straight back to hop one, and with a single
    hop allowed the caller receives the redirect stub as though it were
    the page.

    That failed silently and expensively. A 207-byte stub is a valid
    200 response, so no fallback engaged, and the model dutifully
    classified the boilerplate as `not_a_scholarship` - turning a real
    scholarship into a REJECT_RECOMMENDED backed by no evidence at all.

    Only the trailing slash is restored. Everything else canonicalization
    does - lowercasing the host, dropping tracking parameters, rejecting
    non-HTTP schemes - is left in place, and all of it happens before any
    safety check, so this cannot widen what is reachable: the host is
    unchanged and a trailing slash cannot move a URL to another origin.
    """
    # The *path* is what carries the slash. Testing the whole string misses
    # `/award/?utm_source=x`, where the slash is followed by a query - and
    # a tracking parameter on a redirect target is entirely ordinary.
    if not urlsplit(original.strip()).path.endswith("/"):
        return normalized
    scheme, netloc, path, query, fragment = urlsplit(normalized)
    if not path or path == "/" or path.endswith("/"):
        return normalized
    return urlunsplit((scheme, netloc, path + "/", query, fragment))


async def fetch_source(
    url: str,
    approved_domains: list[str],
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    connect_timeout_seconds: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
    allow_any_public_domain: bool = False,
) -> FetchedSource:
    """Fetch an approved, publicly routable URL.

    Raises `ValueError` for any policy rejection - unapproved domain,
    private host, oversized body, unusable redirect - and `httpx.HTTPError`
    for transport failures. Callers depend on that distinction: a policy
    rejection must never be retried or routed around.
    """
    normalized = validate_source_url(
        url, approved_domains, allow_any_public_domain=allow_any_public_domain
    )
    async with httpx.AsyncClient(
        follow_redirects=False,
        timeout=httpx.Timeout(timeout_seconds, connect=connect_timeout_seconds),
        headers={"User-Agent": USER_AGENT},
    ) as client:
        response, normalized = await _get_once(
            client, normalized, approved_domains, allow_any_public_domain
        )
        if 300 <= response.status_code < 400:
            location = response.headers.get("location")
            if not location:
                await response.aclose()
                raise ValueError("Redirect has no destination")
            # Resolved against the current URL before validation, rather
            # than rejected outright as the original did. A relative
            # `Location` is extremely common on official pages, and
            # resolving it loses no safety: the result still goes through
            # validate_source_url, so an off-domain or private target is
            # refused exactly as before.
            # Closed before the second hop. It was opened with stream=True
            # and only _read_bounded closes one, so overwriting it here
            # leaked a pooled connection on every redirect.
            await response.aclose()
            response, normalized = await _get_once(
                client, urljoin(normalized, location), approved_domains, allow_any_public_domain
            )
        content = await _read_bounded(response, max_bytes)
        return FetchedSource(
            normalized,
            response.status_code,
            content,
            response.headers.get("content-type", ""),
        )


async def _get_once(
    client: httpx.AsyncClient,
    url: str,
    approved_domains: list[str],
    allow_any_public_domain: bool = False,
) -> tuple[httpx.Response, str]:
    """One validated hop. Validation happens per hop, never once up front."""
    validated = validate_source_url(
        url, approved_domains, allow_any_public_domain=allow_any_public_domain
    )
    request = client.build_request("GET", validated)
    response = await client.send(request, stream=True)
    return response, validated


async def _read_bounded(response: httpx.Response, max_bytes: int) -> bytes:
    """Read the body, aborting as soon as it exceeds the limit.

    The original checked the length after `response.content` had already
    pulled the whole body into memory, so an endpoint returning a gigabyte
    was fully downloaded before being rejected. These are arbitrary
    third-party pages; the limit has to bound the work, not just the result.
    """
    chunks: list[bytes] = []
    total = 0
    try:
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > max_bytes:
                raise ValueError("Source response exceeds maximum size")
            chunks.append(chunk)
    finally:
        await response.aclose()
    return b"".join(chunks)
