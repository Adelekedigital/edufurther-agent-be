"""Client for `edufurtherSF-BE`.

The only way this service reaches the product. There is no database
connection to it and there must never be: the product owns discovery
lifecycle, canonical identity, review state and publication, and a second
writer to those tables would be a second source of truth with no way to
resolve a disagreement.

The credential here is scoped to `/internal/agent/*`. It cannot decide a
review, create a scholarship, publish a cycle or withdraw one - the product
enforces that with a separate token, so the boundary does not rest on this
client being well behaved.
"""

import logging
from typing import Any

import httpx

logger = logging.getLogger("app.integrations.scholarship_finder")

AGENT_BASE = "/api/v1/internal/agent"


class ScholarshipFinderError(RuntimeError):
    """A call that produced no usable answer.

    Carries the HTTP status when there was one, so a deploy blip (503) is
    distinguishable from a rejected request (422).
    """

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class ScholarshipFinderNotConfigured(ScholarshipFinderError):
    """Base URL or token missing."""


class DiscoveryNotFound(ScholarshipFinderError):
    """The product does not have this discovery.

    Distinct from a transport failure on purpose: a missing discovery
    cannot be fixed by retrying, and classifying it as retryable would
    spend five attempts reaching the same answer.
    """


class ScholarshipFinderClient:
    def __init__(self, *, base_url: str, token: str, timeout_seconds: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout_seconds = timeout_seconds

    @property
    def _headers(self) -> dict[str, str]:
        return {"X-Service-Token": self.token}

    async def get_discovery(self, discovery_id: str) -> dict[str, Any]:
        """Read one discovery, with the lineage the review queue omits."""
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.get(
                f"{self.base_url}{AGENT_BASE}/discoveries/{discovery_id}",
                headers=self._headers,
            )
        if response.status_code == 404:
            raise DiscoveryNotFound(f"no such discovery: {discovery_id}")
        return _json_object(response, f"get_discovery({discovery_id})")

    async def list_discoveries(
        self,
        *,
        limit: int = 50,
        after: str | None = None,
        workflow_version: str | None = None,
        unprocessed_only: bool = True,
    ) -> dict[str, Any]:
        """A page of work.

        `workflow_version` excludes what this version has already run, so
        bumping it deliberately makes records eligible again rather than
        having them look like duplicates.
        """
        params: dict[str, Any] = {"limit": limit, "unprocessed_only": unprocessed_only}
        if after:
            params["after"] = after
        if workflow_version:
            params["workflow_version"] = workflow_version
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.get(
                f"{self.base_url}{AGENT_BASE}/discoveries",
                headers=self._headers,
                params=params,
            )
        return _json_object(response, "list_discoveries")

    # --- writes ---------------------------------------------------------
    #
    # Everything below proposes. None of it decides: there is no call here
    # that resolves a review, creates a scholarship or publishes a cycle,
    # and the product enforces that with a separate credential rather than
    # trusting this client to be well behaved.

    async def submit_candidates(
        self,
        *,
        parent_discovery_id: str,
        workflow_version: str,
        candidates: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Turn split list items into discoveries the product owns.

        Returns a per-item result: created, duplicate or rejected. Resubmitting
        the same candidates is idempotent, so a retried job does not
        multiply the queue.
        """
        return await self._post(
            f"{AGENT_BASE}/candidates",
            {
                "parent_discovery_id": parent_discovery_id,
                "workflow_version": workflow_version,
                "candidates": candidates,
            },
            "submit_candidates",
        )

    async def attach_evidence(
        self,
        discovery_id: str,
        *,
        workflow_run_id: str,
        workflow_version: str,
        evidence: list[dict[str, Any]],
        prompt_version: str | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        """Attach claim-level evidence.

        Idempotent per run: resubmitting one does not double its evidence,
        while a genuinely new run adds its own alongside.
        """
        return await self._post(
            f"{AGENT_BASE}/candidates/{discovery_id}/evidence",
            {
                "workflow_run_id": workflow_run_id,
                "workflow_version": workflow_version,
                "prompt_version": prompt_version,
                "model": model,
                "evidence": evidence,
            },
            "attach_evidence",
        )

    async def request_review(
        self, discovery_id: str, *, reason: str, priority: int | None = None
    ) -> dict[str, Any]:
        """Ask for a human review task.

        Opens one; cannot close one, and cannot overwrite the product's own
        deterministic draft.
        """
        body: dict[str, Any] = {"reason": reason}
        if priority is not None:
            body["priority"] = priority
        return await self._post(
            f"{AGENT_BASE}/candidates/{discovery_id}/review", body, "request_review"
        )

    async def record_run(
        self,
        *,
        discovery_id: str,
        workflow_version: str,
        agent_outcome: str,
        recommendation: dict[str, Any] | None = None,
        prompt_version: str | None = None,
        model: str | None = None,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        """Record the outcome. Recorded, never acted on.

        Keyed on (discovery_id, workflow_version), so re-running a version
        updates in place while a new version is a new run.
        """
        return await self._post(
            f"{AGENT_BASE}/runs",
            {
                "discovery_id": discovery_id,
                "workflow_version": workflow_version,
                "agent_outcome": agent_outcome,
                "recommendation": recommendation,
                "prompt_version": prompt_version,
                "model": model,
                "correlation_id": correlation_id,
            },
            "record_run",
        )

    async def _post(self, path: str, body: dict[str, Any], what: str) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.post(f"{self.base_url}{path}", headers=self._headers, json=body)
        if response.status_code == 404:
            raise DiscoveryNotFound(f"{what}: the product has no such discovery")
        return _json_object(response, what)


def _json_object(response: httpx.Response, what: str) -> dict[str, Any]:
    if response.status_code >= 400:
        raise ScholarshipFinderError(
            f"{what} returned {response.status_code}: {_problem_code(response)}",
            status_code=response.status_code,
        )
    body = response.json()
    if not isinstance(body, dict):
        raise ScholarshipFinderError(f"{what} returned a non-object body")
    return body


def _problem_code(response: httpx.Response) -> str:
    """The product's machine-readable error code, when it sent one.

    Surfacing that rather than a body excerpt keeps a failure legible in a
    log without dragging the request payload along with it.
    """
    try:
        body = response.json()
        if isinstance(body, dict):
            return str(body.get("code") or body.get("detail") or "")
    except Exception:
        pass
    return ""


def client_from_settings() -> ScholarshipFinderClient:
    from app.core.config import get_settings

    settings = get_settings()
    if not settings.scholarship_finder_configured:
        raise ScholarshipFinderNotConfigured(
            "SCHOLARSHIP_FINDER_BASE_URL and SCHOLARSHIP_FINDER_AGENT_TOKEN are required"
        )
    assert settings.scholarship_finder_base_url and settings.scholarship_finder_agent_token
    return ScholarshipFinderClient(
        base_url=settings.scholarship_finder_base_url,
        token=settings.scholarship_finder_agent_token,
        timeout_seconds=settings.scholarship_finder_timeout_seconds,
    )
