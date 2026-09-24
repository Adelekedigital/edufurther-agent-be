"""Jina Reader adapter.

Ported from Scholarship Finder's `infra/jina_client.py`. Jina renders the
target server-side and returns clean markdown, so a page that blocks a
direct request is still retrievable. It changes how the bytes arrive, never
what counts as evidence: a page fetched through Jina is still a real page
actually fetched, and a page that could not be fetched at all is still
missing evidence.
"""

from dataclasses import dataclass

import httpx

READER_BASE_URL = "https://r.jina.ai"
SEARCH_BASE_URL = "https://s.jina.ai"

DEFAULT_TIMEOUT_SECONDS = 30.0

#: The provider key used for budget accounting. Reader and search share it
#: deliberately: they are one vendor on one key, and a budget that only
#: bounded half the spend would not be a budget.
PROVIDER = "jina"


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    description: str


async def fetch_via_jina(
    url: str, api_key: str, *, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
) -> str:
    """Fetch `url` through Jina's Reader and return its markdown content.

    Raises `httpx.HTTPError` on any transport or HTTP failure. Callers
    decide whether that means "no fallback available this time" or should
    propagate.

    Note what this function deliberately does not do: it performs no domain
    or SSRF validation of its own. That is not an oversight - it is why
    callers must never route a URL here that `validate_source_url` has
    already rejected. Jina fetches from its own infrastructure, so doing so
    would use it to step around the allowlist entirely.
    """
    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        response = await client.get(
            f"{READER_BASE_URL}/{url}",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        response.raise_for_status()
        return response.text


async def search_via_jina(
    query: str,
    api_key: str,
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    limit: int = 10,
) -> list[SearchResult]:
    """Search the web and return results, without their page content.

    `X-Respond-With: no-content` is not an optimisation detail. The default
    behaviour is to fetch and return the full text of every hit, which for
    ten results is ten page reads billed to one call - and every one of
    them fetched outside the SSRF gate, since Jina retrieves from its own
    infrastructure. Asking for links and snippets only keeps a search a
    search: nothing here is treated as page content, and any URL this
    returns still has to go through `fetch_official_page` to be read.

    Raises `httpx.HTTPError` on transport or HTTP failure, like the reader.
    """
    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        response = await client.get(
            f"{SEARCH_BASE_URL}/{query}",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
                "X-Respond-With": "no-content",
            },
        )
        response.raise_for_status()
        body = response.json()
    if not isinstance(body, dict):
        return []
    results = []
    for item in (body.get("data") or [])[:limit]:
        url = str(item.get("url") or "")
        if url:
            results.append(
                SearchResult(
                    title=str(item.get("title") or ""),
                    url=url,
                    description=str(item.get("description") or ""),
                )
            )
    return results
