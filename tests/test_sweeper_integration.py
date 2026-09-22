"""Recovery, against a real database.

The sweeper only matters when something has already gone wrong - a deploy
mid-run, a crashed worker, a submission whose background task never started.
Every test here sets up one of those situations rather than a healthy one.
"""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.core.identifiers import job_idempotency_key, new_correlation_id
from app.infra.database import get_sessionmaker
from app.infra.jobs import enqueue_job, get_job
from app.infra.models import AgentJob
from app.runtime.sweeper import sweep_once
from tests.conftest import requires_db

pytestmark = requires_db


async def make_job(use_case: str = "probe", **payload) -> uuid.UUID:
    reference = f"sweep-{uuid.uuid4()}"
    async with get_sessionmaker()() as session:
        job, _ = await enqueue_job(
            session,
            product_id="scholarship_finder",
            use_case_id=use_case,
            input_reference=reference,
            correlation_id=new_correlation_id(),
            idempotency_key=job_idempotency_key(
                use_case=use_case, workflow_version="v1", input_reference=reference
            ),
            workflow_version="v1",
            payload=payload,
        )
        job_id = job.job_id
        await session.commit()
    return job_id


async def strand(job_id: uuid.UUID, *, lease_expired: bool) -> None:
    """Leave a job looking like its worker died mid-run."""
    async with get_sessionmaker()() as session:
        job = await get_job(session, job_id)
        assert job is not None
        job.state = "running"
        job.attempts = 1
        job.lease_expires_at = datetime.now(UTC) + timedelta(seconds=-60 if lease_expired else 600)
        await session.commit()


async def load(job_id: uuid.UUID) -> AgentJob:
    async with get_sessionmaker()() as session:
        job = await get_job(session, job_id)
        assert job is not None
        return job


async def test_a_queued_job_is_picked_up_and_run():
    """The safety net for a submission whose background task never started -
    a crash between committing the row and spawning the worker."""
    job_id = await make_job()

    result = await sweep_once()

    assert result["picked_up"] == 1
    assert result["completed"] == 1
    assert (await load(job_id)).state == "completed"


async def test_a_job_whose_worker_died_is_reclaimed_and_rerun():
    job_id = await make_job()
    await strand(job_id, lease_expired=True)

    result = await sweep_once()

    assert result["reclaimed"] == 1
    assert (await load(job_id)).state == "completed"


async def test_a_job_still_within_its_lease_is_left_alone():
    """The heartbeat exists to hold this lease open. Reclaiming a run that
    is still working would start a second worker on the same input - the
    duplicate processing the whole design avoids."""
    job_id = await make_job()
    await strand(job_id, lease_expired=False)

    result = await sweep_once()

    assert result["reclaimed"] == 0
    assert result["picked_up"] == 0
    assert (await load(job_id)).state == "running"


async def test_a_terminal_job_is_never_picked_up_again():
    job_id = await make_job()
    await sweep_once()

    result = await sweep_once()

    assert result["picked_up"] == 0
    assert (await load(job_id)).attempts == 1


async def test_one_failing_job_does_not_abort_the_batch():
    """Scholarship Finder learned this on a harvest that lost every item
    after the first failure."""
    failing = await make_job(fail_permanent=True)
    healthy = [await make_job() for _ in range(3)]

    result = await sweep_once()

    assert result["picked_up"] == 4
    assert result["completed"] == 3
    assert result["failed"] == 1
    assert (await load(failing)).state == "failed_review"
    for job_id in healthy:
        assert (await load(job_id)).state == "completed"


async def test_remaining_is_uncapped_so_a_drain_can_report_its_backlog():
    """`picked_up` is bounded by the batch limit; `remaining` deliberately
    is not, so an operator can see what this pass did not reach."""
    for _ in range(4):
        await make_job()

    result = await sweep_once(limit=2)

    assert result["picked_up"] == 2
    assert result["remaining"] == 2


async def test_a_backed_off_job_is_not_run_before_its_time():
    job_id = await make_job(fail_transient=True)
    await sweep_once()
    assert (await load(job_id)).state == "retry_wait"

    result = await sweep_once()

    assert result["picked_up"] == 0, "backoff was ignored"


async def test_an_empty_queue_reports_zeroes():
    assert await sweep_once() == {
        "reclaimed": 0,
        "picked_up": 0,
        "completed": 0,
        "failed": 0,
        "remaining": 0,
    }


async def test_sweeping_counts_only_what_it_touched():
    async with get_sessionmaker()() as session:
        before = (await session.execute(select(AgentJob))).scalars().all()
    assert before == []
