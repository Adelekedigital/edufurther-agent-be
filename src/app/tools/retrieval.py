"""Composing the fetchers: which one goes first, and why.

Scholarship Finder runs the two retrieval paths in opposite orders, and the
reason is not incidental:

* ordinary retrieval wants *some* content, so it fetches directly and falls
  back to Jina only when that fails;
* official-page verification wants *clean* content to extract facts from,
  so it prefers Jina's rendered markdown and falls back to raw HTML.

The rule both share, and the one that must never be relaxed: a domain or
SSRF refusal propagates immediately and never triggers a fallback. Jina
fetches from its own infrastructure, so falling back to it after the guard
said no would use it to step around the guard.
"""

import logging
from dataclasses import dataclass
from typing import Literal

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.tools import registry
from app.tools.base import ToolDisabled, recorded
from app.tools.budget import reserve_call
from app.tools.direct_fetch import fetch_source, validate_source_url
from app.tools.jina import PROVIDER as JINA_PROVIDER
from app.tools.jina import fetch_via_jina

logger = logging.getLogger("app.tools.retrieval")

FetchMethod = Literal["direct", "jina"]


@dataclass(frozen=True)
class RetrievedPage:
    url: str
    text: str
    fetch_method: FetchMethod
    #: Always set: the direct fetcher reports the real status, and a Jina
    #: fetch that returned at all is a 200 by construction - it raises
    #: otherwise.
    status_code: int
    content_type: str
    byte_length: int

    @property
    def response_meta(self) -> dict[str, object]:
        """Shape and size only - never the page text itself."""
        return {
            "fetch_method": self.fetch_method,
            "status_code": self.status_code,
            "content_type": self.content_type,
            "byte_length": self.byte_length,
        }


async def _try_jina(db: AsyncSession, url: str, *, reason: str) -> RetrievedPage | None:
    """Best-effort: unconfigured, disabled, over budget or a further
    transport failure all just mean no Jina this time."""
    settings = get_settings()
    if not settings.jina_api_key:
        return None
    try:
        spec = registry.require_enabled(registry.JINA)
    except ToolDisabled:
        logger.info("jina_disabled", extra={"url": url, "reason": reason})
        return None
    # Reserved before the call, never accounted for after it: a crash
    # between the two would otherwise spend quota nothing records.
    if not await reserve_call(db, JINA_PROVIDER, settings.jina_monthly_call_limit):
        logger.info("jina_budget_exhausted", extra={"url": url, "reason": reason})
        return None
    try:
        text = await fetch_via_jina(
            url, settings.jina_api_key, timeout_seconds=spec.timeout_seconds
        )
    except httpx.HTTPError as exc:
        logger.warning("jina_fetch_failed", extra={"url": url, "error": str(exc)})
        return None
    logger.info("jina_used", extra={"url": url, "reason": reason})
    return RetrievedPage(
        url=url,
        text=text,
        fetch_method="jina",
        status_code=200,
        content_type="text/markdown",
        byte_length=len(text.encode()),
    )


async def _direct(
    url: str, approved_domains: list[str], *, allow_any_public_domain: bool = False
) -> RetrievedPage:
    settings = get_settings()
    spec = registry.require_enabled(registry.DIRECT_FETCH)
    fetched = await fetch_source(
        url,
        approved_domains,
        max_bytes=settings.fetch_max_bytes,
        timeout_seconds=spec.timeout_seconds,
        connect_timeout_seconds=settings.fetch_connect_timeout_seconds,
        allow_any_public_domain=allow_any_public_domain,
    )
    return RetrievedPage(
        url=fetched.url,
        text=fetched.text,
        fetch_method="direct",
        status_code=fetched.status_code,
        content_type=fetched.content_type,
        byte_length=len(fetched.content),
    )


async def fetch_page(
    db: AsyncSession, url: str, approved_domains: list[str], *, job_id: str | None = None
) -> RetrievedPage:
    """Direct first, Jina only on failure.

    For ordinary retrieval, where the goal is to obtain the page at all.
    A `ValueError` from the guard propagates untouched - see the module
    docstring for why that must not fall back.
    """
    async with recorded(db, "fetch_page", target=url, job_id=job_id) as record:
        # Validated here, before any tool is chosen. It used to happen
        # inside `_direct`, *after* that function's kill-switch check - so
        # disabling the direct fetcher raised ToolDisabled before the guard
        # ever ran, and the fallback below handed the raw, unvalidated URL
        # to Jina. Any domain at all could then be fetched through Jina's
        # network by flipping an operational switch. Validating up front
        # makes that ordering impossible to get wrong again.
        validated = validate_source_url(url, approved_domains)
        try:
            page = await _direct(validated, approved_domains)
        except ValueError:
            # Policy refusal. Never a reason to try another route.
            raise
        except (httpx.HTTPError, ToolDisabled) as exc:
            fallback = await _try_jina(db, validated, reason=str(exc))
            if fallback is None:
                raise
            record.response_meta = fallback.response_meta
            return fallback
        if page.status_code >= 400:
            fallback = await _try_jina(db, validated, reason=f"status {page.status_code}")
            if fallback is not None:
                record.response_meta = fallback.response_meta
                return fallback
        record.response_meta = page.response_meta
        return page


async def fetch_official_page(
    db: AsyncSession,
    url: str,
    approved_domains: list[str],
    *,
    job_id: str | None = None,
    allow_any_public_domain: bool = False,
) -> RetrievedPage:
    """Jina first for clean text, direct as the fallback.

    For verifying an official page, where facts are extracted from the text
    and raw HTML is markedly worse input than rendered markdown.

    The guard still runs first even though Jina does not need it: validating
    up front is what stops an unapproved or private URL being handed to
    Jina at all.

    `allow_any_public_domain` exists because an award's official page lives
    on the provider's domain, not the source's, so no useful allowlist
    covers it. Callers that need it have to say so - the network guard
    still applies, and a private or reserved host is refused either way.
    """
    async with recorded(db, "fetch_official_page", target=url, job_id=job_id) as record:
        validated = validate_source_url(
            url, approved_domains, allow_any_public_domain=allow_any_public_domain
        )

        page = await _try_jina(db, validated, reason="official page verification")
        if page is None:
            page = await _direct(
                validated, approved_domains, allow_any_public_domain=allow_any_public_domain
            )
            if page.status_code >= 400:
                raise httpx.HTTPError(f"page fetch returned status {page.status_code}")
        record.response_meta = page.response_meta
        return page
