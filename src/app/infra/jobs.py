"""Durable job storage.

The operational lessons here are Scholarship Finder's, learned the hard way
and worth inheriting rather than rediscovering:

* a database row is not enough - work must also be dispatched;
* at-least-once delivery needs the *database* to arbitrate duplicates, not
  a check-then-insert in application code;
* a lease that outlives its worker must be reclaimable;
* a failed item must leave a durable, inspectable state rather than
  disappearing.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.infra.models import AgentError, AgentJob, AgentJobAttempt
from app.runtime.retries import (
    CLAIMABLE_STATES,
    JobState,
    JobTransition,
    claim,
    lease_expiry,
)


async def enqueue_job(
    db: AsyncSession,
    *,
    product_id: str,
    use_case_id: str,
    input_reference: str,
    correlation_id: str,
    idempotency_key: str,
    workflow_version: str,
    payload: dict | None = None,
    expires_at: datetime | None = None,
) -> tuple[AgentJob, bool]:
    """Create a job, or return the existing one for this idempotency key.

    `ON CONFLICT DO NOTHING RETURNING` then re-select, rather than checking
    for an existing row first: two concurrent deliveries of the same
    submission both pass a check-then-insert, and only the unique index
    stops the second one. Returns `(job, created)` so the caller can tell a
    fresh submission from a redelivery without a second query.
    """
    statement = (
        insert(AgentJob)
        .values(
            product_id=product_id,
            use_case_id=use_case_id,
            input_reference=input_reference,
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
            workflow_version=workflow_version,
            state=JobState.QUEUED.value,
            payload=payload or {},
            expires_at=expires_at,
        )
        .on_conflict_do_nothing(index_elements=["idempotency_key"])
        .returning(AgentJob.job_id)
    )
    result = await db.execute(statement)
    created_id = result.scalar_one_or_none()
    await db.flush()

    job = await db.scalar(select(AgentJob).where(AgentJob.idempotency_key == idempotency_key))
    if job is None:  # pragma: no cover - the insert above guarantees a row
        raise RuntimeError(f"job vanished after enqueue: {idempotency_key}")
    return job, created_id is not None


async def get_job(db: AsyncSession, job_id: uuid.UUID) -> AgentJob | None:
    return await db.scalar(select(AgentJob).where(AgentJob.job_id == job_id))


async def claim_job(
    db: AsyncSession, job_id: uuid.UUID, *, lease_seconds: int, now: datetime | None = None
) -> AgentJob | None:
    """Lock a due job and mark it running, or return None.

    `FOR UPDATE` rather than an optimistic check: the sweeper and an inline
    worker can reach for the same row at the same moment, and exactly one of
    them must win.
    """
    job = await db.scalar(select(AgentJob).where(AgentJob.job_id == job_id).with_for_update())
    if job is None:
        return None
    transition = claim(
        JobState(job.state),
        job.attempts,
        lease_seconds=lease_seconds,
        next_attempt_at=job.next_attempt_at,
        now=now,
    )
    if transition is None:
        return None
    _apply(job, transition)
    await db.flush()
    return job


async def due_job_ids(
    db: AsyncSession, *, limit: int, now: datetime | None = None
) -> list[uuid.UUID]:
    """Ids of jobs ready to run, skipping any another worker already holds.

    `SKIP LOCKED` keeps this correct if the service is ever scaled past one
    replica: two sweepers select disjoint sets instead of blocking on each
    other or racing for the same job.
    """
    moment = now or datetime.now(UTC)
    statement = (
        select(AgentJob.job_id)
        .where(
            AgentJob.state.in_([state.value for state in CLAIMABLE_STATES]),
            (AgentJob.next_attempt_at.is_(None)) | (AgentJob.next_attempt_at <= moment),
        )
        .order_by(AgentJob.job_id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    return list((await db.execute(statement)).scalars().all())


async def count_due_jobs(db: AsyncSession, *, now: datetime | None = None) -> int:
    """Uncapped count, so a drain endpoint can report what it did not reach."""
    moment = now or datetime.now(UTC)
    statement = select(func.count(AgentJob.job_id)).where(
        AgentJob.state.in_([state.value for state in CLAIMABLE_STATES]),
        (AgentJob.next_attempt_at.is_(None)) | (AgentJob.next_attempt_at <= moment),
    )
    return int(await db.scalar(statement) or 0)


async def reclaim_expired_leases(
    db: AsyncSession, *, limit: int, now: datetime | None = None
) -> int:
    """Return jobs whose worker died back to the queue.

    A `running` row with a lease in the past means the process holding it
    went away mid-run - a deploy, a restart, an OOM. The work is safe to
    redo: every write it performs is idempotent, and the checkpointer lets
    it resume rather than start over.
    """
    moment = now or datetime.now(UTC)
    stale = (
        select(AgentJob.job_id)
        .where(
            AgentJob.state == JobState.RUNNING.value,
            (AgentJob.lease_expires_at.is_(None)) | (AgentJob.lease_expires_at <= moment),
        )
        .order_by(AgentJob.job_id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    job_ids = list((await db.execute(stale)).scalars().all())
    if not job_ids:
        return 0
    await db.execute(
        update(AgentJob)
        .where(AgentJob.job_id.in_(job_ids))
        .values(
            state=JobState.RETRY_WAIT.value,
            lease_expires_at=None,
            next_attempt_at=moment,
            last_error="lease expired; worker did not finish",
        )
    )
    await db.flush()
    return len(job_ids)


async def renew_lease(
    db: AsyncSession, job_id: uuid.UUID, *, lease_seconds: int, now: datetime | None = None
) -> None:
    """Extend a running job's lease.

    A long workflow is not a dead one. Without renewal the sweeper would
    reclaim a run that is still making progress, and two workers would
    process the same discovery.
    """
    await db.execute(
        update(AgentJob)
        .where(AgentJob.job_id == job_id, AgentJob.state == JobState.RUNNING.value)
        .values(lease_expires_at=lease_expiry(lease_seconds, now=now))
    )
    await db.flush()


async def apply_transition(
    db: AsyncSession, job_id: uuid.UUID, transition: JobTransition
) -> AgentJob | None:
    job = await db.scalar(select(AgentJob).where(AgentJob.job_id == job_id).with_for_update())
    if job is None:
        return None
    _apply(job, transition)
    await db.flush()
    return job


def _apply(job: AgentJob, transition: JobTransition) -> None:
    job.state = transition.state.value
    job.attempts = transition.attempts
    job.next_attempt_at = transition.next_attempt_at
    job.lease_expires_at = transition.lease_expires_at
    if transition.last_error is not None:
        job.last_error = transition.last_error


async def start_attempt(db: AsyncSession, job: AgentJob) -> AgentJobAttempt:
    attempt = AgentJobAttempt(job_id=job.job_id, attempt_number=job.attempts)
    db.add(attempt)
    await db.flush()
    return attempt


async def finish_attempt(
    db: AsyncSession,
    attempt: AgentJobAttempt,
    *,
    outcome_state: JobState,
    error_class: str | None = None,
    error: str | None = None,
    now: datetime | None = None,
) -> None:
    attempt.finished_at = now or datetime.now(UTC)
    attempt.outcome_state = outcome_state.value
    attempt.error_class = error_class
    attempt.error = error
    await db.flush()


async def record_error(
    db: AsyncSession,
    *,
    job_id: uuid.UUID,
    error_class: str,
    message: str,
    attempt_number: int | None = None,
    node: str | None = None,
    context: dict | None = None,
) -> None:
    db.add(
        AgentError(
            job_id=job_id,
            attempt_number=attempt_number,
            node=node,
            error_class=error_class,
            message=message[:2000],
            context=context,
        )
    )
    await db.flush()
