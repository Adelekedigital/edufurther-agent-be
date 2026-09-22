"""Background recovery loop.

Scholarship Finder's operational lesson, restated: scheduled delivery is not
enough on its own, and a manual drain is needed even when it exists. This
service has no external queue at all - a submission is executed in-process
and a crash between "row written" and "run finished" strands the job. The
sweeper is what makes that recoverable rather than permanent.

Two responsibilities, in order:

1. reclaim jobs whose worker died, by expired lease;
2. run whatever is due, including what step 1 just released.
"""

import asyncio
import contextlib
import logging

from app.core.config import get_settings
from app.infra.database import get_sessionmaker
from app.infra.jobs import count_due_jobs, due_job_ids, reclaim_expired_leases
from app.runtime.graph_runner import execute_job
from app.runtime.retries import JobState

logger = logging.getLogger("app.runtime.sweeper")


async def sweep_once(*, limit: int | None = None) -> dict[str, int]:
    """One pass. Returns what it did, for a drain endpoint to report."""
    settings = get_settings()
    batch = limit if limit is not None else settings.job_sweep_batch_limit
    sessions = get_sessionmaker()

    async with sessions() as session:
        reclaimed = await reclaim_expired_leases(session, limit=batch)
        await session.commit()

    async with sessions() as session:
        job_ids = await due_job_ids(session, limit=batch)
        # Released before running anything: holding SELECT ... FOR UPDATE
        # across the whole batch would keep a transaction open for as long
        # as the slowest workflow takes.
        await session.commit()

    completed = failed = 0
    for job_id in job_ids:
        try:
            state = await execute_job(job_id)
        except Exception:
            # execute_job is contracted not to raise; if it ever does, one
            # bad job must not abort the rest of the batch. Scholarship
            # Finder learned this on a harvest that lost every item after
            # the first failure.
            logger.exception("sweep_job_crashed", extra={"job_id": str(job_id)})
            failed += 1
            continue
        if state is JobState.COMPLETED:
            completed += 1
        elif state in (JobState.FAILED_REVIEW, JobState.CANCELLED):
            failed += 1

    async with sessions() as session:
        remaining = await count_due_jobs(session)
        await session.commit()

    result = {
        "reclaimed": reclaimed,
        "picked_up": len(job_ids),
        "completed": completed,
        "failed": failed,
        "remaining": remaining,
    }
    if any(value for key, value in result.items() if key != "remaining"):
        logger.info("sweep_completed", extra={"sweep": result})
    return result


async def run_sweeper(stop: asyncio.Event) -> None:
    """Loop until asked to stop.

    Errors are logged and the loop continues: a sweeper that dies on one bad
    pass takes every future recovery with it, which is strictly worse than
    the problem it was trying to report.
    """
    interval = get_settings().job_sweep_interval_seconds
    logger.info("sweeper_started", extra={"interval_seconds": interval})
    while not stop.is_set():
        with contextlib.suppress(TimeoutError):
            # Wakes early when shutdown is signalled rather than making the
            # process wait out a full interval to exit.
            await asyncio.wait_for(stop.wait(), timeout=interval)
        if stop.is_set():
            break
        try:
            await sweep_once()
        except Exception:
            logger.exception("sweep_failed")
    logger.info("sweeper_stopped")
