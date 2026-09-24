"""Client for `edufurtherai-be`.

Every model call this service makes goes through here. It holds no provider
keys and constructs no prompts: the router owns model selection, fallback,
budgets, prompts and output validation, and this service owns deciding what
to ask and what to do with the answer.

The signing half is ported from Scholarship Finder's `ai_router_client.py` -
it is already proved against this exact verifier, and reimplementing a JWT
flow to save reading one file is how a subtle claim mismatch gets shipped.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

import httpx
import jwt

logger = logging.getLogger("app.integrations.ai_router")

EXECUTE_PATH = "/api/v1/internal/ai/execute"

#: Must match this service's entry in the router's SERVICE_CALLERS. The
#: router checks sub, iss and aud individually; a mismatch in any one of
#: them is an indistinguishable generic 401, so these are not cosmetic.
SUBJECT = "edufurther-agent-worker"
ISSUER = "edufurther-agent"
AUDIENCE = "edufurther-ai-router"
SCOPE = "ai:execute"

#: The router rejects a token whose lifetime exceeds 300s, and rejects any
#: reused jti. Short and single-use: a leaked token is worth almost nothing.
TOKEN_LIFETIME_SECONDS = 120


class AITask(StrEnum):
    """Tasks registered to this product in the router.

    An allowlist, not a convenience: sending an unregistered task is a 422
    at the router before any policy runs, and a task registered to another
    product is a 403. Both are better caught here.
    """

    classify_source_page = "classify_source_page"
    split_list_candidates = "split_list_candidates"
    extract_scholarship_facts = "extract_scholarship_facts"
    compare_official_evidence = "compare_official_evidence"
    extract_eligibility_requirements = "extract_eligibility_requirements"


class AIRouterOutcome(StrEnum):
    completed = "completed"
    #: The model answered but the answer did not match the task's declared
    #: shape. Terminal and means "a human should look", never "retry".
    review = "review"
    budget_exhausted = "budget_exhausted"
    provider_unavailable = "provider_unavailable"


@dataclass(frozen=True)
class AIRouterRequest:
    task: AITask
    feature_id: str
    correlation_id: str
    #: Deterministic per logical unit of work. A retried node must not
    #: double-spend the shared budget, and the router replays the stored
    #: response for a repeated key rather than calling a model again.
    idempotency_key: str
    source_data: dict[str, Any]
    #: Seeds the router's Langfuse trace. Must be unique per call: the
    #: router rejects a repeat carrying a different idempotency key with
    #: 409 REQUEST_ID_COLLISION. Defaults to the correlation id for callers
    #: that make a single request per run.
    request_id: str | None = None


@dataclass(frozen=True)
class AIRouterResponse:
    request_id: str
    outcome: AIRouterOutcome
    output: dict[str, Any] | None
    model_policy_version: str | None
    #: Persisted alongside any fact derived from this call, so a later
    #: accuracy regression can be traced to the prompt that produced it.
    prompt_version: str | None
    #: Which model actually answered, after any fallback inside the router.
    #: Not derivable from `model_policy_version`, which names the routing
    #: policy rather than the model it selected - so a policy that falls
    #: back reports the same version for two different models. Dropping
    #: this field is what left `model` NULL in every row of both databases
    #: through the whole Stage 1 pilot.
    model: str | None
    trace_reference: str | None

    @property
    def completed(self) -> bool:
        return self.outcome is AIRouterOutcome.completed


class AIRouterError(RuntimeError):
    """A call that produced no usable answer.

    Carries the HTTP status when there was one, so the runtime can tell a
    503 from a 422. Without it every failure classified as permanent and a
    momentary outage parked every in-flight job in `failed_review` - the
    exact case the backoff curve exists for.
    """

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class AIRouterNotConfigured(AIRouterError):
    """Base URL, key or key id missing."""


class AIRouterClient:
    def __init__(
        self,
        *,
        base_url: str,
        private_key_pem: str,
        key_id: str,
        product_id: str,
        timeout_seconds: float = 45.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.private_key_pem = private_key_pem
        self.key_id = key_id
        self.product_id = product_id
        self.timeout_seconds = timeout_seconds

    def _sign_jwt(self) -> str:
        now = datetime.now(UTC)
        claims = {
            "iss": ISSUER,
            "sub": SUBJECT,
            "aud": AUDIENCE,
            "iat": now,
            "nbf": now,
            "exp": now + timedelta(seconds=TOKEN_LIFETIME_SECONDS),
            # Fresh on every call, including retries: the router claims each
            # jti exactly once, so reusing a token silently 401s.
            "jti": str(uuid.uuid4()),
            "scope": SCOPE,
        }
        return jwt.encode(
            claims,
            self.private_key_pem,
            algorithm="RS256",
            headers={"kid": self.key_id},
        )

    async def execute(self, request: AIRouterRequest) -> AIRouterResponse:
        """Run one task. Raises on transport or protocol failure.

        Deliberately not fail-soft, unlike Scholarship Finder's client. For
        that service an AI pass is an optional enrichment on top of
        deterministic extraction, so swallowing a failure leaves a usable
        result. Here the model call *is* the work: swallowing it would
        produce a candidate with no facts and no explanation of why, which
        is worse than a job that retries or parks visibly.
        """
        payload = {
            "product_id": self.product_id,
            "feature_id": request.feature_id,
            "task": request.task.value,
            "correlation_id": request.correlation_id,
            "idempotency_key": request.idempotency_key,
            "source_data": request.source_data,
        }
        headers = {
            "Authorization": f"Bearer {self._sign_jwt()}",
            "Idempotency-Key": request.idempotency_key,
            # The router seeds its Langfuse trace id from this header, and
            # it is the only caller-controlled input to that id. Sending the
            # correlation id makes a run's traces findable from a job.
            "X-Request-ID": request.request_id or request.correlation_id,
        }
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.post(
                f"{self.base_url}{EXECUTE_PATH}", json=payload, headers=headers
            )
        if response.status_code >= 400:
            raise AIRouterError(
                f"ai router returned {response.status_code} for {request.task.value}: "
                f"{_problem_code(response)}",
                status_code=response.status_code,
            )
        body = response.json()
        if not isinstance(body, dict):
            raise AIRouterError("ai router returned a non-object body")
        try:
            outcome = AIRouterOutcome(body["status"])
        except (KeyError, ValueError) as exc:
            raise AIRouterError(f"ai router returned an unusable status: {body!r}") from exc
        return AIRouterResponse(
            request_id=str(body.get("request_id", "")),
            outcome=outcome,
            output=body.get("output"),
            model_policy_version=body.get("model_policy_version"),
            prompt_version=body.get("prompt_version"),
            model=body.get("model"),
            trace_reference=body.get("trace_reference"),
        )


def _problem_code(response: httpx.Response) -> str:
    """The router's machine-readable error code, when it sent one.

    Its errors are problem+json with a stable `code`; surfacing that rather
    than a body excerpt keeps a failure legible in a log without dragging
    the request payload along with it.
    """
    try:
        body = response.json()
        if isinstance(body, dict):
            return str(body.get("code") or body.get("detail") or "")
    except Exception:
        pass
    return ""


def client_from_settings() -> AIRouterClient:
    from app.core.config import get_settings

    settings = get_settings()
    if not settings.ai_router_configured:
        raise AIRouterNotConfigured(
            "AI_ROUTER_BASE_URL, AI_ROUTER_PRIVATE_KEY_PEM and AI_ROUTER_KEY_ID are required"
        )
    assert settings.ai_router_base_url and settings.ai_router_private_key_pem
    assert settings.ai_router_key_id
    return AIRouterClient(
        base_url=settings.ai_router_base_url,
        private_key_pem=settings.ai_router_private_key_pem,
        key_id=settings.ai_router_key_id,
        product_id=settings.ai_router_product_id,
        timeout_seconds=settings.ai_router_timeout_seconds,
    )
