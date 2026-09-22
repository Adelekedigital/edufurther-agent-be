"""Agent runtime state.

Creates the execution-state tables this service owns. Nothing here
duplicates the product's canonical schema - no scholarship, no review task,
no publication status. Those stay in Scholarship Finder, which remains the
system of record.

Revision ID: 0001_agent_runtime_state
Revises:
Create Date: 2026-09-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_agent_runtime_state"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JOB_STATES = (
    "queued",
    "running",
    "retry_wait",
    "waiting_review",
    "completed",
    "failed_review",
    "cancelled",
)


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    ]


def upgrade() -> None:
    op.create_table(
        "agent_jobs",
        sa.Column("job_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("product_id", sa.String(64), nullable=False),
        sa.Column("use_case_id", sa.String(64), nullable=False),
        sa.Column("input_reference", sa.String(255), nullable=False),
        sa.Column("correlation_id", sa.String(128), nullable=False),
        sa.Column("idempotency_key", sa.String(500), nullable=False),
        sa.Column("workflow_version", sa.String(64), nullable=False),
        sa.Column("state", sa.String(30), nullable=False, server_default="queued"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("payload", postgresql.JSONB(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
    )
    # The guarantee that makes at-least-once submission safe. A code-level
    # duplicate check does not survive two workers racing; this does.
    op.create_unique_constraint(
        "uq_agent_jobs_idempotency_key", "agent_jobs", ["idempotency_key"]
    )
    op.create_check_constraint(
        "ck_agent_jobs_state",
        "agent_jobs",
        sa.text("state IN ({})".format(", ".join(f"'{state}'" for state in JOB_STATES))),
    )
    op.create_index("ix_agent_jobs_state", "agent_jobs", ["state"])
    op.create_index("ix_agent_jobs_next_attempt_at", "agent_jobs", ["next_attempt_at"])
    op.create_index("ix_agent_jobs_input_reference", "agent_jobs", ["input_reference"])

    op.create_table(
        "agent_job_attempts",
        sa.Column("attempt_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "job_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent_jobs.job_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("outcome_state", sa.String(30), nullable=True),
        sa.Column("error_class", sa.String(20), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        *_timestamps(),
    )
    op.create_unique_constraint(
        "uq_agent_job_attempts_job_number",
        "agent_job_attempts",
        ["job_id", "attempt_number"],
    )
    op.create_index("ix_agent_job_attempts_job_id", "agent_job_attempts", ["job_id"])

    op.create_table(
        "agent_tool_calls",
        sa.Column("tool_call_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "job_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent_jobs.job_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("attempt_number", sa.Integer(), nullable=True),
        sa.Column("tool", sa.String(60), nullable=False),
        sa.Column("target", sa.Text(), nullable=True),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("response_meta", postgresql.JSONB(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        *_timestamps(),
    )
    op.create_index("ix_agent_tool_calls_job_id", "agent_tool_calls", ["job_id"])

    op.create_table(
        "agent_outputs",
        sa.Column("output_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "job_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent_jobs.job_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("node", sa.String(60), nullable=False),
        # Empty string rather than NULL for "the job as a whole": NULL does
        # not participate in a unique constraint, and the point of the
        # constraint below is that a resumed run overwrites its own earlier
        # output instead of appending a second copy.
        sa.Column("candidate_key", sa.String(255), nullable=False, server_default=""),
        sa.Column("payload", postgresql.JSONB(), nullable=True),
        sa.Column("model", sa.String(120), nullable=True),
        sa.Column("prompt_version", sa.String(64), nullable=True),
        sa.Column("model_policy_version", sa.String(64), nullable=True),
        *_timestamps(),
    )
    op.create_unique_constraint(
        "uq_agent_outputs_job_node_key",
        "agent_outputs",
        ["job_id", "node", "candidate_key"],
    )
    op.create_index("ix_agent_outputs_job_id", "agent_outputs", ["job_id"])

    op.create_table(
        "agent_errors",
        sa.Column("error_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "job_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent_jobs.job_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("attempt_number", sa.Integer(), nullable=True),
        sa.Column("node", sa.String(60), nullable=True),
        sa.Column("error_class", sa.String(20), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("context", postgresql.JSONB(), nullable=True),
        *_timestamps(),
    )
    op.create_index("ix_agent_errors_job_id", "agent_errors", ["job_id"])

    # Composite primary key so reserve-and-check is a single atomic upsert.
    op.create_table(
        "agent_research_usage",
        sa.Column("provider", sa.String(40), primary_key=True),
        sa.Column("period_key", sa.String(7), primary_key=True),
        sa.Column("calls_used", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_table("agent_research_usage")
    op.drop_index("ix_agent_errors_job_id", table_name="agent_errors")
    op.drop_table("agent_errors")
    op.drop_index("ix_agent_outputs_job_id", table_name="agent_outputs")
    op.drop_table("agent_outputs")
    op.drop_index("ix_agent_tool_calls_job_id", table_name="agent_tool_calls")
    op.drop_table("agent_tool_calls")
    op.drop_index("ix_agent_job_attempts_job_id", table_name="agent_job_attempts")
    op.drop_table("agent_job_attempts")
    op.drop_index("ix_agent_jobs_input_reference", table_name="agent_jobs")
    op.drop_index("ix_agent_jobs_next_attempt_at", table_name="agent_jobs")
    op.drop_index("ix_agent_jobs_state", table_name="agent_jobs")
    op.drop_table("agent_jobs")
