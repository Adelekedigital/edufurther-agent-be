"""Monthly provider budgets.

Ported from Scholarship Finder's `test_research_budget.py`. The concurrency
test is added: the whole reason this is one statement rather than a read
then a write is that two callers must never both see "one call under the
limit" and both proceed, and nothing in the original suite demonstrated
that.
"""

import asyncio
from datetime import datetime

from sqlalchemy import select

from app.infra.database import get_sessionmaker
from app.infra.models import ResearchProviderUsage
from app.tools.budget import calls_used, current_period_key, reserve_call
from tests.conftest import requires_db

pytestmark = requires_db

SEPTEMBER = datetime(2026, 9, 1)


def test_the_period_key_is_the_calendar_month():
    assert current_period_key(datetime(2026, 9, 15)) == "2026-09"
    assert current_period_key(datetime(2026, 1, 1)) == "2026-01"


async def test_calls_are_allowed_up_to_the_limit_then_refused():
    async with get_sessionmaker()() as db:
        for _ in range(3):
            assert await reserve_call(db, "tavily", 3, now=SEPTEMBER) is True
        assert await reserve_call(db, "tavily", 3, now=SEPTEMBER) is False


async def test_a_refused_call_does_not_increment_the_counter():
    """Otherwise a service hammering an exhausted budget would inflate its
    own usage figures and never recover them."""
    async with get_sessionmaker()() as db:
        assert await reserve_call(db, "jina", 1, now=SEPTEMBER) is True
        for _ in range(3):
            assert await reserve_call(db, "jina", 1, now=SEPTEMBER) is False

        row = await db.scalar(
            select(ResearchProviderUsage).where(
                ResearchProviderUsage.provider == "jina",
                ResearchProviderUsage.period_key == "2026-09",
            )
        )
    assert row is not None
    assert row.calls_used == 1


async def test_a_new_calendar_month_is_a_fresh_budget():
    async with get_sessionmaker()() as db:
        assert await reserve_call(db, "tavily", 1, now=datetime(2026, 9, 30)) is True
        assert await reserve_call(db, "tavily", 1, now=datetime(2026, 9, 30)) is False
        # Derived from the timestamp, not reset by a scheduled job - so a
        # missed cron cannot hand out an unlimited month.
        assert await reserve_call(db, "tavily", 1, now=datetime(2026, 10, 1)) is True


async def test_budgets_are_scoped_per_provider():
    async with get_sessionmaker()() as db:
        assert await reserve_call(db, "tavily", 1, now=SEPTEMBER) is True
        assert await reserve_call(db, "tavily", 1, now=SEPTEMBER) is False
        assert await reserve_call(db, "jina", 1, now=SEPTEMBER) is True


async def test_concurrent_callers_cannot_both_take_the_last_call():
    """The point of the single-statement upsert. A read-then-write would let
    two callers both see "one under the limit" and both proceed.

    The assertion is on the *recorded* count, not on how many attempts
    returned True. Those differ if an attempt never reaches the database -
    a pool timeout, say - and conflating the two makes a test that fails
    for reasons that have nothing to do with budgets.
    """
    sessions = get_sessionmaker()

    async def attempt() -> bool:
        async with sessions() as db:
            return await reserve_call(db, "jina", 5, now=SEPTEMBER)

    # Kept inside the connection pool so every attempt genuinely races for
    # the budget rather than for a connection.
    results = await asyncio.gather(*(attempt() for _ in range(10)))

    async with sessions() as db:
        recorded = await calls_used(db, "jina", now=SEPTEMBER)
    assert recorded == 5, "the limit was exceeded under concurrency"
    assert sum(results) == recorded, "a grant was handed out but never counted"


async def test_the_budget_is_never_exceeded_however_many_callers_race():
    """The safety property, stated without assuming every caller gets a
    connection: over-granting is the failure that costs money."""
    sessions = get_sessionmaker()

    async def attempt() -> bool | None:
        try:
            async with sessions() as db:
                return await reserve_call(db, "jina", 3, now=SEPTEMBER)
        except Exception:
            return None

    results = await asyncio.gather(*(attempt() for _ in range(40)))

    async with sessions() as db:
        assert await calls_used(db, "jina", now=SEPTEMBER) <= 3
    assert sum(1 for result in results if result is True) <= 3


async def test_usage_reporting_starts_at_zero_for_an_untouched_provider():
    async with get_sessionmaker()() as db:
        assert await calls_used(db, "never_called", now=SEPTEMBER) == 0


async def test_a_zero_limit_means_no_calls_at_all():
    """It used to permit exactly one per month: the WHERE guards only the
    DO UPDATE branch, so the first insert of a period landed regardless of
    the limit. An operator setting a provider to zero meant zero."""
    async with get_sessionmaker()() as db:
        assert await reserve_call(db, "capped", 0, now=SEPTEMBER) is False
        assert await reserve_call(db, "capped", 0, now=SEPTEMBER) is False
        assert await calls_used(db, "capped", now=SEPTEMBER) == 0
