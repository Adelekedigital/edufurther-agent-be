"""Web search, under the same controls as the fetchers.

Its own kill switch rather than Jina's, because they fail independently and
they are worth stopping independently: the reader has run 90 calls without
an error and does every official-page read in the pipeline, so a search that
starts returning noise must be stoppable without taking the reader with it.

Budget is shared with the reader, since they are one vendor on one key.
Reserved before the call for the same reason the reader reserves before its
own: a crash between spending and accounting spends quota that nothing
records.
"""

import logging

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.tools import registry
from app.tools.base import ToolDisabled, recorded
from app.tools.budget import reserve_call
from app.tools.jina import PROVIDER as JINA_PROVIDER
from app.tools.jina import SearchResult, search_via_jina

logger = logging.getLogger("app.tools.search")


async def search_web(
    db: AsyncSession,
    query: str,
    *,
    job_id: str | None = None,
) -> list[SearchResult]:
    """Best effort. Unconfigured, disabled, over budget or a transport
    failure all mean the same thing to the caller: no results this time,
    which is never the same as "no official page exists".
    """
    settings = get_settings()
    async with recorded(db, "search_web", target=query[:200], job_id=job_id) as record:
        if not settings.jina_api_key:
            record.status = "refused"
            return []
        try:
            spec = registry.require_enabled(registry.JINA_SEARCH)
        except ToolDisabled:
            logger.info("search_disabled", extra={"query": query})
            record.status = "refused"
            return []
        if not await reserve_call(db, JINA_PROVIDER, settings.jina_monthly_call_limit):
            logger.info("search_budget_exhausted", extra={"query": query})
            record.status = "refused"
            return []
        try:
            results = await search_via_jina(
                query,
                settings.jina_api_key,
                timeout_seconds=spec.timeout_seconds,
                limit=settings.search_result_limit,
            )
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("search_failed", extra={"query": query, "error": str(exc)})
            record.status = "error"
            record.error = str(exc)
            return []
        record.response_meta = {"result_count": len(results)}
        return results
