"""The runner, against a real database and a real checkpointer.

Deliberately not mocked. A retry classifier, a lease and a resume path that
have only ever been exercised by fakes are a retry classifier, a lease and a
resume path that are untested - and these are exactly the mechanisms that
only matter when something has already gone wrong.
"""

import uuid

import pytest
from sqlalchemy import select

from app.core.identifiers import job_idempotency_key, new_correlation_id
from app.infra.database import get_sessionmaker
from app.infra.jobs import enqueue_job, get_job
from app.infra.models import AgentError, AgentJob, AgentJobAttempt, AgentOutput
from app.runtime.graph_runner import execute_job
from app.runtime.retries import JobState
from tests.conftest import requires_db

pytestmark = requires_db


async def make_job(**payload) -> uuid.UUID:
    reference = f"probe-{uuid.uuid4()}"
    async with get_sessionmaker()() as session:
        job, _ = await enqueue_job(
            session,
            product_id="scholarship_finder",
            use_case_id="probe",
            input_reference=reference,
            correlation_id=new_correlation_id(),
            idempotency_key=job_idempotency_key(
                use_case="probe", workflow_version="v1", input_reference=reference
            ),
            workflow_version="v1",
            payload=payload,
        )
        job_id = job.job_id
        await session.commit()
    return job_id


async def load(job_id: uuid.UUID) -> AgentJob:
    async with get_sessionmaker()() as session:
        job = await get_job(session, job_id)
        assert job is not None
        return job


async def test_a_job_runs_to_completion_and_records_its_result():
    job_id = await make_job()

    state = await execute_job(job_id)

    job = await load(job_id)
    assert state is JobState.COMPLETED
    assert job.state == "completed"
    assert job.attempts == 1
    assert job.last_error is None
    # Released, so the sweeper has nothing to reclaim.
    assert job.lease_expires_at is None

    async with get_sessionmaker()() as session:
        output = await session.scalar(select(AgentOutput).where(AgentOutput.job_id == job_id))
    assert output is not None
    assert output.payload["outcome"] == "PROBE_OK"
    assert output.payload["notes"] == ["probe: begin", "probe: finish"]


async def test_each_run_records_an_attempt():
    job_id = await make_job()

    await execute_job(job_id)

    async with get_sessionmaker()() as session:
        attempts = (
            (await session.execute(select(AgentJobAttempt).where(AgentJobAttempt.job_id == job_id)))
            .scalars()
            .all()
        )
    assert len(attempts) == 1
    assert attempts[0].attempt_number == 1
    assert attempts[0].outcome_state == "completed"
    assert attempts[0].finished_at is not None


async def test_a_transient_failure_is_scheduled_for_retry():
    job_id = await make_job(fail_transient=True)

    state = await execute_job(job_id)

    job = await load(job_id)
    assert state is JobState.RETRY_WAIT
    assert job.state == "retry_wait"
    assert job.next_attempt_at is not None
    assert "TimeoutError" in (job.last_error or "")


async def test_a_permanent_failure_parks_the_job_without_burning_attempts():
    job_id = await make_job(fail_permanent=True)

    state = await execute_job(job_id)

    job = await load(job_id)
    assert state is JobState.FAILED_REVIEW
    assert job.state == "failed_review"
    assert job.attempts == 1
    assert job.next_attempt_at is None


async def test_a_failure_leaves_a_durable_inspectable_record():
    """A failed run that leaves nothing behind is the failure mode that
    costs the most to diagnose."""
    job_id = await make_job(fail_permanent=True)

    await execute_job(job_id)

    async with get_sessionmaker()() as session:
        errors = (
            (await session.execute(select(AgentError).where(AgentError.job_id == job_id)))
            .scalars()
            .all()
        )
        attempts = (
            (await session.execute(select(AgentJobAttempt).where(AgentJobAttempt.job_id == job_id)))
            .scalars()
            .all()
        )

    assert len(errors) == 1
    assert errors[0].error_class == "permanent"
    assert "ValueError" in errors[0].message
    assert attempts[0].outcome_state == "failed_review"
    assert attempts[0].error_class == "permanent"


async def test_a_completed_job_is_not_run_a_second_time():
    """Idempotency at the execution layer, not just at submission: a
    redelivered or double-swept job must not re-run finished work."""
    job_id = await make_job()
    await execute_job(job_id)

    state = await execute_job(job_id)

    job = await load(job_id)
    assert state is JobState.COMPLETED
    assert job.attempts == 1


async def test_running_an_unknown_job_is_not_an_error():
    state = await execute_job(uuid.UUID(int=0))

    assert state is JobState.CANCELLED


async def test_an_unregistered_use_case_fails_permanently():
    """A LookupError would classify as permanent anyway, but only by
    accident of its type name - this asserts the intent."""
    reference = f"unknown-{uuid.uuid4()}"
    async with get_sessionmaker()() as session:
        job, _ = await enqueue_job(
            session,
            product_id="scholarship_finder",
            use_case_id="not_a_real_use_case",
            input_reference=reference,
            correlation_id=new_correlation_id(),
            idempotency_key=f"unknown:{reference}",
            workflow_version="v1",
            payload={},
        )
        job_id = job.job_id
        await session.commit()

    state = await execute_job(job_id)

    job = await load(job_id)
    assert state is JobState.FAILED_REVIEW
    assert job.state == "failed_review"


@pytest.mark.parametrize("attempt_cap", [1])
async def test_attempts_are_capped(attempt_cap, monkeypatch):
    from app.core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "job_max_attempts", attempt_cap)
    job_id = await make_job(fail_transient=True)

    state = await execute_job(job_id)

    assert state is JobState.FAILED_REVIEW
