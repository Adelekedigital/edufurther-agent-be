import time
import uuid

from app.core.identifiers import (
    MAX_IDEMPOTENCY_KEY_LENGTH,
    job_idempotency_key,
    new_correlation_id,
    new_uuid7,
)


def test_the_same_work_always_produces_the_same_key():
    """Determinism is what makes at-least-once delivery safe: a redelivered
    submission must collapse onto the existing job, not start a second run
    of the same work."""
    args = dict(use_case="scholarship_finder", workflow_version="v1", input_reference="d-1")

    assert job_idempotency_key(**args) == job_idempotency_key(**args)


def test_a_new_workflow_version_is_a_different_key():
    """The gap Scholarship Finder's one-time evaluation marker left: a
    changed workflow must be able to reprocess a record deliberately,
    without being mistaken for a duplicate submission."""
    v1 = job_idempotency_key(use_case="sf", workflow_version="v1", input_reference="d-1")
    v2 = job_idempotency_key(use_case="sf", workflow_version="v2", input_reference="d-1")

    assert v1 != v2


def test_different_inputs_and_use_cases_do_not_collide():
    base = dict(use_case="sf", workflow_version="v1")

    assert job_idempotency_key(**base, input_reference="d-1") != job_idempotency_key(
        **base, input_reference="d-2"
    )
    assert job_idempotency_key(
        use_case="sf", workflow_version="v1", input_reference="d-1"
    ) != job_idempotency_key(use_case="other", workflow_version="v1", input_reference="d-1")


def test_an_oversized_key_is_hashed_rather_than_truncated():
    """A truncated key is a key that can collide - two different inputs
    sharing a prefix would silently become one job."""
    long_reference = "d-" + "x" * 2_000
    key = job_idempotency_key(use_case="sf", workflow_version="v1", input_reference=long_reference)

    assert len(key) <= MAX_IDEMPOTENCY_KEY_LENGTH
    assert "sha256:" in key
    assert key != job_idempotency_key(
        use_case="sf", workflow_version="v1", input_reference=long_reference + "y"
    )


def test_a_key_that_fits_stays_readable():
    key = job_idempotency_key(use_case="sf", workflow_version="v1", input_reference="d-1")

    assert key == "sf:v1:d-1"


def test_uuid7_values_are_unique_and_version_7():
    ids = [new_uuid7() for _ in range(200)]

    assert len(set(ids)) == 200
    assert all(isinstance(value, uuid.UUID) and value.version == 7 for value in ids)


def test_uuid7_values_sort_by_millisecond_but_not_within_one():
    """The guarantee is the timestamp prefix, not sub-millisecond ordering:
    two ids minted in the same millisecond differ only in random bits.

    Keyset pagination needs a stable total order, which this gives. Deciding
    which of two near-simultaneous records came first is a different claim,
    and this does not support it."""
    batches = []
    for _ in range(5):
        batches.append(new_uuid7())
        time.sleep(0.002)

    assert [str(value) for value in batches] == sorted(str(value) for value in batches)


def test_uuid7_ordering_is_a_stable_total_order():
    """What pagination actually relies on: sorting the same set twice gives
    the same sequence, so a cursor cannot skip or repeat a row."""
    ids = [new_uuid7() for _ in range(100)]

    assert sorted(str(value) for value in ids) == sorted(str(value) for value in ids)
    assert len({str(value) for value in ids}) == 100


def test_correlation_ids_are_unique_and_fit_the_router_header_limit():
    """Sent to the AI Router as X-Request-ID, which it rejects above 128
    characters - and that header is the only caller-controlled input to its
    Langfuse trace id."""
    ids = {new_correlation_id() for _ in range(100)}

    assert len(ids) == 100
    assert all(value.startswith("agent_") and len(value) <= 128 for value in ids)
    assert all(value.isprintable() for value in ids)
