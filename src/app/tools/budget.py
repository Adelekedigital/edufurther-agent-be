"""Per-provider monthly call budgets for external research APIs.

Ported from Scholarship Finder's `infra/research_budget.py`, including the
single-statement upsert. Owning the counter here rather than calling the
product for it is what makes agent-side tool budgets independent - and is
the end state the tool migration is heading for, where this service owns
provider calls outright.

One consequence worth stating plainly: while both services hold a key for
the same provider, they count separately and the real quota is the sum.
That is why Scholarship Finder's `JINA_API_KEY` is unset at cutover.
"""

from datetime import UTC, datetime

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.infra.models import ResearchProviderUsage


def current_period_key(now: datetime | None = None) -> str:
    """Calendar-month key, matching the cadence a monthly grant resets on.

    Derived from the timestamp rather than reset by a scheduled job: a
    missed cron cannot hand out an unlimited month.
    """
    return (now or datetime.now(UTC)).strftime("%Y-%m")


async def reserve_call(
    db: AsyncSession, provider: str, limit: int, *, now: datetime | None = None
) -> bool:
    """Reserve one call against `provider`'s current-period budget.

    A single atomic `INSERT ... ON CONFLICT DO UPDATE ... WHERE`: the
    increment and the limit check happen in one statement, so two
    concurrent callers can never both read "one call under the limit" and
    both proceed.

    Call this immediately before the real external HTTP call and skip on
    `False`, rather than making the call and accounting for it afterwards -
    accounting after the fact is how a crashed process spends quota that
    nothing ever records.

    Commits internally, which is worth knowing at the call site: it ends
    any transaction the caller had open.
    """
    if limit <= 0:
        # The WHERE guards only the DO UPDATE branch, so the first insert
        # of a period landed regardless and a zero limit still permitted
        # one call every month. Checked here so "no calls" means no calls.
        return False
    period_key = current_period_key(now)
    statement = (
        insert(ResearchProviderUsage)
        .values(provider=provider, period_key=period_key, calls_used=1)
        .on_conflict_do_update(
            index_elements=["provider", "period_key"],
            set_={"calls_used": ResearchProviderUsage.calls_used + 1},
            where=ResearchProviderUsage.calls_used < limit,
        )
        .returning(ResearchProviderUsage.calls_used)
    )
    result = await db.execute(statement)
    await db.commit()
    return result.first() is not None


async def calls_used(db: AsyncSession, provider: str, *, now: datetime | None = None) -> int:
    """Current period's usage, for reporting rather than enforcement."""
    row = await db.get(ResearchProviderUsage, (provider, current_period_key(now)))
    return row.calls_used if row else 0
