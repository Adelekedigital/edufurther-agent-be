"""The AI Router client.

The transport is faked, but nothing else is: a real RSA key pair is
generated per run and the token the client emits is verified the way the
router verifies it. A test that stubbed out signing would pass while the
service 401s in every environment, because the router's rejections are
deliberately indistinguishable generic 401s - iss, sub, aud, kid and scope
all have to be right and none of them tells you which one was wrong.
"""

import json
from datetime import UTC, datetime

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.integrations import ai_router as module
from app.integrations.ai_router import (
    AUDIENCE,
    ISSUER,
    SCOPE,
    SUBJECT,
    TOKEN_LIFETIME_SECONDS,
    AIRouterClient,
    AIRouterError,
    AIRouterOutcome,
    AIRouterRequest,
    AITask,
)

KEY_ID = "agent-key-1"


@pytest.fixture(scope="module")
def keypair() -> tuple[str, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public = (
        key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    return private, public


@pytest.fixture
def captured() -> dict:
    return {}


def make_client(private_key: str, monkeypatch, captured: dict, handler) -> AIRouterClient:
    """Inject a mock transport by wrapping the client class.

    Patching `execute` itself would skip the signing and serialization this
    test exists to check; wrapping the constructor keeps the real request
    path and only replaces the socket. Borrowed from Scholarship Finder's
    `test_ai_router_client.py`.
    """
    original = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original(*args, **kwargs)

    monkeypatch.setattr(module.httpx, "AsyncClient", patched)
    return AIRouterClient(
        base_url="https://router.test/",
        private_key_pem=private_key,
        key_id=KEY_ID,
        product_id="edufurther_agent",
    )


def ok_response(captured: dict, body: dict | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json=body
            or {
                "request_id": "req-1",
                "status": "completed",
                "output": {"page_type": "list", "evidence": []},
                "model_policy_version": "ai-policy-v1",
                "prompt_version": "classify_source_page-v1",
                "trace_reference": "https://langfuse.test/trace/abc",
            },
        )

    return handler


def sample_request() -> AIRouterRequest:
    return AIRouterRequest(
        task=AITask.classify_source_page,
        feature_id="scholarship_verification",
        correlation_id="agent_run_1",
        idempotency_key="classify:discovery-1",
        source_data={"page_text": "ten scholarships"},
    )


async def test_the_token_verifies_the_way_the_router_verifies_it(keypair, monkeypatch, captured):
    private, public = keypair
    client = make_client(private, monkeypatch, captured, ok_response(captured))

    await client.execute(sample_request())

    token = captured["request"].headers["Authorization"].removeprefix("Bearer ")
    assert jwt.get_unverified_header(token)["kid"] == KEY_ID
    claims = jwt.decode(
        token,
        public,
        algorithms=["RS256"],
        audience=AUDIENCE,
        options={"require": ["iss", "sub", "aud", "iat", "nbf", "exp", "jti"]},
    )
    assert claims["iss"] == ISSUER
    assert claims["sub"] == SUBJECT
    assert claims["scope"] == SCOPE


async def test_the_token_lifetime_stays_inside_the_routers_ceiling(keypair, monkeypatch, captured):
    """The router rejects anything over 300 seconds."""
    private, _ = keypair
    client = make_client(private, monkeypatch, captured, ok_response(captured))

    await client.execute(sample_request())

    token = captured["request"].headers["Authorization"].removeprefix("Bearer ")
    claims = jwt.decode(token, options={"verify_signature": False})
    lifetime = claims["exp"] - claims["iat"]
    assert lifetime == TOKEN_LIFETIME_SECONDS
    assert lifetime <= 300
    assert claims["exp"] > datetime.now(UTC).timestamp()


async def test_every_call_uses_a_fresh_jti(keypair, monkeypatch, captured):
    """The router claims each jti exactly once, so a reused token - on a
    retry, say - is a silent 401."""
    private, _ = keypair
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        token = request.headers["Authorization"].removeprefix("Bearer ")
        seen.append(jwt.decode(token, options={"verify_signature": False})["jti"])
        return httpx.Response(200, json={"request_id": "r", "status": "completed", "output": {}})

    client = make_client(private, monkeypatch, captured, handler)
    await client.execute(sample_request())
    await client.execute(sample_request())

    assert len(set(seen)) == 2


async def test_the_payload_matches_the_routers_closed_contract(keypair, monkeypatch, captured):
    """`ExecuteRequest` is extra="forbid": one unexpected key is a 422."""
    private, _ = keypair
    client = make_client(private, monkeypatch, captured, ok_response(captured))

    await client.execute(sample_request())

    assert set(captured["body"]) == {
        "product_id",
        "feature_id",
        "task",
        "correlation_id",
        "idempotency_key",
        "source_data",
    }
    assert captured["body"]["product_id"] == "edufurther_agent"
    assert captured["body"]["task"] == "classify_source_page"


async def test_the_idempotency_key_is_sent_in_both_places(keypair, monkeypatch, captured):
    """The router compares the header against the body field and 400s on a
    mismatch."""
    private, _ = keypair
    client = make_client(private, monkeypatch, captured, ok_response(captured))

    await client.execute(sample_request())

    assert captured["request"].headers["Idempotency-Key"] == "classify:discovery-1"
    assert captured["body"]["idempotency_key"] == "classify:discovery-1"


async def test_the_correlation_id_is_sent_as_the_request_id_header(keypair, monkeypatch, captured):
    """That header seeds the router's Langfuse trace id and is the only
    caller-controlled input to it - without it a run's traces are not
    findable from the job."""
    private, _ = keypair
    client = make_client(private, monkeypatch, captured, ok_response(captured))

    await client.execute(sample_request())

    assert captured["request"].headers["X-Request-ID"] == "agent_run_1"


async def test_the_base_url_is_joined_without_a_double_slash(keypair, monkeypatch, captured):
    private, _ = keypair
    client = make_client(private, monkeypatch, captured, ok_response(captured))

    await client.execute(sample_request())

    assert str(captured["request"].url) == "https://router.test/api/v1/internal/ai/execute"


async def test_a_completed_response_is_mapped_in_full(keypair, monkeypatch, captured):
    private, _ = keypair
    client = make_client(private, monkeypatch, captured, ok_response(captured))

    response = await client.execute(sample_request())

    assert response.completed
    assert response.outcome is AIRouterOutcome.completed
    assert response.output == {"page_type": "list", "evidence": []}
    assert response.prompt_version == "classify_source_page-v1"
    assert response.model_policy_version == "ai-policy-v1"
    assert response.trace_reference == "https://langfuse.test/trace/abc"


@pytest.mark.parametrize("status", ["review", "budget_exhausted", "provider_unavailable"])
async def test_a_non_completed_outcome_is_returned_not_raised(
    keypair, monkeypatch, captured, status
):
    """These are answers, not failures. The router returns them with HTTP
    200 and a null output, and the caller decides what each one means for
    its workflow - retrying a budget_exhausted would just spend the next
    budget."""
    private, _ = keypair
    client = make_client(
        private,
        monkeypatch,
        captured,
        ok_response(captured, {"request_id": "r", "status": status, "output": None}),
    )

    response = await client.execute(sample_request())

    assert not response.completed
    assert response.outcome.value == status


@pytest.mark.parametrize("code", [400, 401, 403, 409, 422, 429, 500, 503])
async def test_an_error_status_raises_with_the_routers_code(keypair, monkeypatch, captured, code):
    private, _ = keypair

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(code, json={"code": "TASK_NOT_ALLOWED"})

    client = make_client(private, monkeypatch, captured, handler)

    with pytest.raises(AIRouterError, match="TASK_NOT_ALLOWED"):
        await client.execute(sample_request())


async def test_an_unrecognised_status_raises_rather_than_being_guessed(
    keypair, monkeypatch, captured
):
    private, _ = keypair
    client = make_client(
        private,
        monkeypatch,
        captured,
        ok_response(captured, {"request_id": "r", "status": "something_new", "output": {}}),
    )

    with pytest.raises(AIRouterError, match="unusable status"):
        await client.execute(sample_request())


async def test_a_non_object_body_raises(keypair, monkeypatch, captured):
    private, _ = keypair

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=["not", "an", "object"])

    client = make_client(private, monkeypatch, captured, handler)

    with pytest.raises(AIRouterError, match="non-object"):
        await client.execute(sample_request())


def test_every_task_this_client_knows_is_one_the_router_registers():
    """These strings are an allowlist shared across two repositories. A
    typo here is a 422 at the router, discovered at runtime."""
    assert {task.value for task in AITask} == {
        "classify_source_page",
        "split_list_candidates",
        "extract_scholarship_facts",
        "compare_official_evidence",
        "extract_eligibility_requirements",
    }


def test_the_caller_identity_matches_the_registered_service_caller():
    """subject, issuer and audience are checked individually by the router
    and a mismatch in any one is an indistinguishable generic 401."""
    assert SUBJECT == "edufurther-agent-worker"
    assert ISSUER == "edufurther-agent"
    assert AUDIENCE == "edufurther-ai-router"
    assert SCOPE == "ai:execute"


async def test_each_call_sends_its_own_request_id(keypair, monkeypatch, captured):
    """The router binds one X-Request-ID to one idempotency key, so sending
    the job's correlation id for every step made every model call after the
    first a 409 REQUEST_ID_COLLISION - which only surfaced once a workflow
    ran past classification for the first time."""
    private, _ = keypair
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["X-Request-ID"])
        return ok_response(captured)(request)

    client = make_client(private, monkeypatch, captured, handler)
    base = sample_request()
    for step in ("classify", "facts:0"):
        # Rebuilt rather than mutated: the request is frozen, which is what
        # stops a retry quietly rewriting the key it is meant to replay.
        await client.execute(
            AIRouterRequest(
                task=base.task,
                feature_id=base.feature_id,
                correlation_id=base.correlation_id,
                idempotency_key=f"job-1:{step}",
                source_data=base.source_data,
                request_id=f"{base.correlation_id}:{step}",
            )
        )

    assert len(set(seen)) == 2, f"two calls in one run shared a request id: {seen}"
    assert all(s.startswith(sample_request().correlation_id) for s in seen)


async def test_the_request_id_falls_back_to_the_correlation_id(keypair, monkeypatch, captured):
    """Callers making a single request per run need not supply one."""
    private, _ = keypair
    client = make_client(private, monkeypatch, captured, ok_response(captured))

    await client.execute(sample_request())

    assert captured["request"].headers["X-Request-ID"] == sample_request().correlation_id
