"""LangGraph checkpoint storage.

Checkpointing is what makes "resume after worker interruption" (spec §15)
real rather than aspirational. A Railway deploy, a restart or an OOM lands
mid-run; without checkpoints the sweeper's only option is to start the whole
workflow again, re-fetching every page and re-paying for every model call it
had already made.

Two drivers against one database, deliberately: our own tables use
SQLAlchemy with asyncpg, and LangGraph's Postgres saver is built on psycopg3.
Rather than force one of them onto the other's driver, we translate the URL.
"""

import asyncio
import logging
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg import AsyncConnection
from psycopg.rows import DictRow, dict_row
from psycopg_pool import AsyncConnectionPool

from app.core.config import get_settings
from app.core.eventloop import assert_loop_supports_psycopg

logger = logging.getLogger("app.runtime.checkpoints")

_pool: AsyncConnectionPool[AsyncConnection[DictRow]] | None = None
_saver: AsyncPostgresSaver | None = None
_lock = asyncio.Lock()

#: Bounded so a misconfigured environment surfaces as an error, not a stall.
CONNECT_TIMEOUT_SECONDS = 10.0


def psycopg_dsn(url: str) -> str:
    """Translate the configured SQLAlchemy URL into a psycopg DSN.

    Strips the SQLAlchemy driver suffix, and maps asyncpg's `ssl` query
    parameter back to libpq's `sslmode`, which is what psycopg expects. One
    `DATABASE_URL` in the environment, two drivers that disagree about how
    to spell TLS - translating here keeps that disagreement out of the
    deployment configuration.
    """
    if not url:
        return url
    for prefix in (
        "postgresql+asyncpg://",
        "postgresql+psycopg2://",
        "postgresql+psycopg://",
        "postgres://",
    ):
        if url.startswith(prefix):
            url = "postgresql://" + url[len(prefix) :]
            break
    parts = urlsplit(url)
    if not parts.query:
        return url
    rewritten = [
        ("sslmode", "require" if value.lower() in {"true", "1", "require"} else "disable")
        if key == "ssl"
        else (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
    ]
    return urlunsplit(parts._replace(query=urlencode(rewritten)))


async def get_checkpointer() -> AsyncPostgresSaver:
    """Return the process-wide saver, creating it on first use.

    Lazy rather than import-time so that importing the application does not
    require a reachable database - `/health` must answer even when Postgres
    is down, and the test suite must be able to import the app without one.
    """
    global _pool, _saver
    if _saver is not None:
        return _saver
    async with _lock:
        if _saver is not None:
            return _saver
        assert_loop_supports_psycopg()
        settings = get_settings()
        pool: AsyncConnectionPool[AsyncConnection[DictRow]] = AsyncConnectionPool(
            conninfo=psycopg_dsn(settings.database_url),
            min_size=1,
            max_size=4,
            connection_class=AsyncConnection[DictRow],
            # Matches how LangGraph configures its own connection in
            # `from_conn_string`. row_factory=dict_row is the load-bearing
            # one - the saver reads checkpoint columns by name. The saver
            # manages its own transactions, so autocommit keeps one from
            # being held open for a whole workflow run, and
            # prepare_threshold=0 keeps it working through poolers that do
            # not support prepared statements.
            kwargs={
                "autocommit": True,
                "prepare_threshold": 0,
                "row_factory": dict_row,
            },
            open=False,
            # Without a bound, a pool that cannot connect retries quietly
            # and the symptom reaching the operator is "jobs are slow"
            # rather than "the database is unreachable".
            timeout=CONNECT_TIMEOUT_SECONDS,
        )
        await pool.open(wait=True, timeout=CONNECT_TIMEOUT_SECONDS)
        saver = AsyncPostgresSaver(pool)
        # Creates the checkpoint tables. Idempotent, so it is safe on every
        # boot; LangGraph owns this schema, which is why it is not in our
        # Alembic chain.
        await saver.setup()
        _pool, _saver = pool, saver
        logger.info("checkpointer_ready")
        return saver


async def close_checkpointer() -> None:
    global _pool, _saver
    async with _lock:
        if _pool is not None:
            await _pool.close()
        _pool, _saver = None, None
