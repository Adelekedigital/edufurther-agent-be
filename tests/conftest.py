import os
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.core.eventloop import install_selector_event_loop_policy

# At import time, before pytest-asyncio creates the session event loop: the
# Postgres checkpointer runs on psycopg, which cannot use Windows' default
# ProactorEventLoop.
install_selector_event_loop_policy()

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@127.0.0.1:55434/edufurther_agent_test",
)

TEST_SERVICE_TOKEN = "test-agent-service-token"

#: A pass with the database tests skipped is not a full pass - the skip is
#: opt-in and loud, never the default.
requires_db = pytest.mark.skipif(
    os.environ.get("SKIP_DB_TESTS") == "1",
    reason="SKIP_DB_TESTS=1; a pass with database tests skipped is not a full pass",
)

#: Every table the suite writes to. Adding a table means adding it here -
#: the list is manual on purpose, because a truncate that silently misses a
#: table produces cross-test bleed that looks like a real failure.
TABLES = (
    "agent_errors",
    "agent_outputs",
    "agent_tool_calls",
    "agent_job_attempts",
    "agent_jobs",
    "agent_research_usage",
)


@pytest.fixture(scope="session", autouse=True)
def _configure_environment() -> None:
    os.environ["DATABASE_URL"] = TEST_DATABASE_URL
    os.environ["INTERNAL_SERVICE_TOKEN"] = TEST_SERVICE_TOKEN
    os.environ.setdefault("ENVIRONMENT", "development")

    from app.core.config import Settings, get_settings
    from app.infra.database import _engine_and_sessions

    # A developer's .env exists to *run* the service, not to test it, but
    # Settings reads it by default - so once one was written for local
    # integration work, SCHOLARSHIP_FINDER_BASE_URL leaked into the suite and
    # the workflow's unreachable-product tests started making real
    # connections to a port nothing was serving. Sixteen of them failed, and
    # only on machines where that file happened to exist.
    #
    # Neutralising the whole file rather than clearing named keys: an
    # enumerated list silently goes stale the moment a setting is added,
    # which is precisely how this got through.
    Settings.model_config["env_file"] = None

    # Both are lru_cached, and the engine caches the URL read at first call.
    get_settings.cache_clear()
    _engine_and_sessions.cache_clear()


@pytest.fixture(autouse=True)
async def _clean_tables(_configure_environment) -> AsyncIterator[None]:
    yield
    if os.environ.get("SKIP_DB_TESTS") == "1":
        return
    from app.infra.database import get_sessionmaker

    try:
        async with get_sessionmaker()() as session:
            await session.execute(text(f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY CASCADE"))
            await session.commit()
    except Exception:
        # A suite run with no database still needs its non-database tests to
        # report their own results rather than erroring in teardown.
        pass


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    """In-process ASGI client. No network, no live server, no port."""
    from app.main import app

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://agent.test"
    ) as http_client:
        yield http_client


@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {"X-Service-Token": TEST_SERVICE_TOKEN}


@pytest.fixture(autouse=True)
def spawned(monkeypatch):
    """Capture background workflow launches instead of running them.

    Submitting a job spawns its run as a detached task. Left alone that
    makes every assertion about job state a race: the row may be `queued`
    or already `completed` depending on scheduling. Capturing the coroutine
    lets a test await it at a chosen moment - deterministic, and still the
    real runner rather than a mock.
    """
    launched: list = []

    def capture(coro):
        launched.append(coro)

    monkeypatch.setattr("app.api.jobs._spawn", capture)
    yield launched
    # Anything a test did not await would otherwise warn as a coroutine
    # that was never awaited, burying real warnings.
    for coro in launched:
        coro.close()
