import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Query, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import JobAccepted, JobStatus, JobSubmission, SweepResult
from app.core.config import get_settings
from app.core.errors import problem
from app.core.identifiers import job_idempotency_key, new_correlation_id
from app.core.security import require_agent_service
from app.infra.database import get_db
from app.infra.jobs import enqueue_job, get_job
from app.infra.metrics import collect
from app.runtime.graph_runner import execute_job
from app.runtime.sweeper import sweep_once
from app.usecases.registry import is_known, known_use_cases

logger = logging.getLogger("app.api.jobs")

router = APIRouter(
    prefix="/internal/agent",
    tags=["agent"],
    dependencies=[Depends(require_agent_service)],
)

#: How long a job's inputs, outputs and page excerpts are retained. Set on
#: the row at creation so retention travels with the record rather than
#: depending on whichever cleanup policy happens to be deployed.
RETENTION_DAYS = 90

#: asyncio holds only a weak reference to a running task, so a task nothing
#: else refers to can be garbage collected mid-run. Keeping them here until
#: they finish is what stops a workflow disappearing silently.
_BACKGROUND: set[asyncio.Task] = set()


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _BACKGROUND.add(task)
    task.add_done_callback(_BACKGROUND.discard)


@router.post("/jobs", response_model=JobAccepted, status_code=status.HTTP_202_ACCEPTED)
async def submit_job(
    request: Request,
    body: JobSubmission,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> JobAccepted | JSONResponse:
    """Accept work and start it in the background.

    202, not 200: a run fetches several pages and makes several model calls,
    far longer than an HTTP request should be held open. Scholarship Finder
    runs its jobs inline inside the QStash callback and is bounded by that
    request's lifetime as a result; progress here is read from
    `GET /jobs/{job_id}`.

    Submitting the same work twice returns the same job with
    `created: false` rather than starting a second run. The unique
    constraint on the idempotency key enforces that, not this handler.
    """
    if not is_known(body.use_case_id):
        # Rejected here rather than queued: a job for a use case this build
        # cannot run is guaranteed to fail, and failing now tells the caller
        # something they can act on.
        return problem(
            request,
            422,
            "Unprocessable Content",
            "UNKNOWN_USE_CASE",
            f"No workflow for {body.use_case_id!r}; known: {', '.join(known_use_cases())}",
        )

    settings = get_settings()
    workflow_version = body.workflow_version or settings.workflow_version
    correlation_id = body.correlation_id or new_correlation_id()
    idempotency_key = job_idempotency_key(
        use_case=body.use_case_id,
        workflow_version=workflow_version,
        input_reference=body.input_reference,
    )

    job, created = await enqueue_job(
        db,
        product_id=body.product_id,
        use_case_id=body.use_case_id,
        input_reference=body.input_reference,
        correlation_id=correlation_id,
        idempotency_key=idempotency_key,
        workflow_version=workflow_version,
        payload=body.payload,
        expires_at=datetime.now(UTC) + timedelta(days=RETENTION_DAYS),
    )
    job_id, job_state = job.job_id, job.state

    # Committed before the worker is started, not left to dependency
    # teardown. Scholarship Finder's lesson, in its own words: dispatch must
    # happen after the transaction commits - otherwise the worker looks for
    # a row that is not visible yet and finds nothing to claim.
    await db.commit()

    logger.info(
        "agent_job_submitted",
        extra={
            "job_id": str(job_id),
            "use_case_id": body.use_case_id,
            "input_reference": body.input_reference,
            "correlation_id": correlation_id,
            # Not "created": LogRecord already owns that attribute (the
            # record's own timestamp) and logging raises on the collision.
            "job_created": created,
        },
    )

    if created:
        # Only for a genuinely new job. Spawning on a redelivery would have
        # two workers racing for one row - survivable, because claiming is
        # atomic, but pointless work.
        _spawn(execute_job(job_id))

    response.headers["Location"] = f"{request.url.path}/{job_id}"
    return JobAccepted(
        job_id=job_id,
        state=job_state,
        created=created,
        correlation_id=correlation_id,
        workflow_version=workflow_version,
    )


@router.post("/jobs/run-due", response_model=SweepResult)
async def run_due_jobs(
    limit: int = Query(default=20, ge=1, le=1000),
) -> SweepResult:
    """Drain due jobs now, synchronously, and report what happened.

    Declared before `/jobs/{job_id}` so the literal path wins the match.

    The manual escape hatch. Scholarship Finder needed one even with
    scheduled delivery in place, and this service has no external queue at
    all - when the in-process sweeper is wedged or a deploy stranded a
    batch, this is how an operator gets the queue moving without waiting for
    the next interval.
    """
    return SweepResult(**await sweep_once(limit=limit))


@router.get("/metrics")
async def read_metrics(
    workflow_version: str | None = Query(default=None, max_length=64),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Counters for a pilot report.

    Filter by `workflow_version` to read one version's pilot in isolation -
    otherwise a rerun under a new version is mixed in with the old one and
    neither number means anything.
    """
    return await collect(db, workflow_version=workflow_version)


@router.get("/jobs/{job_id}", response_model=JobStatus)
async def read_job(
    request: Request, job_id: uuid.UUID, db: AsyncSession = Depends(get_db)
) -> JobStatus | JSONResponse:
    job = await get_job(db, job_id)
    if job is None:
        return problem(request, 404, "Not Found", "JOB_NOT_FOUND", "No such job")
    return JobStatus.model_validate(job, from_attributes=True)
