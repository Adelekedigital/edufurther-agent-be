"""The search tool under its controls, and what the workflow does with it.

Search is the one tool whose failure is invisible: a fetch that fails leaves
a candidate with no page, which the outcome reports, but a search that
quietly returns nothing looks exactly like a search that found nothing. So
the refusals are asserted rather than assumed, and each records a tool call
so the reason survives in `agent_tool_calls`.
"""

import httpx
import pytest

from app.core.config import get_settings
from app.tools import registry, search
from app.tools.jina import SearchResult, search_via_jina
from app.usecases.scholarship_finder import nodes


class FakeSession:
    """Enough of a session for the budget call and the telemetry writer."""

    def __init__(self) -> None:
        self.added: list[object] = []

    def add(self, item: object) -> None:
        self.added.append(item)

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        return None


@pytest.fixture
def db() -> FakeSession:
    return FakeSession()


@pytest.fixture
def allow_budget(monkeypatch):
    async def _reserve(*args, **kwargs):
        return True

    monkeypatch.setattr(search, "reserve_call", _reserve)


# --- the client ---------------------------------------------------------


async def test_the_search_asks_for_links_not_page_content(monkeypatch):
    """`no-content` is a safety property, not a saving. The default
    returns the full text of every hit - ten page reads billed as one
    call, every one of them retrieved by Jina's infrastructure rather
    than through the SSRF gate."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["headers"] = dict(request.headers)
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"data": []})

    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *a, **k: original(*a, **{**k, "transport": httpx.MockTransport(handler)}),
    )

    await search_via_jina("chevening official site", "key-1")

    assert seen["headers"]["x-respond-with"] == "no-content"
    assert seen["headers"]["authorization"] == "Bearer key-1"


async def test_results_without_a_url_are_dropped(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {"title": "No link", "description": "x"},
                    {"title": "Real", "url": "https://chevening.org/", "description": "y"},
                ]
            },
        )

    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *a, **k: original(*a, **{**k, "transport": httpx.MockTransport(handler)}),
    )

    results = await search_via_jina("q", "key-1")

    assert [r.url for r in results] == ["https://chevening.org/"]


# --- the controls -------------------------------------------------------


async def test_no_key_means_no_search(db, monkeypatch):
    monkeypatch.setattr(get_settings(), "jina_api_key", None)

    assert await search.search_web(db, "q") == []


async def test_the_kill_switch_stops_search_without_stopping_the_reader(db, monkeypatch):
    """The reader has done every official-page read in the pipeline
    without an error. Turning search off must not cost it."""
    monkeypatch.setattr(get_settings(), "jina_api_key", "key-1")
    monkeypatch.setattr(get_settings(), "disabled_tools", {registry.JINA_SEARCH})

    assert await search.search_web(db, "q") == []
    assert registry.is_enabled(registry.JINA), "the reader is still on"


async def test_an_exhausted_budget_stops_search(db, monkeypatch):
    monkeypatch.setattr(get_settings(), "jina_api_key", "key-1")

    async def _exhausted(*args, **kwargs):
        return False

    monkeypatch.setattr(search, "reserve_call", _exhausted)

    assert await search.search_web(db, "q") == []


async def test_a_transport_failure_is_no_results_not_an_exception(db, monkeypatch, allow_budget):
    monkeypatch.setattr(get_settings(), "jina_api_key", "key-1")

    async def _boom(*args, **kwargs):
        raise httpx.ConnectError("no route")

    monkeypatch.setattr(search, "search_via_jina", _boom)

    assert await search.search_web(db, "q") == []


async def test_the_search_tool_is_a_known_tool():
    """A kill switch that matches no known tool reads as "off" to whoever
    set it while the tool carries on running."""
    assert registry.JINA_SEARCH in registry.KNOWN_TOOLS
    assert registry.JINA_SEARCH not in registry.UNIMPLEMENTED_TOOLS


# --- what the workflow does with it ------------------------------------


async def test_search_never_overrides_a_link_that_was_already_found(monkeypatch):
    """Search is a last resort. A candidate that already has an official
    URL from the page must not have it replaced by a search result, which
    is weaker evidence arriving later."""
    called = False

    async def _search(*args, **kwargs):
        nonlocal called
        called = True
        return [SearchResult(title="x", url="https://chevening.org/", description="")]

    monkeypatch.setattr(nodes, "search_web", _search)
    candidate = nodes.Candidate(
        title="Chevening Scholarships",
        identity_key="chevening",
        parent_source_url="https://aggregator.test/list",
        parent_discovery_id="d-1",
        official_url="https://found-on-the-page.test/award",
    )

    await nodes._search_for_official(
        candidate, {"job_id": "job-1", "source_url": "https://aggregator.test/list"}, set()
    )

    assert candidate.official_url == "https://found-on-the-page.test/award"
    assert not called, "no call should have been made at all"


async def test_a_search_that_names_nothing_says_which_kind_of_nothing(monkeypatch):
    """ "Results came back and none named this award" is a different state
    from "we never looked", and only one of them is worth retrying."""

    async def _search(*args, **kwargs):
        return [SearchResult(title="x", url="https://en.wikipedia.org/wiki/X", description="")]

    monkeypatch.setattr(nodes, "search_web", _search)
    candidate = nodes.Candidate(
        title="Chevening Scholarships",
        identity_key="chevening",
        parent_source_url="https://aggregator.test/list",
        parent_discovery_id="d-1",
    )

    await nodes._search_for_official(
        candidate, {"job_id": "job-1", "source_url": "https://aggregator.test/list"}, set()
    )

    assert candidate.official_url is None
    assert any("none on a domain naming this award" in r for r in candidate.uncertainty_reasons)


async def test_a_search_that_finds_the_provider_sets_the_official_url(monkeypatch):
    async def _search(*args, **kwargs):
        return [
            SearchResult(title="ads", url="https://scholarshipsads.com/chevening", description=""),
            SearchResult(title="real", url="https://www.chevening.org/apply/", description=""),
        ]

    monkeypatch.setattr(nodes, "search_web", _search)
    candidate = nodes.Candidate(
        title="Chevening Scholarships",
        identity_key="chevening",
        parent_source_url="https://aggregator.test/list",
        parent_discovery_id="d-1",
    )

    await nodes._search_for_official(
        candidate, {"job_id": "job-1", "source_url": "https://aggregator.test/list"}, set()
    )

    assert candidate.official_url == "https://www.chevening.org/apply/"
    assert any("identified by search" in r for r in candidate.uncertainty_reasons)


async def test_search_cannot_hand_back_the_discoverys_own_page(monkeypatch):
    """The rule that cost sixteen grade-A records before, arriving by a
    new route: a page cannot corroborate itself however it was found."""

    async def _search(*args, **kwargs):
        return [SearchResult(title="x", url="https://chevening.test/award", description="")]

    monkeypatch.setattr(nodes, "search_web", _search)
    candidate = nodes.Candidate(
        title="Chevening Scholarships",
        identity_key="chevening",
        parent_source_url="https://chevening.test/award",
        parent_discovery_id="d-1",
    )

    await nodes._search_for_official(
        candidate, {"job_id": "job-1", "source_url": "https://chevening.test/award"}, set()
    )

    assert candidate.official_url is None
    assert any("cannot corroborate itself" in r for r in candidate.uncertainty_reasons)
