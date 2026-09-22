"""Tool call telemetry.

Every external call a job makes should be answerable afterwards: which
tool, against what, how long, and how it ended. Without it, "why did this
candidate have no official source" is unanswerable once the run is over.
"""

import uuid

import pytest
from sqlalchemy import select

from app.core.identifiers import job_idempotency_key, new_correlation_id
from app.infra.database import get_sessionmaker
from app.infra.jobs import enqueue_job
from app.infra.models import AgentToolCall
from app.tools.base import (
    ToolBudgetExhausted,
    ToolDisabled,
    ToolStatus,
    observe,
    persist_tool_call,
)
from tests.conftest import requires_db

pytestmark = requires_db


async def make_job() -> uuid.UUID:
    reference = f"tool-{uuid.uuid4()}"
    async with get_sessionmaker()() as session:
        job, _ = await enqueue_job(
            session,
            product_id="scholarship_finder",
            use_case_id="probe",
            input_reference=reference,
            correlation_id=new_correlation_id(),
            idempotency_key=job_idempotency_key(
                use_case="probe", workflow_version="v1", input_reference=reference
            ),
            workflow_version="v1",
        )
        job_id = job.job_id
        await session.commit()
    return job_id


async def test_a_successful_call_is_timed_and_marked_ok():
    async with observe("direct_fetch", target="https://example.test/a") as record:
        record.response_meta = {"status_code": 200}

    assert record.status == ToolStatus.OK
    assert record.error is None
    assert record.duration_ms >= 0


@pytest.mark.parametrize(
    ("raised", "expected"),
    [
        (ToolDisabled("off"), ToolStatus.DISABLED),
        (ToolBudgetExhausted("spent"), ToolStatus.BUDGET_EXHAUSTED),
        (ValueError("Source URL domain is not approved"), ToolStatus.REFUSED),
        (RuntimeError("something else"), ToolStatus.ERROR),
    ],
)
async def test_each_failure_kind_is_classified_distinctly(raised, expected):
    """The taxonomy is the point: a policy refusal and a timeout call for
    opposite responses, and one status for both would hide that."""
    with pytest.raises(type(raised)):
        async with observe("direct_fetch") as record:
            raise raised

    assert record.status == expected


async def test_failures_are_re_raised_not_swallowed():
    """`observe` records what happened; it does not decide what to do."""
    with pytest.raises(ValueError):
        async with observe("direct_fetch"):
            raise ValueError("not approved")


async def test_a_call_is_written_to_the_job_audit_trail():
    job_id = await make_job()

    async with observe("direct_fetch", target="https://example.test/a") as record:
        record.response_meta = {"status_code": 200, "byte_length": 1234}

    async with get_sessionmaker()() as session:
        await persist_tool_call(session, job_id=job_id, record=record, attempt_number=1)
        await session.commit()

    async with get_sessionmaker()() as session:
        row = await session.scalar(select(AgentToolCall).where(AgentToolCall.job_id == job_id))

    assert row is not None
    assert row.tool == "direct_fetch"
    assert row.target == "https://example.test/a"
    assert row.status == ToolStatus.OK
    assert row.response_meta == {"status_code": 200, "byte_length": 1234}
    assert row.attempt_number == 1


async def test_a_failed_call_records_its_error():
    job_id = await make_job()

    with pytest.raises(ValueError):
        async with observe("direct_fetch", target="https://attacker.test/a") as record:
            raise ValueError("Source URL domain is not approved")

    async with get_sessionmaker()() as session:
        await persist_tool_call(session, job_id=job_id, record=record)
        await session.commit()

    async with get_sessionmaker()() as session:
        row = await session.scalar(select(AgentToolCall).where(AgentToolCall.job_id == job_id))

    assert row is not None
    assert row.status == ToolStatus.REFUSED
    assert "not approved" in row.error


async def test_a_very_long_error_is_truncated_rather_than_rejected():
    job_id = await make_job()

    with pytest.raises(RuntimeError):
        async with observe("direct_fetch") as record:
            raise RuntimeError("x" * 10_000)

    async with get_sessionmaker()() as session:
        await persist_tool_call(session, job_id=job_id, record=record)
        await session.commit()

    async with get_sessionmaker()() as session:
        row = await session.scalar(select(AgentToolCall).where(AgentToolCall.job_id == job_id))

    assert row is not None and len(row.error) == 2000


async def test_recorded_writes_the_call_without_a_separate_step():
    """`observe` alone only logs. Nothing was writing to agent_tool_calls
    at all until this wrapper existed, so the table stayed empty and the
    metrics endpoint's tool breakdown was always {}."""
    from app.tools.base import recorded

    job_id = await make_job()

    async with get_sessionmaker()() as session:
        async with recorded(
            session, "direct_fetch", target="https://example.test/a", job_id=job_id
        ) as record:
            record.response_meta = {"status_code": 200}

    async with get_sessionmaker()() as session:
        row = await session.scalar(select(AgentToolCall).where(AgentToolCall.job_id == job_id))

    assert row is not None
    assert row.tool == "direct_fetch"
    assert row.status == ToolStatus.OK


async def test_a_refused_call_is_recorded_too():
    """The ones worth having. A run that reached no official page should
    be explainable afterwards as "the guard refused it"."""
    from app.tools.base import recorded

    job_id = await make_job()

    with pytest.raises(ValueError):
        async with get_sessionmaker()() as session:
            async with recorded(session, "direct_fetch", job_id=job_id):
                raise ValueError("Source URL domain is not approved")

    async with get_sessionmaker()() as session:
        row = await session.scalar(select(AgentToolCall).where(AgentToolCall.job_id == job_id))

    assert row is not None
    assert row.status == ToolStatus.REFUSED


async def test_a_telemetry_failure_never_breaks_the_tool_call():
    """Losing the record of a fetch is bad; losing the fetch because
    recording it failed is worse."""
    from app.tools.base import recorded

    async with get_sessionmaker()() as session:
        # No such job, so the insert violates its foreign key.
        async with recorded(session, "direct_fetch", job_id=uuid.uuid4()) as record:
            record.response_meta = {"status_code": 200}

    assert record.status == ToolStatus.OK


async def test_retrieval_records_its_tool_call(monkeypatch):
    """The end-to-end wiring: a real retrieval call lands in the job's
    audit trail without the caller doing anything extra."""
    import httpx

    from app.core.config import get_settings
    from app.tools import direct_fetch, registry
    from app.tools.retrieval import fetch_page

    monkeypatch.setattr(get_settings(), "disabled_tools", set())
    monkeypatch.setattr(get_settings(), "jina_api_key", None)
    monkeypatch.setattr(direct_fetch, "_is_public_host", lambda hostname: True)
    original = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(
            lambda request: httpx.Response(200, text="a page")
        )
        return original(*args, **kwargs)

    monkeypatch.setattr(direct_fetch.httpx, "AsyncClient", patched)
    job_id = await make_job()

    async with get_sessionmaker()() as session:
        await fetch_page(
            session, "https://example.test/award", ["example.test"], job_id=str(job_id)
        )

    async with get_sessionmaker()() as session:
        row = await session.scalar(select(AgentToolCall).where(AgentToolCall.job_id == job_id))

    assert row is not None
    assert row.tool == "fetch_page"
    assert row.status == ToolStatus.OK
    assert row.response_meta["fetch_method"] == "direct"
    assert registry.DIRECT_FETCH in registry.KNOWN_TOOLS
