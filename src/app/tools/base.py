"""Shared tool vocabulary: failure kinds and call telemetry.

The failure taxonomy matters more than it looks. A tool can fail because
the network was bad, because policy refused the target, because a kill
switch is off, or because a budget is spent - and the runtime's response to
each is different. Collapsing them into one exception type means either
retrying a policy refusal forever or giving up on a transient timeout.
"""

import logging
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.infra.models import AgentToolCall

logger = logging.getLogger("app.tools")


class ToolError(RuntimeError):
    """Base for every tool failure."""


class ToolDisabled(ToolError):
    """A kill switch is off. Permanent until an operator changes it."""


class ToolBudgetExhausted(ToolError):
    """The provider's period budget is spent. Permanent for this period."""


class ToolPolicyRefusal(ToolError):
    """The target was refused: unapproved domain, private host, oversize.

    Never retry one of these, and never route around it by reaching for a
    different tool. A refusal is the guard working.
    """


class ToolStatus:
    OK = "ok"
    ERROR = "error"
    DISABLED = "disabled"
    BUDGET_EXHAUSTED = "budget_exhausted"
    REFUSED = "refused"


@dataclass
class ToolCallRecord:
    tool: str
    target: str | None = None
    status: str = ToolStatus.OK
    duration_ms: int = 0
    #: Shape and size only - content type, byte length, fetch method. Never
    #: the fetched page text: that belongs with the evidence it supports,
    #: in the product, not scattered through this service's telemetry.
    response_meta: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


@asynccontextmanager
async def observe(tool: str, *, target: str | None = None) -> AsyncIterator[ToolCallRecord]:
    """Time a tool call and classify however it ended.

    Re-raises everything. This records what happened; it does not decide
    what to do about it.
    """
    record = ToolCallRecord(tool=tool, target=target)
    started = time.monotonic()
    try:
        yield record
    except ToolDisabled as exc:
        record.status, record.error = ToolStatus.DISABLED, str(exc)
        raise
    except ToolBudgetExhausted as exc:
        record.status, record.error = ToolStatus.BUDGET_EXHAUSTED, str(exc)
        raise
    except (ToolPolicyRefusal, ValueError) as exc:
        # ValueError is how the ported fetch guard signals a refusal.
        record.status, record.error = ToolStatus.REFUSED, str(exc)
        raise
    except Exception as exc:
        record.status, record.error = ToolStatus.ERROR, f"{type(exc).__name__}: {exc}"
        raise
    finally:
        record.duration_ms = int((time.monotonic() - started) * 1000)
        logger.info(
            "tool_call",
            extra={
                "tool": record.tool,
                "tool_status": record.status,
                "duration_ms": record.duration_ms,
            },
        )


async def persist_tool_call(
    db: AsyncSession,
    *,
    job_id: uuid.UUID,
    record: ToolCallRecord,
    attempt_number: int | None = None,
) -> None:
    """Write one tool call to the job's audit trail.

    Separate from `observe` on purpose: a tool must be usable without a
    database session, and telemetry that fails must not take the workflow
    down with it.
    """
    db.add(
        AgentToolCall(
            job_id=job_id,
            attempt_number=attempt_number,
            tool=record.tool,
            target=record.target,
            status=record.status,
            duration_ms=record.duration_ms,
            response_meta=record.response_meta or None,
            error=record.error[:2000] if record.error else None,
        )
    )
    await db.flush()


@asynccontextmanager
async def recorded(
    db: AsyncSession,
    tool: str,
    *,
    target: str | None = None,
    job_id: uuid.UUID | str | None = None,
    attempt_number: int | None = None,
) -> AsyncIterator[ToolCallRecord]:
    """Time a tool call *and* write it to the job's audit trail.

    `observe` alone only logs. Without this the `agent_tool_calls` table
    stays empty and "which tools did this job call, and how did they end?"
    is unanswerable after the fact - which is most of what an incident
    needs.

    Persisting happens in a `finally`, so a refused or failed call is
    recorded too. Those are the ones worth having.

    A telemetry failure never propagates: losing the record of a fetch is
    bad, losing the fetch because recording it failed is worse.
    """
    holder: list[ToolCallRecord] = []
    try:
        async with observe(tool, target=target) as record:
            holder.append(record)
            yield record
    finally:
        if job_id is not None and holder:
            try:
                await persist_tool_call(
                    db,
                    job_id=uuid.UUID(str(job_id)),
                    record=holder[0],
                    attempt_number=attempt_number,
                )
                await db.commit()
            except Exception:
                logger.warning("tool_call_not_recorded", extra={"tool": tool}, exc_info=True)
