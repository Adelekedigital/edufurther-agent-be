import asyncio
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.api.schemas import HealthResponse, MigrationStatus, ReadyResponse
from app.core.config import get_settings
from app.core.errors import problem
from app.infra.database import get_sessionmaker
from app.infra.migration_status import migration_status

logger = logging.getLogger("app.api.health")

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Liveness. Touches nothing external.

    A liveness probe that checks the database restarts the process when the
    database blips, which is precisely when restarting helps least.
    """
    settings = get_settings()
    return HealthResponse(status="ok", service=settings.app_name, version=settings.app_version)


@router.get("/ready", response_model=ReadyResponse)
async def ready(request: Request) -> ReadyResponse | JSONResponse:
    """Readiness: can this instance actually serve traffic?

    Bounded by `db_connect_timeout_seconds` so an unreachable database fails
    the probe promptly rather than holding it until the platform's own
    timeout.

    Schema drift is reported but never fails the probe. Refusing readiness
    over a pending migration takes the service down at exactly the moment
    someone is trying to deploy the migration that fixes it.
    """
    settings = get_settings()
    try:
        async with get_sessionmaker()() as session:
            await asyncio.wait_for(
                session.execute(text("SELECT 1")),
                timeout=settings.db_connect_timeout_seconds,
            )
            migration = await migration_status(session)
    except Exception as exc:
        logger.warning("readiness_check_failed", extra={"error": str(exc)})
        return problem(
            request,
            503,
            "Service Unavailable",
            "DEPENDENCY_NOT_READY",
            "Database is not reachable",
            retryable=True,
        )

    if not migration.up_to_date:
        logger.warning("schema_migration_drift", extra=migration.as_dict())

    return ReadyResponse(
        status="ready",
        service=settings.app_name,
        migration=MigrationStatus(
            applied=migration.applied,
            expected=migration.expected,
            up_to_date=migration.up_to_date,
        ),
    )
