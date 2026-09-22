"""Execute one job: claim it, run its graph, record what happened.

Everything that can go wrong here has to leave the job in a state someone
can act on. A run that vanishes - no terminal state, no error row, no
released lease - is the failure mode that costs the most to diagnose, so
every path out of this module ends in an explicit transition.
"""

import asyncio
import logging
import uuid
from typing import Any

from langchain_core.runnables import RunnableConfig
from sqlalchemy.dialects.postgresql import insert

from app.core.config import get_settings
from app.infra.database import get_sessionmaker
from app.infra.jobs import (
    apply_transition,
    claim_job,
    finish_attempt,
    get_job,
    record_error,
    renew_lease,
    start_attempt,
)
from app.infra.models import AgentJobAttempt, AgentOutput
from app.runtime.checkpoints import get_checkpointer
from app.runtime.retries import ErrorClass, JobState, classify_error, fail, succeed
from app.runtime.task_state import initial_state
from app.usecases.registry import UnknownUseCase, build_graph

logger = logging.getLogger("app.runtime.graph_runner")

#: Renew the lease at a third of its length. Frequent enough that two
#: consecutive missed renewals still leave time before the sweeper acts,
#: rare enough not to be a write per second.
_HEARTBEAT_DIVISOR = 3
_MIN_HEARTBEAT_SECONDS = 5


async def _heartbeat(job_id: uuid.UUID, lease_seconds: int) -> None:
    """Keep a running job's lease alive.

    A long workflow is not a dead one. Without this the sweeper reclaims a
    run that is still making progress and a second worker starts the same
    discovery - the duplicate-processing failure this design exists to
    avoid.
    """
    interval = max(lease_seconds // _HEARTBEAT_DIVISOR, _MIN_HEARTBEAT_SECONDS)
    sessions = get_sessionmaker()
    while True:
        await asyncio.sleep(interval)
        try:
            async with sessions() as session:
                await renew_lease(session, job_id, lease_seconds=lease_seconds)
                await session.commit()
        except asyncio.CancelledError:
            raise
        except Exception:
            # A failed renewal is not itself a reason to abandon the run.
            # The sweeper reclaiming it later is recoverable; killing a
            # working job over one transient write is not.
            logger.warning("lease_renewal_failed", extra={"job_id": str(job_id)}, exc_info=True)


async def execute_job(job_id: uuid.UUID) -> JobState:
    """Run a single job to a terminal state. Never raises.

    Opens its own session rather than borrowing the request's: this runs
    after the response has been sent, by which time that one is closed.
    """
    settings = get_settings()
    sessions = get_sessionmaker()

    async with sessions() as session:
        job = await claim_job(session, job_id, lease_seconds=settings.job_lease_seconds)
        if job is None:
            # Another worker holds it, or it is already terminal. Both are
            # ordinary outcomes of racing the sweeper, not errors.
            existing = await get_job(session, job_id)
            state = JobState(existing.state) if existing else JobState.CANCELLED
            await session.commit()
            logger.info(
                "job_not_claimable", extra={"job_id": str(job_id), "job_state": state.value}
            )
            return state
        attempt_id = (await start_attempt(session, job)).attempt_id
        snapshot: dict[str, Any] = {
            "product_id": job.product_id,
            "use_case_id": job.use_case_id,
            "input_reference": job.input_reference,
            "correlation_id": job.correlation_id,
            "workflow_version": job.workflow_version,
            "payload": job.payload or {},
            "attempt_number": job.attempts,
        }
        await session.commit()

    heartbeat = asyncio.create_task(_heartbeat(job_id, settings.job_lease_seconds))
    try:
        result = await _run_graph(job_id, snapshot, settings.max_workflow_duration_seconds)
    except Exception as exc:
        return await _record_failure(job_id, attempt_id, snapshot, exc)
    finally:
        heartbeat.cancel()

    await _record_output(job_id, result)
    async with sessions() as session:
        await apply_transition(session, job_id, succeed(snapshot["attempt_number"]))
        attempt = await session.get(AgentJobAttempt, attempt_id)
        if attempt is not None:
            await finish_attempt(session, attempt, outcome_state=JobState.COMPLETED)
        await session.commit()

    logger.info(
        "job_completed",
        extra={"job_id": str(job_id), "job_outcome": result.get("outcome")},
    )
    return JobState.COMPLETED


async def _run_graph(
    job_id: uuid.UUID, snapshot: dict[str, Any], timeout_seconds: int
) -> dict[str, Any]:
    checkpointer = await get_checkpointer()
    graph = build_graph(snapshot["use_case_id"], checkpointer=checkpointer)
    state = initial_state(
        job_id=str(job_id),
        product_id=snapshot["product_id"],
        use_case_id=snapshot["use_case_id"],
        input_reference=snapshot["input_reference"],
        correlation_id=snapshot["correlation_id"],
        workflow_version=snapshot["workflow_version"],
        payload=snapshot["payload"],
    )
    # thread_id is the job id, so a resumed run picks up the checkpoints the
    # interrupted attempt already wrote instead of starting from the top.
    config: RunnableConfig = {"configurable": {"thread_id": str(job_id)}}

    # The distinction that makes checkpointing worth anything: passing a
    # state dict starts the thread over, and only `None` resumes it. A retry
    # that re-sent its input would re-fetch every page and re-pay for every
    # model call the interrupted attempt had already completed - the
    # checkpoints would be written faithfully and never once used.
    resuming = await checkpointer.aget_tuple(config) is not None
    graph_input = None if resuming else state
    if resuming:
        logger.info("job_resuming_from_checkpoint", extra={"job_id": str(job_id)})

    async with asyncio.timeout(timeout_seconds):
        return dict(await graph.ainvoke(graph_input, config=config))


async def _record_output(job_id: uuid.UUID, result: dict[str, Any]) -> None:
    """Persist the run's result outside the checkpoint.

    Checkpoints belong to LangGraph, which is free to prune them. This row
    is ours, and it is what an operator or an evaluation reads afterwards.
    """
    payload = {
        "outcome": result.get("outcome"),
        "notes": result.get("notes") or [],
        "uncertainty_reasons": result.get("uncertainty_reasons") or [],
    }
    async with get_sessionmaker()() as session:
        await session.execute(
            insert(AgentOutput)
            .values(job_id=job_id, node="workflow", candidate_key="", payload=payload)
            # A resumed or retried run overwrites its own earlier result
            # rather than appending a second, contradictory one.
            .on_conflict_do_update(
                index_elements=["job_id", "node", "candidate_key"], set_={"payload": payload}
            )
        )
        await session.commit()


def _classify(exc: Exception) -> ErrorClass:
    if isinstance(exc, UnknownUseCase):
        # Cannot succeed on a later attempt, whatever the generic
        # classifier would make of a LookupError.
        return ErrorClass.PERMANENT
    if isinstance(exc, TimeoutError):
        # Includes asyncio.timeout expiry. A workflow that exceeded its
        # ceiling may well fit next time: a slow upstream, not a bad input.
        return ErrorClass.RETRYABLE
    return classify_error(exc)


async def _record_failure(
    job_id: uuid.UUID, attempt_id: uuid.UUID, snapshot: dict[str, Any], exc: Exception
) -> JobState:
    settings = get_settings()
    error_class = _classify(exc)
    message = f"{type(exc).__name__}: {exc}"
    transition = fail(
        snapshot["attempt_number"],
        message,
        error_class=error_class,
        max_attempts=settings.job_max_attempts,
    )

    async with get_sessionmaker()() as session:
        await apply_transition(session, job_id, transition)
        await record_error(
            session,
            job_id=job_id,
            error_class=error_class.value,
            message=message,
            attempt_number=snapshot["attempt_number"],
        )
        attempt = await session.get(AgentJobAttempt, attempt_id)
        if attempt is not None:
            await finish_attempt(
                session,
                attempt,
                outcome_state=transition.state,
                error_class=error_class.value,
                error=message,
            )
        await session.commit()

    logger.warning(
        "job_failed",
        extra={
            "job_id": str(job_id),
            "error_class": error_class.value,
            "next_state": transition.state.value,
        },
    )
    return transition.state
