from tests.conftest import requires_db


async def test_health_is_unauthenticated_and_touches_nothing(client):
    """Liveness must not check the database: restarting the process when
    the database blips is precisely when restarting helps least."""
    response = await client.get("/health")

    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "ok"
    assert body["service"] == "edufurther-agent"


async def test_every_response_carries_a_request_id(client):
    response = await client.get("/health")

    assert response.headers["X-Request-ID"]


async def test_a_caller_supplied_request_id_is_echoed(client):
    """The AI Router derives its Langfuse trace id from this header, so a
    run's traces are only findable if the id survives the hop."""
    response = await client.get("/health", headers={"X-Request-ID": "agent_run_42"})

    assert response.headers["X-Request-ID"] == "agent_run_42"


async def test_an_oversized_request_id_is_replaced_not_echoed(client):
    """An id from a header reaches logs and downstream requests, so it must
    not be able to carry unbounded length or control characters."""
    response = await client.get("/health", headers={"X-Request-ID": "x" * 500})

    assert response.headers["X-Request-ID"].startswith("req_")


@requires_db
async def test_ready_reports_schema_migration_state(client):
    response = await client.get("/ready")

    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "ready"
    assert body["migration"]["applied"] == body["migration"]["expected"]
    assert body["migration"]["up_to_date"] is True
