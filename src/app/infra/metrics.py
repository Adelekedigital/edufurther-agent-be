"""Operational counters for a pilot report.

Spec §16 asks for workflow version, outcome, tool calls, latency and
failure reason per job. The per-job detail already lives in `agent_jobs`,
`agent_outputs`, `agent_tool_calls` and `agent_errors`; this aggregates it
so the questions that decide whether to widen the batch can be answered
without a database session.

Deliberately counts rather than rates. "62 of 80 jobs completed" is a fact;
"77.5% success" invites being read as a quality measure when it is a
liveness one - a job that completed with MORE_EVIDENCE_REQUIRED did its
work correctly.
"""

from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infra.models import AgentError, AgentJob, AgentOutput, AgentToolCall


async def _counts(db: AsyncSession, statement: Select) -> dict[str, int]:
    """Run a `group_by` count into a plain dict, dropping null keys.

    A null group is a row with no value for the thing being counted, which
    is not a category anyone wants in a report.
    """
    rows = (await db.execute(statement)).all()
    return {str(key): int(value) for key, value in rows if key is not None}


async def collect(db: AsyncSession, *, workflow_version: str | None = None) -> dict[str, Any]:
    scoped = [AgentJob.workflow_version == workflow_version] if workflow_version else []

    jobs_by_state = await _counts(
        db, select(AgentJob.state, func.count()).where(*scoped).group_by(AgentJob.state)
    )

    outcome_column = AgentOutput.payload["outcome"].astext
    outcomes = await _counts(
        db,
        select(outcome_column, func.count())
        .join(AgentJob, AgentJob.job_id == AgentOutput.job_id)
        .where(AgentOutput.node == "workflow", *scoped)
        .group_by(outcome_column),
    )

    tools = await _counts(
        db,
        select(AgentToolCall.tool, func.count())
        .join(AgentJob, AgentJob.job_id == AgentToolCall.job_id)
        .where(*scoped)
        .group_by(AgentToolCall.tool),
    )
    tool_statuses = await _counts(
        db,
        select(AgentToolCall.status, func.count())
        .join(AgentJob, AgentJob.job_id == AgentToolCall.job_id)
        .where(*scoped)
        .group_by(AgentToolCall.status),
    )

    # Grouped by class, not by message: "14 permanent failures" is the
    # number that decides whether to widen the batch, where fourteen
    # distinct messages is a reading exercise.
    errors = await _counts(
        db,
        select(AgentError.error_class, func.count())
        .join(AgentJob, AgentJob.job_id == AgentError.job_id)
        .where(*scoped)
        .group_by(AgentError.error_class),
    )

    attempts = int(await db.scalar(select(func.sum(AgentJob.attempts)).where(*scoped)) or 0)
    total = sum(jobs_by_state.values())

    return {
        "workflow_version": workflow_version,
        "jobs": {"total": total, "by_state": jobs_by_state},
        "outcomes": outcomes,
        "tool_calls": {"by_tool": tools, "by_status": tool_statuses},
        "errors_by_class": errors,
        "total_attempts": attempts,
        # Above 1.0 means work is being redone. A pilot where this climbs is
        # one to look at before widening, whatever the outcome counts say.
        "attempts_per_job": round(attempts / total, 2) if total else 0.0,
    }
