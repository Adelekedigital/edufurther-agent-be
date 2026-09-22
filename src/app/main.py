import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.responses import Response

from app.api import health, jobs
from app.core.config import get_settings
from app.runtime.checkpoints import close_checkpointer, get_checkpointer
from app.runtime.sweeper import run_sweeper

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("app.main")

settings = get_settings()


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Start the checkpointer and the recovery sweeper.

    Both failures are survivable and neither prevents boot: an instance that
    cannot reach Postgres must still answer `/health` and report the problem
    on `/ready`, rather than crash-looping where nobody can query it.
    """
    stop = asyncio.Event()
    sweeper: asyncio.Task[None] | None = None
    try:
        await get_checkpointer()
    except Exception:
        # The first job to run will retry this lazily.
        logger.exception("checkpointer_unavailable_at_startup")
    try:
        sweeper = asyncio.create_task(run_sweeper(stop))
        yield
    finally:
        stop.set()
        if sweeper is not None:
            sweeper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sweeper
        await close_checkpointer()


app = FastAPI(
    title="Edufurther Agent",
    version=settings.app_version,
    lifespan=lifespan,
    # Closed in deployed environments: the internal surface is not a public
    # contract, and an open schema hands an unauthenticated caller the exact
    # shape of every route. Same posture as Scholarship Finder.
    docs_url=None if settings.is_deployed else "/docs",
    redoc_url=None if settings.is_deployed else "/redoc",
    openapi_url=None if settings.is_deployed else "/openapi.json",
)


@app.middleware("http")
async def request_context(request: Request, call_next: Any) -> Response:
    """Adopt the caller's request id, or mint one.

    Bounded and printability-checked before it is echoed: an id taken from a
    header ends up in logs and in downstream requests, so it must not be
    able to carry control characters or unbounded length.
    """
    inbound = request.headers.get("X-Request-ID", "")
    request_id = (
        inbound if 1 <= len(inbound) <= 128 and inbound.isprintable() else f"req_{uuid4().hex}"
    )
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response


app.include_router(health.router)
app.include_router(jobs.router, prefix="/api/v1")
