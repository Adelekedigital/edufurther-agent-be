"""Per-host pacing.

Covered directly rather than through the fetchers, because the suite runs
with pacing disabled: a real sleep per host would add minutes to a run
whose fetches are all mocked.
"""

import asyncio
import time

import pytest

from app.tools.throttle import host_pace, reset_for_tests


@pytest.fixture(autouse=True)
def _clean_hosts():
    reset_for_tests()
    yield
    reset_for_tests()


async def test_a_second_request_to_the_same_host_waits():
    started: list[float] = []

    async def fetch():
        async with host_pace("https://example.org/a", 0.20):
            started.append(time.monotonic())

    await fetch()
    await fetch()

    assert started[1] - started[0] >= 0.20


async def test_a_different_host_is_not_delayed():
    """Pacing is per origin. One slow site must not hold up the rest of a
    batch - that would turn politeness into a global rate limit."""
    async with host_pace("https://slow.test/a", 0.30):
        pass

    began = time.monotonic()
    async with host_pace("https://other.test/a", 0.30):
        pass

    assert time.monotonic() - began < 0.10


async def test_concurrent_requests_to_one_host_are_serialized():
    """The burst this exists to prevent.

    Spacing without a lock lets every coroutine read the same "last
    request" stamp, all conclude nothing is due, and all fire together -
    which is exactly the ten-at-once pattern that produced the connect
    failures.
    """
    order: list[float] = []

    async def fetch():
        async with host_pace("https://example.org/a", 0.15):
            order.append(time.monotonic())

    await asyncio.gather(*(fetch() for _ in range(3)))

    gaps = [order[i + 1] - order[i] for i in range(len(order) - 1)]
    assert all(gap >= 0.15 for gap in gaps), gaps


async def test_a_zero_interval_disables_pacing_entirely():
    began = time.monotonic()
    for _ in range(5):
        async with host_pace("https://example.org/a", 0):
            pass
    assert time.monotonic() - began < 0.05


async def test_the_stamp_is_taken_after_the_request_not_before():
    """The gap that matters is between one request finishing and the next
    starting. Stamping on entry would let a slow fetch be followed
    instantly by the next one."""
    async with host_pace("https://example.org/a", 0.20):
        await asyncio.sleep(0.20)

    began = time.monotonic()
    async with host_pace("https://example.org/a", 0.20):
        pass

    assert time.monotonic() - began >= 0.19


async def test_a_failure_inside_the_block_still_stamps():
    """A refused request is still a request the origin saw; not stamping it
    would let a failing host be retried with no pacing at all."""
    with pytest.raises(RuntimeError):
        async with host_pace("https://example.org/a", 0.20):
            raise RuntimeError("boom")

    began = time.monotonic()
    async with host_pace("https://example.org/a", 0.20):
        pass

    assert time.monotonic() - began >= 0.19


async def test_a_url_without_a_host_is_passed_through():
    async with host_pace("not-a-url", 0.20):
        pass
