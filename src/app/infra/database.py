from collections.abc import AsyncIterator
from functools import lru_cache
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import get_settings

_ASYNC_DRIVER = "postgresql+asyncpg"


def normalize_database_url(url: str) -> str:
    """Accept a managed-provider URL verbatim and make it asyncpg-usable.

    Railway and Neon hand out `postgres://`/`postgresql://` URLs with
    libpq query parameters. asyncpg rejects `sslmode` and does not know
    `channel_binding`, so a pasted URL fails at connect time with an error
    that points at neither. Rewriting here means the value in the dashboard
    and the value in the environment can stay identical.
    """
    if not url:
        return url
    for prefix in (
        "postgresql+psycopg2://",
        "postgresql+psycopg://",
        "postgresql://",
        "postgres://",
    ):
        if url.startswith(prefix):
            url = f"{_ASYNC_DRIVER}://{url[len(prefix) :]}"
            break
    parts = urlsplit(url)
    if not parts.query:
        return url
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)]
    # The mode is carried through verbatim, not reduced to a boolean.
    # asyncpg's `ssl` takes exactly libpq's sslmode vocabulary - it parses
    # the string through its own SSLMode enum - so `ssl=true` is rejected
    # at connect time with "`sslmode` parameter must be one of: ...", an
    # error that names a parameter the URL no longer contains. Passing the
    # mode through also keeps verify-ca and verify-full distinct from
    # require, where collapsing them silently dropped certificate
    # verification.
    rewritten = [
        ("ssl", value) if key == "sslmode" else (key, value)
        for key, value in query
        if key != "channel_binding"
    ]
    return urlunsplit(parts._replace(query=urlencode(rewritten)))


@lru_cache
def _engine_and_sessions() -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    settings = get_settings()
    engine = create_async_engine(
        normalize_database_url(settings.database_url),
        pool_pre_ping=True,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        connect_args={"timeout": settings.db_pool_connect_timeout_seconds},
    )
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def get_engine() -> AsyncEngine:
    return _engine_and_sessions()[0]


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return _engine_and_sessions()[1]


async def get_db() -> AsyncIterator[AsyncSession]:
    """Request-scoped session that commits on a clean response.

    A handler that raises leaves the transaction to roll back untouched -
    the alternative, committing whatever partial writes happened before the
    error, is how a half-written job row outlives the request that failed.
    """
    async with get_sessionmaker()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
