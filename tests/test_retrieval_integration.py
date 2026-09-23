"""Retrieval precedence, kill switches and the no-fallback rule.

The rule this file exists to protect: a domain or SSRF refusal must never
trigger a fallback. Jina fetches from its own infrastructure, so falling
back to it after the guard said no would use it to walk straight around the
guard - turning the fetch boundary into a suggestion.
"""

import httpx
import pytest

from app.core.config import get_settings
from app.infra.database import get_sessionmaker
from app.tools import direct_fetch, jina, registry, retrieval
from app.tools.base import ToolDisabled
from app.tools.budget import calls_used
from app.tools.retrieval import fetch_official_page, fetch_page
from tests.conftest import requires_db

pytestmark = requires_db

APPROVED = ["example.test"]


@pytest.fixture(autouse=True)
def tool_settings(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "jina_api_key", "jina-test-key")
    monkeypatch.setattr(settings, "jina_monthly_call_limit", 100)
    monkeypatch.setattr(settings, "disabled_tools", set())
    monkeypatch.setattr(direct_fetch, "_is_public_host", lambda hostname: True)
    return settings


def mock_direct(monkeypatch, handler):
    original = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original(*args, **kwargs)

    monkeypatch.setattr(direct_fetch.httpx, "AsyncClient", patched)


def mock_jina(monkeypatch, *, text: str | None = None, error: Exception | None = None):
    calls = []

    async def fake(url, api_key, *, timeout_seconds=30.0):
        calls.append(url)
        if error is not None:
            raise error
        return text or "jina markdown"

    monkeypatch.setattr(retrieval, "fetch_via_jina", fake)
    return calls


async def session():
    return get_sessionmaker()()


# --- fetch_page: direct first ----------------------------------------


async def test_fetch_page_uses_the_direct_fetcher_when_it_works(monkeypatch):
    mock_direct(monkeypatch, lambda r: httpx.Response(200, text="direct html " * 40))
    jina_calls = mock_jina(monkeypatch)

    async with get_sessionmaker()() as db:
        page = await fetch_page(db, "https://example.test/award", APPROVED)

    assert page.fetch_method == "direct"
    assert page.text.startswith("direct html")
    assert jina_calls == [], "Jina was called when the direct fetch succeeded"


async def test_fetch_page_falls_back_to_jina_on_a_transport_failure(monkeypatch):
    def boom(request):
        raise httpx.ConnectError("refused")

    mock_direct(monkeypatch, boom)
    jina_calls = mock_jina(monkeypatch, text="rendered")

    async with get_sessionmaker()() as db:
        page = await fetch_page(db, "https://example.test/award", APPROVED)

    assert page.fetch_method == "jina"
    assert page.text == "rendered"
    assert len(jina_calls) == 1


async def test_fetch_page_falls_back_to_jina_on_a_blocking_status(monkeypatch):
    """A 403 is the usual shape of a page that blocks direct requests."""
    mock_direct(monkeypatch, lambda r: httpx.Response(403, text="forbidden"))
    mock_jina(monkeypatch, text="rendered")

    async with get_sessionmaker()() as db:
        page = await fetch_page(db, "https://example.test/award", APPROVED)

    assert page.fetch_method == "jina"


async def test_a_refused_domain_never_reaches_jina(monkeypatch):
    """The rule. Jina would fetch it happily from its own network."""
    mock_direct(monkeypatch, lambda r: httpx.Response(200, text="should not happen"))
    jina_calls = mock_jina(monkeypatch)

    async with get_sessionmaker()() as db:
        with pytest.raises(ValueError, match="not approved"):
            await fetch_page(db, "https://attacker.test/award", APPROVED)

    assert jina_calls == [], "a policy refusal was routed around via Jina"


async def test_a_private_host_never_reaches_jina(monkeypatch):
    monkeypatch.setattr(direct_fetch, "_is_public_host", lambda hostname: False)
    jina_calls = mock_jina(monkeypatch)

    async with get_sessionmaker()() as db:
        with pytest.raises(ValueError, match="Private or reserved"):
            await fetch_page(db, "https://example.test/award", APPROVED)

    assert jina_calls == []


async def test_an_oversize_response_never_reaches_jina(monkeypatch):
    monkeypatch.setattr(get_settings(), "fetch_max_bytes", 10)
    mock_direct(monkeypatch, lambda r: httpx.Response(200, content=b"x" * 5_000))
    jina_calls = mock_jina(monkeypatch)

    async with get_sessionmaker()() as db:
        with pytest.raises(ValueError, match="exceeds maximum size"):
            await fetch_page(db, "https://example.test/award", APPROVED)

    assert jina_calls == []


async def test_a_failing_direct_fetch_with_jina_unavailable_raises(monkeypatch):
    def boom(request):
        raise httpx.ConnectError("refused")

    mock_direct(monkeypatch, boom)
    mock_jina(monkeypatch, error=httpx.ConnectError("jina down too"))

    async with get_sessionmaker()() as db:
        with pytest.raises(httpx.HTTPError):
            await fetch_page(db, "https://example.test/award", APPROVED)


# --- fetch_official_page: Jina first ---------------------------------


async def test_official_page_prefers_jina_for_clean_text(monkeypatch):
    """Verification extracts facts from the text, and raw HTML is markedly
    worse input than rendered markdown."""
    mock_direct(monkeypatch, lambda r: httpx.Response(200, text="<html>raw</html>"))
    # Long enough to read as a page: a near-empty render now counts as a
    # failed fetch, so a token body would fall through to direct.
    mock_jina(monkeypatch, text="# Award\n\nDeadline: 1 March\n\n" + "eligibility detail " * 20)

    async with get_sessionmaker()() as db:
        page = await fetch_official_page(db, "https://example.test/award", APPROVED)

    assert page.fetch_method == "jina"
    assert "Deadline" in page.text


async def test_official_page_falls_back_to_the_direct_fetcher(monkeypatch):
    mock_direct(monkeypatch, lambda r: httpx.Response(200, text="<html>raw</html>"))
    mock_jina(monkeypatch, error=httpx.ConnectError("jina down"))

    async with get_sessionmaker()() as db:
        page = await fetch_official_page(db, "https://example.test/award", APPROVED)

    assert page.fetch_method == "direct"


async def test_official_page_validates_before_reaching_jina(monkeypatch):
    """Jina needs no allowlist of its own, which is exactly why the guard
    has to run first rather than after."""
    jina_calls = mock_jina(monkeypatch)

    async with get_sessionmaker()() as db:
        with pytest.raises(ValueError, match="not approved"):
            await fetch_official_page(db, "https://attacker.test/award", APPROVED)

    assert jina_calls == []


async def test_official_page_raises_when_the_direct_fallback_errors(monkeypatch):
    mock_direct(monkeypatch, lambda r: httpx.Response(404, text="gone"))
    mock_jina(monkeypatch, error=httpx.ConnectError("jina down"))

    async with get_sessionmaker()() as db:
        with pytest.raises(httpx.HTTPError, match="404"):
            await fetch_official_page(db, "https://example.test/award", APPROVED)


# --- budget and kill switches ----------------------------------------


async def test_jina_use_is_charged_to_its_budget(monkeypatch):
    mock_direct(monkeypatch, lambda r: httpx.Response(403))
    mock_jina(monkeypatch, text="rendered")

    async with get_sessionmaker()() as db:
        before = await calls_used(db, "jina")
    async with get_sessionmaker()() as db:
        await fetch_page(db, "https://example.test/award", APPROVED)
    async with get_sessionmaker()() as db:
        assert await calls_used(db, "jina") == before + 1


async def test_an_exhausted_jina_budget_leaves_the_direct_result(monkeypatch):
    monkeypatch.setattr(get_settings(), "jina_monthly_call_limit", 0)
    mock_direct(monkeypatch, lambda r: httpx.Response(200, text="direct"))
    jina_calls = mock_jina(monkeypatch)

    async with get_sessionmaker()() as db:
        page = await fetch_page(db, "https://example.test/award", APPROVED)

    assert page.fetch_method == "direct"
    assert jina_calls == []


async def test_disabling_jina_skips_it_without_failing_the_fetch(monkeypatch):
    monkeypatch.setattr(get_settings(), "disabled_tools", {registry.JINA})
    mock_direct(monkeypatch, lambda r: httpx.Response(403))
    jina_calls = mock_jina(monkeypatch)

    async with get_sessionmaker()() as db:
        page = await fetch_page(db, "https://example.test/award", APPROVED)

    assert page.fetch_method == "direct"
    assert page.status_code == 403
    assert jina_calls == []


async def test_disabling_the_direct_fetcher_falls_through_to_jina(monkeypatch):
    monkeypatch.setattr(get_settings(), "disabled_tools", {registry.DIRECT_FETCH})
    mock_jina(monkeypatch, text="rendered")

    async with get_sessionmaker()() as db:
        page = await fetch_page(db, "https://example.test/award", APPROVED)

    assert page.fetch_method == "jina"


async def test_disabling_both_fetchers_raises_rather_than_returning_nothing(monkeypatch):
    monkeypatch.setattr(get_settings(), "disabled_tools", {registry.DIRECT_FETCH, registry.JINA})

    async with get_sessionmaker()() as db:
        with pytest.raises(ToolDisabled):
            await fetch_page(db, "https://example.test/award", APPROVED)


async def test_an_unconfigured_jina_key_is_simply_no_fallback(monkeypatch):
    monkeypatch.setattr(get_settings(), "jina_api_key", None)
    mock_direct(monkeypatch, lambda r: httpx.Response(403, text="forbidden"))

    async with get_sessionmaker()() as db:
        page = await fetch_page(db, "https://example.test/award", APPROVED)

    assert page.fetch_method == "direct"


def test_the_jina_provider_key_matches_the_budget_counter():
    """Drift here would silently give Jina a second, uncounted allowance."""
    assert jina.PROVIDER == "jina"


async def test_a_disabled_direct_fetcher_does_not_bypass_the_domain_allowlist(monkeypatch):
    """The bypass this ordering was hiding: `require_enabled` ran inside
    `_direct`, *before* `fetch_source` - which is where validation lives.
    With direct_fetch disabled the guard never ran at all, and the raw
    caller URL went to Jina, whose own fetch does no validation. Any domain
    could be reached through Jina's network by flipping an operational
    switch."""
    monkeypatch.setattr(get_settings(), "disabled_tools", {registry.DIRECT_FETCH})
    jina_calls = mock_jina(monkeypatch, text="attacker content")

    async with get_sessionmaker()() as db:
        with pytest.raises(ValueError, match="not approved"):
            await fetch_page(db, "https://attacker.test/secret", APPROVED)

    assert jina_calls == [], "a disabled direct fetcher routed an unapproved URL to Jina"


async def test_a_disabled_direct_fetcher_still_refuses_a_private_host(monkeypatch):
    monkeypatch.setattr(get_settings(), "disabled_tools", {registry.DIRECT_FETCH})
    monkeypatch.setattr(direct_fetch, "_is_public_host", lambda hostname: False)
    jina_calls = mock_jina(monkeypatch)

    async with get_sessionmaker()() as db:
        with pytest.raises(ValueError, match="Private or reserved"):
            await fetch_page(db, "https://example.test/award", APPROVED)

    assert jina_calls == []


async def test_the_open_ended_mode_still_refuses_a_private_host(monkeypatch):
    """`allow_any_public_domain` relaxes the domain allowlist and nothing
    else. An official page can be on any provider's domain; it can never
    be on an internal one."""
    monkeypatch.setattr(direct_fetch, "_is_public_host", lambda hostname: False)
    mock_jina(monkeypatch, text="rendered")

    async with get_sessionmaker()() as db:
        with pytest.raises(ValueError, match="Private or reserved"):
            await fetch_official_page(
                db, "https://anything.test/award", APPROVED, allow_any_public_domain=True
            )


async def test_the_open_ended_mode_accepts_an_off_domain_official_page(monkeypatch):
    """The reason the mode exists: an award's official page lives on the
    provider's domain, not the aggregator's."""
    mock_jina(monkeypatch, text="# Award\n\n" + "award detail " * 20)

    async with get_sessionmaker()() as db:
        page = await fetch_official_page(
            db, "https://provider.test/award", APPROVED, allow_any_public_domain=True
        )

    assert page.fetch_method == "jina"


# --- a successful response carrying no page ----------------------------


async def test_a_thin_direct_body_falls_back_to_jina(monkeypatch):
    """The failure that rejected a real scholarship on no evidence.

    A 301 stub is a valid 200 once followed wrongly - a couple of hundred
    bytes of boilerplate, no error status, nothing for a status check to
    catch. It reached the model, which reasonably called it
    `not_a_scholarship`, and the workflow turned that into a
    REJECT_RECOMMENDED against a real award.
    """
    mock_direct(monkeypatch, lambda r: httpx.Response(200, text="<html><body>Moved</body></html>"))
    jina_calls = mock_jina(monkeypatch, text="# Real award\n\n" + "actual content " * 40)

    async with get_sessionmaker()() as db:
        page = await fetch_page(db, "https://example.test/award", APPROVED)

    assert page.fetch_method == "jina"
    assert "actual content" in page.text
    assert jina_calls, "a body too small to be a page must trigger the fallback"


async def test_a_thin_body_keeps_the_direct_result_when_jina_does_no_better(monkeypatch):
    """Swapping one empty page for another would spend quota and obscure
    which fetcher was used, so the fallback has to actually improve on it."""
    mock_direct(monkeypatch, lambda r: httpx.Response(200, text="<html><body>Moved</body></html>"))
    mock_jina(monkeypatch, text="also nothing")

    async with get_sessionmaker()() as db:
        page = await fetch_page(db, "https://example.test/award", APPROVED)

    assert page.fetch_method == "direct"


async def test_a_full_page_never_triggers_the_fallback(monkeypatch):
    """The guard must not fire on ordinary pages - that would put every
    fetch through Jina and exhaust a 500-a-month budget in a day."""
    body = "<html>" + ("x" * 5_000) + "</html>"
    mock_direct(monkeypatch, lambda r: httpx.Response(200, text=body))
    jina_calls = mock_jina(monkeypatch)

    async with get_sessionmaker()() as db:
        page = await fetch_page(db, "https://example.test/award", APPROVED)

    assert page.fetch_method == "direct"
    assert jina_calls == []


async def test_a_thin_jina_render_falls_back_to_direct_for_official_pages(monkeypatch):
    """The mirror case: an empty render is worse input for fact extraction
    than raw HTML, so the official-page path has to reverse too."""
    body = "<html>" + ("award detail " * 40) + "</html>"
    mock_direct(monkeypatch, lambda r: httpx.Response(200, text=body))
    mock_jina(monkeypatch, text="# ")

    async with get_sessionmaker()() as db:
        page = await fetch_official_page(db, "https://example.test/award", APPROVED)

    assert page.fetch_method == "direct"
    assert "award detail" in page.text
