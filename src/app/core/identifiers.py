import hashlib
import time
import uuid

#: Keep in step with the ProcessingJob.idempotency_key column width.
MAX_IDEMPOTENCY_KEY_LENGTH = 500


def new_uuid7() -> uuid.UUID:
    """Return UUIDv7 using the standard library or a compatible fallback.

    Ordering matters here - keyset pagination over jobs orders by id - but
    be precise about what is guaranteed: UUIDv7 sorts by its millisecond
    timestamp prefix, and two ids minted inside the same millisecond are
    ordered by their random bits, which is to say arbitrarily. That is
    enough for pagination, which needs a stable total order rather than a
    chronological one, and it is not enough to decide which of two
    near-simultaneous records came first.

    `uuid.uuid7` arrives in Python 3.14; on 3.13 the fallback below is
    always what runs. Ported from Scholarship Finder so ids sort identically
    across services.
    """
    uuid7 = getattr(uuid, "uuid7", None)
    if uuid7 is not None:
        return uuid7()
    timestamp_ms = int(time.time() * 1000) & ((1 << 48) - 1)
    random_bits = uuid.uuid4().int
    value = (
        (timestamp_ms << 80)
        | (0x7 << 76)
        | ((random_bits >> 62) & 0xFFF) << 64
        | (0x2 << 62)
        | (random_bits & ((1 << 62) - 1))
    )
    return uuid.UUID(int=value)


def job_idempotency_key(*, use_case: str, workflow_version: str, input_reference: str) -> str:
    """A job key that is stable across retries and distinct across versions.

    Deterministic so a redelivered submission collapses onto the existing
    job instead of starting a second run of the same work. Versioned so that
    changing the workflow is a deliberate, separate run rather than a
    silently suppressed duplicate - the failure mode Scholarship Finder hit
    with a one-time evaluation marker.

    Readable while it fits, hashed when it would not: the column is bounded,
    and a truncated key is a key that can collide.
    """
    key = f"{use_case}:{workflow_version}:{input_reference}"
    if len(key) <= MAX_IDEMPOTENCY_KEY_LENGTH:
        return key
    digest = hashlib.sha256(key.encode()).hexdigest()
    return f"{use_case}:{workflow_version}:sha256:{digest}"


def new_correlation_id() -> str:
    """A correlation id for one logical unit of work across services.

    Sent to the AI Router as `X-Request-ID`, which is the only
    caller-controlled input to its Langfuse trace id - so a deterministic
    value here is what makes a run's traces findable later.
    """
    return f"agent_{new_uuid7().hex}"
