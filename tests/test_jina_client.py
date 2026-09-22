"""The Jina Reader request itself.

Covered separately because the retrieval tests mock this function out. The
URL construction is the easy thing to get wrong - Reader takes the target
URL as a path suffix, so a stray encoding or a missing scheme silently
fetches the wrong thing rather than failing.
"""

import httpx
import pytest

from app.tools import jina
from app.tools.jina import READER_BASE_URL, fetch_via_jina


def transport(monkeypatch, handler):
    original = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original(*args, **kwargs)

    monkeypatch.setattr(jina.httpx, "AsyncClient", patched)


async def test_the_target_url_is_appended_to_the_reader_base(monkeypatch):
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(200, text="# Award")

    transport(monkeypatch, handler)

    await fetch_via_jina("https://example.test/award", "key")

    assert seen["url"] == f"{READER_BASE_URL}/https://example.test/award"


async def test_the_api_key_is_sent_as_a_bearer_token(monkeypatch):
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, text="ok")

    transport(monkeypatch, handler)

    await fetch_via_jina("https://example.test/a", "secret-key")

    assert seen["auth"] == "Bearer secret-key"


async def test_the_markdown_body_is_returned(monkeypatch):
    transport(monkeypatch, lambda r: httpx.Response(200, text="# Award\n\nDeadline: 1 March"))

    assert "Deadline" in await fetch_via_jina("https://example.test/a", "key")


@pytest.mark.parametrize("status", [401, 403, 404, 429, 500, 503])
async def test_an_error_status_raises_rather_than_returning_a_body(monkeypatch, status):
    """A 429 body is not scholarship text. Returning it would feed an error
    page into extraction as though it were evidence."""
    transport(monkeypatch, lambda r: httpx.Response(status, text="rate limited"))

    with pytest.raises(httpx.HTTPError):
        await fetch_via_jina("https://example.test/a", "key")


async def test_a_transport_failure_propagates(monkeypatch):
    def boom(request):
        raise httpx.ConnectError("unreachable")

    transport(monkeypatch, boom)

    with pytest.raises(httpx.HTTPError):
        await fetch_via_jina("https://example.test/a", "key")
