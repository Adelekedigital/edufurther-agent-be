"""Job lifecycle arithmetic. Pure - no database, no fixtures, no clock."""

from datetime import UTC, datetime, timedelta

import pytest

from app.runtime.retries import (
    MAX_BACKOFF_SECONDS,
    ErrorClass,
    JobState,
    backoff_seconds,
    cancel,
    claim,
    classify_error,
    fail,
    is_due,
    is_lease_expired,
    lease_expiry,
    succeed,
)

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)


def test_backoff_doubles_then_holds_at_the_cap():
    assert [backoff_seconds(n) for n in (1, 2, 3, 4, 5)] == [60, 120, 240, 480, 960]
    assert backoff_seconds(20) == MAX_BACKOFF_SECONDS


def test_backoff_never_returns_zero_for_a_first_attempt():
    """A zero delay turns a failing job into a hot loop against whatever
    just rejected it."""
    assert backoff_seconds(0) > 0
    assert backoff_seconds(-1) > 0


@pytest.mark.parametrize("state", [JobState.QUEUED, JobState.RETRY_WAIT])
def test_a_due_job_is_claimable(state):
    assert is_due(state, None, now=NOW)
    assert is_due(state, NOW - timedelta(seconds=1), now=NOW)


@pytest.mark.parametrize(
    "state",
    [JobState.RUNNING, JobState.COMPLETED, JobState.FAILED_REVIEW, JobState.CANCELLED],
)
def test_a_job_that_is_not_waiting_is_never_claimable(state):
    assert not is_due(state, None, now=NOW)


def test_a_job_scheduled_for_later_is_not_yet_due():
    assert not is_due(JobState.RETRY_WAIT, NOW + timedelta(seconds=1), now=NOW)


def test_claiming_takes_a_lease_and_counts_the_attempt():
    transition = claim(JobState.QUEUED, 0, lease_seconds=900, now=NOW)

    assert transition is not None
    assert transition.state is JobState.RUNNING
    assert transition.attempts == 1
    assert transition.lease_expires_at == NOW + timedelta(seconds=900)
    # Cleared, so a reclaimed job is not held back by its old backoff.
    assert transition.next_attempt_at is None


def test_claiming_a_job_that_is_not_due_returns_none_rather_than_raising():
    """A worker racing the sweeper for the same row is expected, not
    exceptional - exactly one of them wins and the other moves on."""
    assert claim(JobState.RUNNING, 1, lease_seconds=900, now=NOW) is None
    assert claim(JobState.COMPLETED, 1, lease_seconds=900, now=NOW) is None


def test_a_missing_lease_counts_as_expired():
    """A row marked running with no lease cannot be reasoned about, and
    stranding it forever is worse than reclaiming work that is idempotent
    by construction."""
    assert is_lease_expired(None, now=NOW)


def test_lease_expiry_is_compared_inclusively():
    assert is_lease_expired(NOW, now=NOW)
    assert not is_lease_expired(NOW + timedelta(seconds=1), now=NOW)
    assert lease_expiry(900, now=NOW) == NOW + timedelta(seconds=900)


def test_a_retryable_failure_is_rescheduled_with_backoff():
    transition = fail(
        1, "upstream timeout", error_class=ErrorClass.RETRYABLE, max_attempts=5, now=NOW
    )

    assert transition.state is JobState.RETRY_WAIT
    assert transition.next_attempt_at == NOW + timedelta(seconds=60)
    assert transition.last_error == "upstream timeout"


def test_a_permanent_failure_skips_the_remaining_attempts():
    """Re-running a workflow over an input that cannot parse produces the
    same failure four more times, at four times the cost."""
    transition = fail(
        1, "unparseable source", error_class=ErrorClass.PERMANENT, max_attempts=5, now=NOW
    )

    assert transition.state is JobState.FAILED_REVIEW
    assert transition.next_attempt_at is None


def test_exhausting_attempts_parks_the_job_durably():
    transition = fail(5, "still failing", error_class=ErrorClass.RETRYABLE, max_attempts=5, now=NOW)

    assert transition.state is JobState.FAILED_REVIEW
    assert transition.last_error == "still failing"


def test_a_long_error_is_truncated_rather_than_rejected():
    transition = fail(1, "x" * 10_000, error_class=ErrorClass.PERMANENT, max_attempts=5, now=NOW)

    assert transition.last_error is not None
    assert len(transition.last_error) == 2_000


def test_terminal_transitions_carry_the_attempt_count_forward():
    assert succeed(3).state is JobState.COMPLETED
    assert succeed(3).attempts == 3
    assert cancel(2).state is JobState.CANCELLED


@pytest.mark.parametrize(
    "exc",
    [
        TimeoutError("slow"),
        type("ConnectTimeout", (Exception,), {})("t"),
        type("ReadTimeout", (Exception,), {})("t"),
        type("RateLimitError", (Exception,), {})("t"),
        type("ServiceUnavailable", (Exception,), {})("t"),
        type("PoolError", (Exception,), {})("t"),
    ],
)
def test_transient_failures_are_classified_retryable(exc):
    assert classify_error(exc) is ErrorClass.RETRYABLE


@pytest.mark.parametrize("exc", [ValueError("bad url"), KeyError("missing"), TypeError("x")])
def test_deterministic_failures_are_classified_permanent(exc):
    assert classify_error(exc) is ErrorClass.PERMANENT


def test_an_unrecognised_failure_defaults_to_permanent():
    """Retrying something we do not understand burns budget to reach the
    same place, and a job parked in failed_review is visible where one
    quietly retrying five times is not."""

    class SomethingNew(Exception):
        pass

    assert classify_error(SomethingNew("?")) is ErrorClass.PERMANENT


class _StatusError(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"upstream said {status_code}")
        self.status_code = status_code


@pytest.mark.parametrize("status", [500, 502, 503, 504, 429, 408])
def test_an_upstream_server_error_is_retryable(status):
    """Classifying by exception *name* missed these entirely - a client's
    error type name says nothing about the status it wrapped - so a
    momentary 503 from the router or the product parked every in-flight
    job in failed_review, which is exactly what the backoff curve exists
    to avoid."""
    assert classify_error(_StatusError(status)) is ErrorClass.RETRYABLE


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 422])
def test_an_upstream_client_error_is_permanent(status):
    """Retrying a rejected request just reaches the same rejection."""
    assert classify_error(_StatusError(status)) is ErrorClass.PERMANENT
