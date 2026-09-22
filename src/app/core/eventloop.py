"""Event loop selection.

psycopg's async mode - which the LangGraph Postgres checkpointer is built on
- cannot run on Windows' default ProactorEventLoop. It fails at connect
time with a message about the loop, and a connection pool turns that into a
long series of retries rather than one clear error, so the symptom presents
as "everything is slow" rather than "wrong event loop".

Linux, where this service actually deploys, already defaults to a selector
loop, so this is a development-environment fix and a no-op in production.

The selector loop on Windows caps out around 512 sockets and does not
support subprocess transports. Neither matters here: this process makes a
handful of outbound HTTP calls and spawns nothing.
"""

import asyncio
import sys


def install_selector_event_loop_policy() -> None:
    """Call before any event loop is created.

    Deliberately a function rather than an import side effect. A library
    module that reconfigures the global event loop policy just because it
    was imported is the kind of action-at-a-distance that is very hard to
    track down later; the entrypoint and the test suite each opt in.
    """
    if sys.platform != "win32":
        return
    policy = getattr(asyncio, "WindowsSelectorEventLoopPolicy", None)
    if policy is not None:
        asyncio.set_event_loop_policy(policy())


#: Uvicorn 0.36+ passes a loop factory straight to `asyncio.run`, which
#: ignores the global policy above. On Windows it picks ProactorEventLoop
#: whenever it is not using subprocesses - so `fastapi run` gets one and
#: `fastapi dev` (which reloads, and therefore does) does not.
INCOMPATIBLE_LOOP_HINT = (
    "psycopg - and therefore the LangGraph checkpointer - cannot run on "
    "Windows' ProactorEventLoop. Locally, use `uv run fastapi dev`, which "
    "runs on a selector loop. This does not affect Linux deployments, where "
    "a selector loop is already the default."
)


def assert_loop_supports_psycopg() -> None:
    """Fail immediately, and legibly, on an unusable loop.

    Without this the failure is a connection pool retrying quietly until it
    times out: the operator sees a ten-second stall and a `PoolTimeout`,
    which says nothing about the actual cause. Windows-only, so it cannot
    change behaviour in a deployed environment.
    """
    if sys.platform != "win32":
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    if type(loop).__name__ == "ProactorEventLoop":
        raise RuntimeError(INCOMPATIBLE_LOOP_HINT)
