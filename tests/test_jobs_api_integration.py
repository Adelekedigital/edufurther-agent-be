"""The job submission surface, over real HTTP against a real database."""

import pytest

from tests.conftest import requires_db

pytestmark = requires_db


async def submission(**overrides) -> dict:
    body = {
        "product_id": "scholarship_finder",
        "use_case_id": "probe",
        "input_reference": "discovery-1",
        "payload": {"source_url": "https://example.org/awards"},
    }
    return body | overrides


async def test_submission_requires_the_service_token(client):
    response = await client.post("/api/v1/internal/agent/jobs", json=await submission())

    assert response.status_code == 401


async def test_a_wrong_token_is_rejected(client):
    response = await client.post(
        "/api/v1/internal/agent/jobs",
        json=await submission(),
        headers={"X-Service-Token": "not-the-token"},
    )

    assert response.status_code == 401


async def test_submitting_work_is_accepted_without_running_it(client, auth_headers):
    """202, not 200: a run fetches several pages and makes several model
    calls, which is far longer than an HTTP request should be held open."""
    response = await client.post(
        "/api/v1/internal/agent/jobs", json=await submission(), headers=auth_headers
    )

    body = response.json()
    assert response.status_code == 202
    assert body["state"] == "queued"
    assert body["created"] is True
    assert body["workflow_version"] == "scholarship-verification-v1"
    assert body["correlation_id"].startswith("agent_")
    assert response.headers["Location"].endswith(body["job_id"])


async def test_resubmitting_the_same_work_returns_the_same_job(client, auth_headers):
    """At-least-once delivery must not mean at-least-once execution."""
    first = await client.post(
        "/api/v1/internal/agent/jobs", json=await submission(), headers=auth_headers
    )
    second = await client.post(
        "/api/v1/internal/agent/jobs", json=await submission(), headers=auth_headers
    )

    assert first.status_code == second.status_code == 202
    assert first.json()["job_id"] == second.json()["job_id"]
    assert first.json()["created"] is True
    assert second.json()["created"] is False


async def test_a_new_workflow_version_is_a_separate_run(client, auth_headers):
    """A changed workflow has to be able to reprocess a record deliberately
    rather than being suppressed as a duplicate."""
    first = await client.post(
        "/api/v1/internal/agent/jobs", json=await submission(), headers=auth_headers
    )
    second = await client.post(
        "/api/v1/internal/agent/jobs",
        json=await submission(workflow_version="scholarship-verification-v2"),
        headers=auth_headers,
    )

    assert first.json()["job_id"] != second.json()["job_id"]
    assert second.json()["created"] is True


async def test_a_supplied_correlation_id_is_preserved(client, auth_headers):
    response = await client.post(
        "/api/v1/internal/agent/jobs",
        json=await submission(correlation_id="sf-run-99"),
        headers=auth_headers,
    )

    assert response.json()["correlation_id"] == "sf-run-99"


async def test_an_unknown_field_is_rejected_rather_than_ignored(client, auth_headers):
    """Silently dropping a field the caller thought mattered is worse than
    telling them it is not part of the contract."""
    response = await client.post(
        "/api/v1/internal/agent/jobs",
        json=await submission(priority="urgent"),
        headers=auth_headers,
    )

    assert response.status_code == 422


@pytest.mark.parametrize("missing", ["product_id", "use_case_id", "input_reference"])
async def test_required_fields_are_enforced(client, auth_headers, missing):
    body = await submission()
    body.pop(missing)

    response = await client.post("/api/v1/internal/agent/jobs", json=body, headers=auth_headers)

    assert response.status_code == 422


async def test_job_status_reports_the_stored_record(client, auth_headers):
    created = await client.post(
        "/api/v1/internal/agent/jobs", json=await submission(), headers=auth_headers
    )
    job_id = created.json()["job_id"]

    response = await client.get(f"/api/v1/internal/agent/jobs/{job_id}", headers=auth_headers)

    body = response.json()
    assert response.status_code == 200
    assert body["job_id"] == job_id
    assert body["state"] == "queued"
    assert body["attempts"] == 0
    assert body["input_reference"] == "discovery-1"
    assert body["last_error"] is None


async def test_reading_an_unknown_job_is_a_problem_response(client, auth_headers):
    response = await client.get(
        "/api/v1/internal/agent/jobs/00000000-0000-0000-0000-000000000000",
        headers=auth_headers,
    )

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "JOB_NOT_FOUND"


async def test_reading_a_job_requires_the_service_token(client):
    response = await client.get("/api/v1/internal/agent/jobs/00000000-0000-0000-0000-000000000000")

    assert response.status_code == 401


async def test_an_unknown_use_case_is_rejected_rather_than_queued(client, auth_headers):
    """A job for a workflow this build cannot run is guaranteed to fail.
    Saying so now gives the caller something to act on; queueing it gives
    them a job that dies later for reasons they cannot see."""
    response = await client.post(
        "/api/v1/internal/agent/jobs",
        json=await submission(use_case_id="not_a_registered_workflow"),
        headers=auth_headers,
    )

    assert response.status_code == 422
    assert response.json()["code"] == "UNKNOWN_USE_CASE"
    # The error names what this build does implement, so a caller with a
    # typo can see it rather than guessing.
    assert "probe" in response.json()["detail"]
    assert "scholarship_verification" in response.json()["detail"]


async def test_a_new_job_is_handed_to_a_worker(client, auth_headers, spawned):
    await client.post("/api/v1/internal/agent/jobs", json=await submission(), headers=auth_headers)

    assert len(spawned) == 1


async def test_a_redelivery_does_not_start_a_second_worker(client, auth_headers, spawned):
    """Claiming is atomic, so a second worker would be harmless - but it
    would still be a duplicate run of work already in flight."""
    await client.post("/api/v1/internal/agent/jobs", json=await submission(), headers=auth_headers)
    await client.post("/api/v1/internal/agent/jobs", json=await submission(), headers=auth_headers)

    assert len(spawned) == 1


async def test_run_due_reports_an_empty_queue(client, auth_headers):
    response = await client.post("/api/v1/internal/agent/jobs/run-due", headers=auth_headers)

    assert response.status_code == 200
    assert response.json() == {
        "reclaimed": 0,
        "picked_up": 0,
        "completed": 0,
        "failed": 0,
        "remaining": 0,
    }


async def test_run_due_requires_the_service_token(client):
    response = await client.post("/api/v1/internal/agent/jobs/run-due")

    assert response.status_code == 401


@pytest.mark.parametrize("token", ["tokén-with-accents", "😀", "ÿ" * 8])
async def test_a_non_ascii_token_is_refused_rather_than_raising(token):
    """Starlette decodes headers as latin-1, so a byte >= 0x80 yields a
    non-ASCII str - and `hmac.compare_digest` raises TypeError on one,
    which was an unhandled 500 from an unauthenticated request on every
    route behind this dependency.

    Exercised against the dependency directly because httpx refuses to
    *send* a non-ASCII header value, so a client-driven test cannot reach
    the server at all.
    """
    from fastapi import HTTPException

    from app.core.security import require_agent_service

    with pytest.raises(HTTPException) as caught:
        await require_agent_service(token)

    assert caught.value.status_code == 401


async def test_the_correct_token_still_authenticates():
    from app.core.security import require_agent_service
    from tests.conftest import TEST_SERVICE_TOKEN

    assert await require_agent_service(TEST_SERVICE_TOKEN) is None
