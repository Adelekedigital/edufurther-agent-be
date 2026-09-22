import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class JobSubmission(BaseModel):
    """A request to run a workflow over one unit of product work.

    `extra="forbid"` for the same reason the AI Router forbids it: an
    unrecognised field is far more often a caller sending something this
    service will silently ignore than it is a harmless extra. Failing tells
    them; ignoring does not.
    """

    model_config = ConfigDict(extra="forbid")

    product_id: str = Field(min_length=1, max_length=64)
    use_case_id: str = Field(min_length=1, max_length=64)
    #: The product's own identifier for the work - a discovery id, usually.
    #: Treated as opaque: the agent never parses a product key.
    input_reference: str = Field(min_length=1, max_length=255)
    #: Supplied by the caller when it already has one, so a single unit of
    #: work is traceable across services. Generated here when absent.
    correlation_id: str | None = Field(default=None, min_length=1, max_length=128)
    #: Pin a run to a specific workflow version. Normally omitted; useful
    #: for deliberately reprocessing a record under a new version.
    workflow_version: str | None = Field(default=None, min_length=1, max_length=64)
    payload: dict[str, Any] = Field(default_factory=dict)


class JobAccepted(BaseModel):
    """202 body.

    `created` distinguishes a new run from a redelivery that collapsed onto
    an existing job, so an at-least-once caller can tell the difference
    without treating the second 202 as a second unit of work.
    """

    job_id: uuid.UUID
    state: str
    created: bool
    correlation_id: str
    workflow_version: str


class JobStatus(BaseModel):
    job_id: uuid.UUID
    product_id: str
    use_case_id: str
    input_reference: str
    correlation_id: str
    workflow_version: str
    state: str
    attempts: int
    last_error: str | None
    next_attempt_at: datetime | None
    lease_expires_at: datetime | None
    created_at: datetime
    updated_at: datetime


class HealthResponse(BaseModel):
    status: str
    service: str
    version: str


class MigrationStatus(BaseModel):
    applied: str | None
    expected: str | None
    up_to_date: bool


class ReadyResponse(BaseModel):
    status: str
    service: str
    migration: MigrationStatus


class SweepResult(BaseModel):
    """What one recovery pass did.

    `remaining` is uncapped, unlike the others, so an operator draining a
    backlog can see what this pass did not reach rather than inferring it
    from an empty-looking result.
    """

    reclaimed: int
    picked_up: int
    completed: int
    failed: int
    remaining: int
