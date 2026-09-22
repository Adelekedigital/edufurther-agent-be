"""Resume, against a real Postgres checkpointer.

This is the mechanism spec §15 calls "resume after worker interruption", and
the one that decides whether a retry costs a few seconds or re-fetches every
page and re-pays for every model call the interrupted attempt completed.

Worth being precise about what is asserted here, because it is easy to write
a test that passes while resume does nothing: a graph that simply re-runs
from the top also ends up `completed`. The assertion that matters is the
*call count* of the node that already succeeded.
"""

import uuid

from langgraph.graph import END, START, StateGraph
from sqlalchemy import select

from app.core.identifiers import job_idempotency_key, new_correlation_id
from app.infra.database import get_sessionmaker
from app.infra.jobs import enqueue_job, get_job
from app.infra.models import AgentJob, AgentJobAttempt
from app.runtime.graph_runner import execute_job
from app.runtime.retries import JobState
from app.runtime.task_state import WorkflowState
from app.usecases.registry import register
from tests.conftest import requires_db

pytestmark = requires_db

USE_CASE_ID = "resume_probe"

#: Call counters live outside the graph state on purpose. Anything inside
#: state is checkpointed and restored, so a counter kept there could not
#: distinguish "this node ran twice" from "this node ran once and its state
#: was reloaded".
CALLS: dict[str, int] = {"first": 0, "second": 0}
#: Drives a failure that is *not* part of the checkpointed payload, the way
#: a real transient failure - an upstream timeout - is not.
FAIL_SECOND_UNTIL: dict[str, int] = {"attempts": 0}


async def first(state: WorkflowState) -> WorkflowState:
    CALLS["first"] += 1
    return WorkflowState(notes=["first"])


async def second(state: WorkflowState) -> WorkflowState:
    CALLS["second"] += 1
    if CALLS["second"] <= FAIL_SECOND_UNTIL["attempts"]:
        raise TimeoutError("simulated upstream timeout")
    return WorkflowState(notes=["second"], outcome="RESUMED_OK")


def build() -> StateGraph:
    graph = StateGraph(WorkflowState)
    graph.add_node("first", first)
    graph.add_node("second", second)
    graph.add_edge(START, "first")
    graph.add_edge("first", "second")
    graph.add_edge("second", END)
    return graph


register(USE_CASE_ID, build)


async def make_job() -> uuid.UUID:
    reference = f"resume-{uuid.uuid4()}"
    async with get_sessionmaker()() as session:
        job, _ = await enqueue_job(
            session,
            product_id="scholarship_finder",
            use_case_id=USE_CASE_ID,
            input_reference=reference,
            correlation_id=new_correlation_id(),
            idempotency_key=job_idempotency_key(
                use_case=USE_CASE_ID, workflow_version="v1", input_reference=reference
            ),
            workflow_version="v1",
            payload={},
        )
        job_id = job.job_id
        await session.commit()
    return job_id


async def clear_backoff(job_id: uuid.UUID) -> None:
    """Make a retrying job due now, as its next_attempt_at eventually would."""
    async with get_sessionmaker()() as session:
        job = await get_job(session, job_id)
        assert job is not None
        job.next_attempt_at = None
        await session.commit()


def reset() -> None:
    CALLS["first"] = CALLS["second"] = 0
    FAIL_SECOND_UNTIL["attempts"] = 0


async def test_a_retry_resumes_instead_of_replaying_completed_work():
    """The assertion that matters: `first` succeeded on attempt one and must
    not run again. If resume were broken this test would still reach
    `completed` - only the call count catches it."""
    reset()
    FAIL_SECOND_UNTIL["attempts"] = 1
    job_id = await make_job()

    assert await execute_job(job_id) is JobState.RETRY_WAIT
    assert CALLS == {"first": 1, "second": 1}

    await clear_backoff(job_id)
    state = await execute_job(job_id)

    assert state is JobState.COMPLETED
    assert CALLS["first"] == 1, "completed node was replayed instead of resumed"
    assert CALLS["second"] == 2


async def test_a_resumed_run_still_reaches_the_real_result():
    reset()
    FAIL_SECOND_UNTIL["attempts"] = 1
    job_id = await make_job()
    await execute_job(job_id)
    await clear_backoff(job_id)

    await execute_job(job_id)

    async with get_sessionmaker()() as session:
        from app.infra.models import AgentOutput

        output = await session.scalar(select(AgentOutput).where(AgentOutput.job_id == job_id))
    assert output is not None
    assert output.payload["outcome"] == "RESUMED_OK"
    # The note from the pre-failure node survived the interruption rather
    # than being lost or duplicated.
    assert output.payload["notes"] == ["first", "second"]


async def test_each_attempt_is_recorded_separately():
    reset()
    FAIL_SECOND_UNTIL["attempts"] = 1
    job_id = await make_job()
    await execute_job(job_id)
    await clear_backoff(job_id)
    await execute_job(job_id)

    async with get_sessionmaker()() as session:
        attempts = (
            (
                await session.execute(
                    select(AgentJobAttempt)
                    .where(AgentJobAttempt.job_id == job_id)
                    .order_by(AgentJobAttempt.attempt_number)
                )
            )
            .scalars()
            .all()
        )
        job = await session.scalar(select(AgentJob).where(AgentJob.job_id == job_id))

    assert [a.attempt_number for a in attempts] == [1, 2]
    assert [a.outcome_state for a in attempts] == ["retry_wait", "completed"]
    assert attempts[0].error_class == "retryable"
    assert job is not None and job.attempts == 2


async def test_repeated_failures_eventually_park_the_job():
    """Resume must not become an infinite loop: a node that keeps failing
    has to exhaust its attempts and land somewhere a human can see it."""
    reset()
    FAIL_SECOND_UNTIL["attempts"] = 99
    job_id = await make_job()

    for _ in range(5):
        await execute_job(job_id)
        await clear_backoff(job_id)

    job = await make_loaded(job_id)
    assert job.state == "failed_review"
    assert job.attempts == 5


async def make_loaded(job_id: uuid.UUID) -> AgentJob:
    async with get_sessionmaker()() as session:
        job = await get_job(session, job_id)
        assert job is not None
        return job
