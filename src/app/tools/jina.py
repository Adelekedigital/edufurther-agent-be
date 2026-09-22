"""Jina Reader adapter.

Ported from Scholarship Finder's `infra/jina_client.py`. Jina renders the
target server-side and returns clean markdown, so a page that blocks a
direct request is still retrievable. It changes how the bytes arrive, never
what counts as evidence: a page fetched through Jina is still a real page
actually fetched, and a page that could not be fetched at all is still
missing evidence.
"""

import httpx

READER_BASE_URL = "https://r.jina.ai"

DEFAULT_TIMEOUT_SECONDS = 30.0

#: The provider key used for budget accounting.
PROVIDER = "jina"


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
