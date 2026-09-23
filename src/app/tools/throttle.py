"""Per-host pacing for outbound fetches.

A batch is not a random walk across the web. Discoveries arrive grouped by
source, so ten of them are ten requests to one origin in as many seconds -
and a ten-record run produced a 35% connect-failure rate against a single
host, every failure a `ConnectTimeout` that the runtime correctly called
retryable and correctly retried, spending three attempts to do one page.

Retries are the wrong tool for this. `classify_error` cannot tell
"throttled" from "network blip" - both surface as a connect failure - so
backing off after the fact treats a signal we created ourselves as weather.
Not making the second request too soon is cheaper than recovering from it,
and it is the polite thing to do besides.

Scope, stated plainly: this is per process. One Railway replica today, so
it holds; a second replica would double the real rate. A shared limiter
belongs in the database next to `agent_research_usage` if that day comes,
and this docstring is the note to say so.
"""

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

#: Hosts seen so far, each with its own lock and last-request timestamp.
#: Unbounded in principle, bounded in practice by the number of distinct
#: sources - hundreds, not millions - and a host costs a lock and a float.
_HOSTS: dict[str, tuple[asyncio.Lock, list[float]]] = {}
_REGISTRY_LOCK = asyncio.Lock()


async def _slot(host: str) -> tuple[asyncio.Lock, list[float]]:
    async with _REGISTRY_LOCK:
        if host not in _HOSTS:
            # The timestamp lives in a one-element list so it can be mutated
            # while the lock is held without another round trip here.
            _HOSTS[host] = (asyncio.Lock(), [0.0])
        return _HOSTS[host]


@asynccontextmanager
async def host_pace(url: str, min_interval_seconds: float) -> AsyncIterator[None]:
    """Hold the host's turn, waiting out the remainder of its interval.

    Serializing per host as well as spacing is deliberate. Spacing alone
    would let ten coroutines all read the same "last request" timestamp,
    all decide nothing is due, and all fire at once - which is the burst
    this exists to prevent.

    A non-positive interval disables it entirely, which is what the test
    suite uses: a per-host sleep would add minutes to a run whose fetches
    are mocked anyway.
    """
    if min_interval_seconds <= 0:
        yield
        return
    host = (urlsplit(url).hostname or "").lower()
    if not host:
        yield
        return
    lock, last = await _slot(host)
    async with lock:
        wait = min_interval_seconds - (time.monotonic() - last[0])
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            yield
        finally:
            # Stamped on the way out, not on the way in: the interval we
            # care about is between the end of one request and the start of
            # the next. Stamping on entry would let a slow request be
            # followed immediately by the next one.
            last[0] = time.monotonic()


def reset_for_tests() -> None:
    """Drop all per-host state. Tests only."""
    _HOSTS.clear()
