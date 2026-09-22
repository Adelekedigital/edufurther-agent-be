"""Write-back, and the shadow-mode boundary.

Shadow mode is the setting Stage 1 of the rollout depends on, so what it
does and does not write is asserted directly rather than trusted. The
property that matters: in shadow mode nothing the product's review,
approval or publication paths can see is written.
"""

import uuid
from typing import Any

import pytest

from app.core.config import get_settings
from app.integrations.scholarship_finder import ScholarshipFinderError
from app.usecases.scholarship_finder import submission
from app.usecases.scholarship_finder.schemas import AgentOutcome, Candidate

PARENT_ID = str(uuid.uuid4())


class FakeFinder:
    """Records every write, so absence is as testable as presence."""

    def __init__(self, *, fail: str | None = None) -> None:
        self.fail = fail
        self.candidates: list[dict[str, Any]] = []
        self.evidence: list[dict[str, Any]] = []
        self.reviews: list[str] = []
        self.runs: list[dict[str, Any]] = []
        self.created_ids: dict[str, str] = {}

    def _maybe_fail(self, what: str) -> None:
        if self.fail == what:
            raise ScholarshipFinderError(f"{what} is unavailable")

    async def submit_candidates(self, *, parent_discovery_id, workflow_version, candidates):
        self._maybe_fail("submit_candidates")
        self.candidates.append({"parent": parent_discovery_id, "candidates": candidates})
        results = []
        for item in candidates:
            new_id = self.created_ids.setdefault(item["title"], str(uuid.uuid4()))
            results.append({"title": item["title"], "discovery_id": new_id, "status": "created"})
        return {"created": len(results), "duplicates": 0, "rejected": 0, "results": results}

    async def attach_evidence(self, discovery_id, **kwargs):
        self._maybe_fail("attach_evidence")
        self.evidence.append({"discovery_id": discovery_id, **kwargs})
        return {"recorded": len(kwargs.get("evidence") or []), "duplicates": 0}

    async def request_review(self, discovery_id, *, reason, priority=None):
        self._maybe_fail("request_review")
        self.reviews.append(discovery_id)
        return {"review_task_id": str(uuid.uuid4()), "created": True}

    async def record_run(self, **kwargs):
        self._maybe_fail("record_run")
        self.runs.append(kwargs)
        return {"run_id": str(uuid.uuid4())} | kwargs


@pytest.fixture
def finder(monkeypatch):
    def _install(*, shadow: bool, fail: str | None = None) -> FakeFinder:
        client = FakeFinder(fail=fail)
        monkeypatch.setattr(submission, "client_from_settings", lambda: client)
        monkeypatch.setattr(get_settings(), "shadow_mode", shadow)
        return client

    return _install


def candidate(**overrides) -> dict[str, Any]:
    base = {
        "title": "Award One",
        "identity_key": "award|one",
        "parent_source_url": "https://aggregator.test/list",
        "parent_discovery_id": PARENT_ID,
        "outcome": AgentOutcome.REVIEW_REQUIRED.value,
        "evidence": [
            {
                "claim_path": "funding.amount",
                "value": "£10,000",
                "source_url": "https://official.test/a",
                "source_type": "official_page",
                "observed_at": "2026-09-21T00:00:00+00:00",
            }
        ],
    }
    return Candidate(**(base | overrides)).to_dict()


def state(candidates: list[dict[str, Any]], **overrides) -> dict[str, Any]:
    return {
        "job_id": str(uuid.uuid4()),
        "workflow_version": "v1",
        "correlation_id": "agent_run_1",
        "discovery": {"discovery_id": PARENT_ID},
        "page_type": "list",
        "page_fetch_method": "direct",
        "outcome": AgentOutcome.REVIEW_REQUIRED.value,
        "uncertainty_reasons": [],
        "candidates": candidates,
    } | overrides


# --- shadow mode -------------------------------------------------------


async def test_shadow_mode_creates_no_candidates(finder):
    """Each candidate would become a real Discovery and enter the review
    queue, which is a change to product state."""
    client = finder(shadow=True)

    await submission.submit_to_product(state([candidate()]))

    assert client.candidates == []


async def test_shadow_mode_requests_no_reviews(finder):
    client = finder(shadow=True)

    await submission.submit_to_product(state([candidate()]))

    assert client.reviews == []


async def test_shadow_mode_still_records_the_run(finder):
    """What makes a shadow run auditable from the product's side rather
    than only from this service's logs."""
    client = finder(shadow=True)

    await submission.submit_to_product(state([candidate()]))

    assert len(client.runs) == 1
    run = client.runs[0]
    assert run["discovery_id"] == PARENT_ID
    assert run["agent_outcome"] == AgentOutcome.REVIEW_REQUIRED.value
    assert run["recommendation"]["candidate_count"] == 1


async def test_shadow_mode_records_the_full_breakdown_against_the_parent(finder):
    """Split candidates have no discovery of their own yet, so their detail
    is recorded against the page it came from. Nothing is lost."""
    client = finder(shadow=True)
    candidates = [candidate(title=f"Award {i}", identity_key=f"award|{i}") for i in range(3)]

    await submission.submit_to_product(state(candidates))

    recommendation = client.runs[0]["recommendation"]
    assert recommendation["candidate_count"] == 3
    assert {c["title"] for c in recommendation["candidates"]} == {
        "Award 0",
        "Award 1",
        "Award 2",
    }


async def test_shadow_mode_attaches_evidence_to_a_discovery_that_already_exists(finder):
    """An individual page's candidate *is* the parent discovery, so its
    evidence has somewhere to go even when nothing is created."""
    client = finder(shadow=True)

    await submission.submit_to_product(state([candidate(discovery_id=PARENT_ID)]))

    assert len(client.evidence) == 1
    assert client.evidence[0]["discovery_id"] == PARENT_ID


async def test_shadow_mode_attaches_no_evidence_for_an_uncreated_candidate(finder):
    client = finder(shadow=True)

    await submission.submit_to_product(state([candidate(discovery_id=None)]))

    assert client.evidence == []


# --- live mode ---------------------------------------------------------


async def test_live_mode_creates_candidates_and_keeps_their_ids(finder):
    client = finder(shadow=False)

    result = await submission.submit_to_product(state([candidate()]))

    assert len(client.candidates) == 1
    assert result["candidates"][0]["discovery_id"] == client.created_ids["Award One"]


async def test_live_mode_attaches_evidence_to_the_new_discovery(finder):
    client = finder(shadow=False)

    await submission.submit_to_product(state([candidate()]))

    assert len(client.evidence) == 1
    assert client.evidence[0]["discovery_id"] == client.created_ids["Award One"]
    assert client.evidence[0]["workflow_version"] == "v1"


async def test_live_mode_asks_for_a_review_when_one_is_needed(finder):
    client = finder(shadow=False)

    await submission.submit_to_product(
        state([candidate(outcome=AgentOutcome.REVIEW_REQUIRED.value)])
    )

    assert client.reviews == [client.created_ids["Award One"]]


@pytest.mark.parametrize(
    "outcome",
    [
        AgentOutcome.MORE_EVIDENCE_REQUIRED.value,
        AgentOutcome.REJECT_RECOMMENDED.value,
        AgentOutcome.AUTO_CHECK_ELIGIBLE.value,
    ],
)
async def test_other_outcomes_do_not_ask_for_a_review(finder, outcome):
    """The product gives every new candidate a review task through
    link_canonical anyway. Asking again for each one would flood the queue
    this service exists to shorten."""
    client = finder(shadow=False)

    await submission.submit_to_product(state([candidate(outcome=outcome)]))

    assert client.reviews == []


async def test_live_mode_records_a_run_per_candidate_and_one_for_the_parent(finder):
    client = finder(shadow=False)
    candidates = [candidate(title=f"Award {i}", identity_key=f"award|{i}") for i in range(2)]

    await submission.submit_to_product(state(candidates))

    recorded = [run["discovery_id"] for run in client.runs]
    assert PARENT_ID in recorded
    assert len(recorded) == 3


async def test_auto_check_eligible_is_submitted_as_a_recommendation_only(finder):
    """The outcome most easily misread as an instruction. Submitting one
    creates no scholarship and publishes nothing - there is no call in this
    client that could."""
    client = finder(shadow=False)

    await submission.submit_to_product(
        state(
            [candidate(outcome=AgentOutcome.AUTO_CHECK_ELIGIBLE.value)],
            outcome=AgentOutcome.AUTO_CHECK_ELIGIBLE.value,
        )
    )

    assert client.reviews == []
    assert all(
        run["agent_outcome"] == AgentOutcome.AUTO_CHECK_ELIGIBLE.value for run in client.runs
    )
    assert not hasattr(client, "publish")


# --- resilience --------------------------------------------------------


@pytest.mark.parametrize(
    "failing", ["submit_candidates", "attach_evidence", "request_review", "record_run"]
)
async def test_a_submission_failure_never_raises(finder, failing):
    """A failure here must not discard the analysis that produced it. The
    job's own output row already holds the result, so this is recoverable
    by resubmitting rather than by re-running every model call."""
    finder(shadow=False, fail=failing)

    result = await submission.submit_to_product(state([candidate()]))

    assert result is not None


async def test_a_failed_candidate_submission_is_reported_not_swallowed(finder):
    finder(shadow=False, fail="submit_candidates")

    result = await submission.submit_to_product(state([candidate()]))

    assert any("candidate submission failed" in note for note in result["notes"])


async def test_an_unconfigured_product_skips_submission_without_failing(monkeypatch):
    """A development environment with no product configured must still be
    able to run the workflow end to end."""
    monkeypatch.setattr(get_settings(), "scholarship_finder_base_url", None)
    monkeypatch.setattr(get_settings(), "scholarship_finder_agent_token", None)

    result = await submission.submit_to_product(state([candidate()]))

    assert any("submission skipped" in reason for reason in result["uncertainty_reasons"])


async def test_a_run_with_no_candidates_still_records_an_outcome(finder):
    """A list page that decomposed to nothing is a real answer, and it
    should be visible in the product rather than only in a log."""
    client = finder(shadow=True)

    await submission.submit_to_product(state([], outcome=AgentOutcome.MORE_EVIDENCE_REQUIRED.value))

    assert len(client.runs) == 1
    assert client.runs[0]["agent_outcome"] == AgentOutcome.MORE_EVIDENCE_REQUIRED.value
