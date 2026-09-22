"""Job lifecycle arithmetic. Pure - no database, no FastAPI, no clock reads
that a test cannot control.

Ported from Scholarship Finder's `domain/jobs.py`, with one deliberate
change: that service decides retryability from the job *kind*, because every
job of a kind fails the same way. An agent workflow does not - a tool
timeout on the same job that earlier hit an unparseable page are different
failures, and only one of them is worth retrying. So retryability is carried
per failure, classified from the error itself.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

#: 60s, doubling, capped at six hours - Scholarship Finder's curve. Long
#: enough that a provider outage is ridden out rather than burned through.
BASE_BACKOFF_SECONDS = 60
MAX_BACKOFF_SECONDS = 21_600
MAX_ERROR_LENGTH = 2_000


class JobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    RETRY_WAIT = "retry_wait"
    #: Parked pending a human decision in the product, not a failure.
    WAITING_REVIEW = "waiting_review"
    COMPLETED = "completed"
    #: Terminal, durable and inspectable. Never deleted by the runtime: a
    #: failure nobody can look at afterwards is a failure that repeats.
    FAILED_REVIEW = "failed_review"
    CANCELLED = "cancelled"


TERMINAL_STATES = frozenset({JobState.COMPLETED, JobState.FAILED_REVIEW, JobState.CANCELLED})
#: A job in one of these may be picked up by a worker or the sweeper.
CLAIMABLE_STATES = frozenset({JobState.QUEUED, JobState.RETRY_WAIT})


class ErrorClass(StrEnum):
    #: Transient: the same input may well succeed later.
    RETRYABLE = "retryable"
    #: Deterministic: the input itself is the problem, so retrying only
    #: spends budget to reach the same answer.
    PERMANENT = "permanent"


@dataclass(frozen=True)
class JobTransition:
    state: JobState
    attempts: int
    next_attempt_at: datetime | None = None
    lease_expires_at: datetime | None = None
    last_error: str | None = None


def _now(now: datetime | None = None) -> datetime:
    return now or datetime.now(UTC)


def backoff_seconds(attempts: int) -> int:
    """Delay before attempt `attempts` + 1."""
    if attempts < 1:
        return BASE_BACKOFF_SECONDS
    return min(BASE_BACKOFF_SECONDS * 2 ** (attempts - 1), MAX_BACKOFF_SECONDS)


def lease_expiry(lease_seconds: int, *, now: datetime | None = None) -> datetime:
    return _now(now) + timedelta(seconds=lease_seconds)


def is_lease_expired(lease_expires_at: datetime | None, *, now: datetime | None = None) -> bool:
    """A missing lease counts as expired.

    A row marked `running` with no lease cannot be reasoned about, and
    leaving it stuck forever is worse than reclaiming it: the work is
    idempotent by construction, a permanently stranded job is not.
    """
    if lease_expires_at is None:
        return True
    return lease_expires_at <= _now(now)


def is_due(
    state: JobState, next_attempt_at: datetime | None, *, now: datetime | None = None
) -> bool:
    if state not in CLAIMABLE_STATES:
        return False
    return next_attempt_at is None or next_attempt_at <= _now(now)


def claim(
    state: JobState,
    attempts: int,
    *,
    lease_seconds: int,
    next_attempt_at: datetime | None = None,
    now: datetime | None = None,
) -> JobTransition | None:
    """Move a due job to `running`, or return None if it is not claimable.

    Returning None rather than raising: a worker racing the sweeper for the
    same row is expected, not exceptional.
    """
    if not is_due(state, next_attempt_at, now=now):
        return None
    return JobTransition(
        state=JobState.RUNNING,
        attempts=attempts + 1,
        next_attempt_at=None,
        lease_expires_at=lease_expiry(lease_seconds, now=now),
    )


def succeed(attempts: int) -> JobTransition:
    return JobTransition(state=JobState.COMPLETED, attempts=attempts)


def await_review(attempts: int) -> JobTransition:
    return JobTransition(state=JobState.WAITING_REVIEW, attempts=attempts)


def cancel(attempts: int) -> JobTransition:
    return JobTransition(state=JobState.CANCELLED, attempts=attempts)


def fail(
    attempts: int,
    error: str,
    *,
    error_class: ErrorClass,
    max_attempts: int,
    now: datetime | None = None,
) -> JobTransition:
    """Schedule a retry, or park the job durably for a human.

    A permanent error goes straight to `failed_review` without spending the
    remaining attempts: re-running a workflow over an input that cannot
    parse produces the same failure four more times, at four more times the
    cost.
    """
    truncated = error[:MAX_ERROR_LENGTH]
    exhausted = attempts >= max_attempts
    if error_class is ErrorClass.PERMANENT or exhausted:
        return JobTransition(state=JobState.FAILED_REVIEW, attempts=attempts, last_error=truncated)
    return JobTransition(
        state=JobState.RETRY_WAIT,
        attempts=attempts,
        next_attempt_at=_now(now) + timedelta(seconds=backoff_seconds(attempts)),
        last_error=truncated,
    )


def classify_error(exc: BaseException) -> ErrorClass:
    """Decide retryability from the exception itself.

    Matches on type name rather than importing every library's exception
    hierarchy, so a tool added later is classified sensibly without this
    module growing a dependency on it. Unknown failures are treated as
    permanent: retrying something we do not understand burns budget to
    reach the same place, and a job parked in `failed_review` is visible,
    whereas one quietly retrying five times is not.
    """
    if isinstance(exc, TimeoutError):
        return ErrorClass.RETRYABLE
    # An upstream that answered with a server error or a rate limit will
    # very likely answer differently in a minute. Classifying by exception
    # *name* alone missed these entirely, because the name of a client's
    # error type says nothing about the status it wrapped.
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return (
            ErrorClass.RETRYABLE if status >= 500 or status in {408, 429} else ErrorClass.PERMANENT
        )
    name = type(exc).__name__.lower()
    transient = (
        "timeout",
        "connect",
        "ratelimit",
        "serviceunavailable",
        "temporar",
        "unavailable",
        "remoteprotocol",
        "readerror",
        "writeerror",
        "poolerror",
    )
    if any(marker in name for marker in transient):
        return ErrorClass.RETRYABLE
    return ErrorClass.PERMANENT
