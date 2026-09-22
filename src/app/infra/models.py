"""Agent-owned operational state.

Deliberately narrow: this service owns *execution* state - which node ran,
which tool was called, what the model returned, why a run failed. It does not
own business state. There is no scholarship table here, no review task, no
publication status, and there must not be. Duplicating the product's
canonical schema would create a second source of truth that drifts, and the
agent has no authority to resolve a disagreement between the two.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.core.identifiers import new_uuid7
from app.runtime.retries import JobState


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


#: Stored as VARCHAR plus a CHECK rather than a native PostgreSQL enum.
#: A native enum binds as `$1::jobstate`, a type no migration created, and
#: adding a value to one later needs an ALTER TYPE outside a transaction.
JOB_STATE_VALUES = tuple(state.value for state in JobState)
_JOB_STATE_CHECK = "state IN ({})".format(", ".join(f"'{value}'" for value in JOB_STATE_VALUES))


class AgentJob(Base, TimestampMixin):
    __tablename__ = "agent_jobs"

    job_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=new_uuid7
    )
    product_id: Mapped[str] = mapped_column(String(64), nullable=False)
    use_case_id: Mapped[str] = mapped_column(String(64), nullable=False)
    #: The product's identifier for the work - a discovery id, usually.
    #: Opaque here on purpose: the agent does not interpret product keys.
    input_reference: Mapped[str] = mapped_column(String(255), nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    #: Deterministic per (use case, workflow version, input). The unique
    #: constraint is what makes at-least-once delivery safe: a redelivered
    #: submission collapses onto the existing row instead of starting a
    #: second run. A code-level check alone would not survive two workers.
    idempotency_key: Mapped[str] = mapped_column(String(500), nullable=False)
    workflow_version: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(30), nullable=False, default=JobState.QUEUED.value)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: Retention horizon for inputs, outputs and page excerpts held against
    #: this job. Set at creation so retention is a property of the record,
    #: not of whichever cleanup policy happens to be running.
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_agent_jobs_idempotency_key"),
        CheckConstraint(_JOB_STATE_CHECK, name="ck_agent_jobs_state"),
        Index("ix_agent_jobs_state", "state"),
        Index("ix_agent_jobs_next_attempt_at", "next_attempt_at"),
        Index("ix_agent_jobs_input_reference", "input_reference"),
    )


class AgentJobAttempt(Base, TimestampMixin):
    """One row per execution attempt, kept even when the attempt succeeded.

    Attempt history is how "this job took four tries and two models" stays
    answerable after the fact; collapsing it into a counter on the job loses
    exactly the detail an incident needs.
    """

    __tablename__ = "agent_job_attempts"

    attempt_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=new_uuid7
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("agent_jobs.job_id", ondelete="CASCADE"), nullable=False
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    outcome_state: Mapped[str | None] = mapped_column(String(30), nullable=True)
    error_class: Mapped[str | None] = mapped_column(String(20), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("job_id", "attempt_number", name="uq_agent_job_attempts_job_number"),
        Index("ix_agent_job_attempts_job_id", "job_id"),
    )


class AgentToolCall(Base, TimestampMixin):
    __tablename__ = "agent_tool_calls"

    tool_call_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=new_uuid7
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("agent_jobs.job_id", ondelete="CASCADE"), nullable=False
    )
    attempt_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tool: Mapped[str] = mapped_column(String(60), nullable=False)
    #: The URL or query. Never credentials - tool keys stay in settings and
    #: are not part of a call record.
    target: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: Shape and size only, never fetched page text. Page content belongs
    #: with the evidence it supports, in the product, not scattered here.
    response_meta: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (Index("ix_agent_tool_calls_job_id", "job_id"),)


class AgentOutput(Base, TimestampMixin):
    """A node's result, with the provenance needed to explain it later."""

    __tablename__ = "agent_outputs"

    output_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=new_uuid7
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("agent_jobs.job_id", ondelete="CASCADE"), nullable=False
    )
    node: Mapped[str] = mapped_column(String(60), nullable=False)
    #: Which candidate this output belongs to on a split list page. The
    #: empty string means the output is about the job as a whole - not NULL,
    #: because NULL does not participate in a unique constraint, and the
    #: whole point of this one is that a resumed run overwrites rather than
    #: duplicates.
    candidate_key: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    model: Mapped[str | None] = mapped_column(String(120), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model_policy_version: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        UniqueConstraint("job_id", "node", "candidate_key", name="uq_agent_outputs_job_node_key"),
        Index("ix_agent_outputs_job_id", "job_id"),
    )


class AgentError(Base, TimestampMixin):
    __tablename__ = "agent_errors"

    error_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=new_uuid7
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("agent_jobs.job_id", ondelete="CASCADE"), nullable=False
    )
    attempt_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    node: Mapped[str | None] = mapped_column(String(60), nullable=True)
    error_class: Mapped[str] = mapped_column(String(20), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    context: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    __table_args__ = (Index("ix_agent_errors_job_id", "job_id"),)


class ResearchProviderUsage(Base):
    """Monthly call budget per external provider.

    Ported from Scholarship Finder, including the composite primary key that
    lets reserve-and-check be a single atomic upsert. Owning this here is
    what makes agent-side tool budgets independent of the product's.
    """

    __tablename__ = "agent_research_usage"

    provider: Mapped[str] = mapped_column(String(40), primary_key=True)
    #: "YYYY-MM"; the period rolls by key rather than by a scheduled reset,
    #: so a missed cron cannot hand out an unlimited month.
    period_key: Mapped[str] = mapped_column(String(7), primary_key=True)
    calls_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
