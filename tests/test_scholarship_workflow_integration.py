"""The scholarship workflow, end to end.

Every external dependency is faked - the product, the router and the
fetchers - but the graph itself is real: real nodes, real branching, real
deterministic comparison and the real outcome policy. What is under test is
the wiring and the judgement, not httpx.

The scenarios are drawn from spec §24's acceptance list: an individual
page, a ten-item list, a missing official page, a conflicting deadline, a
non-numeric academic requirement, a duplicate, and a page that is not a
scholarship at all.
"""

import uuid
from typing import Any

import pytest

from app.integrations.ai_router import AIRouterOutcome, AIRouterResponse, AITask
from app.tools.retrieval import RetrievedPage
from app.usecases.scholarship_finder import nodes
from app.usecases.scholarship_finder.graph import build
from app.usecases.scholarship_finder.schemas import AgentOutcome, PageType
from tests.conftest import requires_db

pytestmark = requires_db

LIST_URL = "https://aggregator.test/top-10"
DISCOVERY_ID = str(uuid.uuid4())


def discovery(**overrides) -> dict[str, Any]:
    return {
        "discovery_id": DISCOVERY_ID,
        "source_url": LIST_URL,
        "approved_domains": ["aggregator.test"],
        "raw_title": "Top 10 Scholarships for 2026",
        "raw_excerpt": "A roundup of ten funding opportunities.",
        "authority_grade": "C",
    } | overrides


def router_response(output: dict | None, *, outcome=AIRouterOutcome.completed) -> AIRouterResponse:
    return AIRouterResponse(
        request_id="req-1",
        outcome=outcome,
        output=output,
        model_policy_version="ai-policy-v1",
        prompt_version="task-v1",
        trace_reference=None,
    )


class FakeRouter:
    """Answers per task, and records what it was asked."""

    def __init__(self, answers: dict[AITask, Any]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, str]] = []

    async def execute(self, request):
        self.calls.append((request.task.value, request.idempotency_key))
        answer = self.answers.get(request.task)
        if callable(answer):
            answer = answer(request)
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, AIRouterResponse):
            return answer
        return router_response(answer)


class FakeFinder:
    def __init__(self, record: dict[str, Any]) -> None:
        self.record = record

    async def get_discovery(self, discovery_id: str) -> dict[str, Any]:
        return self.record


def page(text: str, *, method: str = "direct", status: int = 200) -> RetrievedPage:
    return RetrievedPage(
        url="https://example.test/x",
        text=text,
        fetch_method=method,
        status_code=status,
        content_type="text/html",
        byte_length=len(text.encode()),
    )


@pytest.fixture
def wire(monkeypatch):
    """Install fakes for the product, the router and both fetchers."""

    def _wire(
        *,
        record: dict[str, Any] | None = None,
        answers: dict[AITask, Any] | None = None,
        source_text: str = "ten scholarships listed here",
        official: Any = None,
    ) -> FakeRouter:
        router = FakeRouter(answers or {})
        finder = FakeFinder(record or discovery())
        monkeypatch.setattr(nodes, "sf_client_from_settings", lambda: finder)
        monkeypatch.setattr(nodes, "client_from_settings", lambda: router)

        # Accepts job_id because the real ones do: retrieval records each
        # call against the job, and a fake with the old signature would
        # pass while the wiring it stands in for was broken.
        async def fake_fetch_page(db, url, domains, *, job_id=None):
            return page(source_text)

        async def fake_fetch_official(
            db, url, domains, *, job_id=None, allow_any_public_domain=False
        ):
            if official is None:
                raise RuntimeError("official page unreachable")
            if isinstance(official, Exception):
                raise official
            return page(official, method="jina")

        monkeypatch.setattr(nodes, "fetch_page", fake_fetch_page)
        monkeypatch.setattr(nodes, "fetch_official_page", fake_fetch_official)
        return router

    return _wire


async def run(**overrides) -> dict[str, Any]:
    graph = build().compile()
    state = {
        "job_id": str(uuid.uuid4()),
        "product_id": "scholarship_finder",
        "use_case_id": "scholarship_verification",
        "input_reference": DISCOVERY_ID,
        "correlation_id": "agent_test_run",
        "workflow_version": "v1",
        "payload": {},
        "notes": [],
        "uncertainty_reasons": [],
        "outcome": None,
    }
    return dict(await graph.ainvoke(state | overrides))


def split_output(count: int, *, host: str = "official.test") -> dict:
    return {
        "candidates": [
            {
                "title": f"Award {index}",
                "url": f"https://{host}/award-{index}",
                "excerpt": f"Worth £{index},000 closing March 1, 2026.",
                "heading": f"{index}. Award {index}",
            }
            for index in range(1, count + 1)
        ],
        "evidence": [],
    }


# --- a ten-item list page: the headline capability ---------------------


async def test_a_list_page_becomes_one_candidate_per_award(wire):
    wire(
        answers={
            AITask.classify_source_page: {"page_type": "list", "evidence": []},
            AITask.split_list_candidates: split_output(10),
            AITask.extract_scholarship_facts: {"candidate": {}, "evidence": []},
            AITask.compare_official_evidence: {
                "comparisons": [],
                "contradictions": [],
                "evidence": [],
            },
            AITask.extract_eligibility_requirements: {"rules": [], "evidence": []},
        },
        official="Worth £1,000 closing March 1, 2026.",
    )

    result = await run()

    assert result["page_type"] == PageType.LIST.value
    assert len(result["candidates"]) == 10
    # Each keeps the page it came from - the lineage the product records as
    # split_from_discovery_id.
    assert all(c["parent_source_url"] == LIST_URL for c in result["candidates"])
    assert all(c["parent_discovery_id"] == DISCOVERY_ID for c in result["candidates"])
    assert len({c["identity_key"] for c in result["candidates"]}) == 10


async def test_an_individual_page_is_one_candidate(wire):
    wire(
        record=discovery(raw_title="The Example Scholarship"),
        answers={
            AITask.classify_source_page: {"page_type": "individual", "evidence": []},
            AITask.extract_scholarship_facts: {"candidate": {}, "evidence": []},
            AITask.extract_eligibility_requirements: {"rules": [], "evidence": []},
        },
    )

    result = await run()

    assert len(result["candidates"]) == 1
    assert result["candidates"][0]["title"] == "The Example Scholarship"


async def test_a_page_with_no_award_is_rejected_without_extraction(wire):
    """Short-circuits to the decision. Routing an empty candidate to a
    reviewer would waste the one resource this system is trying to save."""
    router = wire(
        answers={AITask.classify_source_page: {"page_type": "not_a_scholarship", "evidence": []}}
    )

    result = await run()

    assert result["outcome"] == AgentOutcome.REJECT_RECOMMENDED.value
    assert result["candidates"] == []
    assert [task for task, _ in router.calls] == ["classify_source_page"]


# --- deduplication -----------------------------------------------------


async def test_the_same_award_listed_twice_becomes_one_candidate(wire):
    """Deterministic, by the product's own identity key - so the agent's
    idea of a duplicate matches the product's rather than being re-split
    on submission."""
    wire(
        answers={
            AITask.classify_source_page: {"page_type": "list", "evidence": []},
            AITask.split_list_candidates: {
                "candidates": [
                    {"title": "Chevening Scholarship", "url": "https://official.test/a"},
                    {"title": "  chevening   scholarship ", "url": None},
                    {"title": "Rhodes Scholarship", "url": "https://official.test/b"},
                ],
                "evidence": [],
            },
            AITask.extract_scholarship_facts: {"candidate": {}, "evidence": []},
            AITask.compare_official_evidence: {
                "comparisons": [],
                "contradictions": [],
                "evidence": [],
            },
            AITask.extract_eligibility_requirements: {"rules": [], "evidence": []},
        },
        official="Nothing in particular.",
    )

    result = await run()

    titles = {c["title"] for c in result["candidates"]}
    assert len(result["candidates"]) == 2
    assert "Rhodes Scholarship" in titles
    # The one with a link of its own is kept: strictly more useful.
    kept = next(c for c in result["candidates"] if "chevening" in c["identity_key"])
    assert kept["url"] == "https://official.test/a"


# --- official source ---------------------------------------------------


async def test_a_link_that_stays_on_the_source_domain_is_not_official(wire):
    """An aggregator linking to its own page is not corroboration. Treating
    it as such is how a list page ends up standing as proof of every award
    on it."""
    wire(
        answers={
            AITask.classify_source_page: {"page_type": "list", "evidence": []},
            AITask.split_list_candidates: split_output(1, host="aggregator.test"),
            AITask.extract_scholarship_facts: {"candidate": {}, "evidence": []},
            AITask.extract_eligibility_requirements: {"rules": [], "evidence": []},
        }
    )

    result = await run()

    candidate = result["candidates"][0]
    assert candidate["official_url"] is None
    assert candidate["outcome"] == AgentOutcome.MORE_EVIDENCE_REQUIRED.value
    assert any("source domain" in reason for reason in candidate["uncertainty_reasons"])


async def test_a_missing_official_page_is_more_evidence_required(wire):
    wire(
        answers={
            AITask.classify_source_page: {"page_type": "list", "evidence": []},
            AITask.split_list_candidates: split_output(1),
            AITask.extract_scholarship_facts: {"candidate": {}, "evidence": []},
            AITask.extract_eligibility_requirements: {"rules": [], "evidence": []},
        },
        official=None,
    )

    result = await run()

    assert result["outcome"] == AgentOutcome.MORE_EVIDENCE_REQUIRED.value
    assert any(
        "fetch failed" in reason for reason in result["candidates"][0]["uncertainty_reasons"]
    )


# --- conflicting evidence ----------------------------------------------


async def test_a_conflicting_deadline_is_rejected(wire):
    """The candidate claims March, the official page says April. That is
    not a gap in what we know."""
    wire(
        answers={
            AITask.classify_source_page: {"page_type": "individual", "evidence": []},
            AITask.extract_scholarship_facts: {"candidate": {}, "evidence": []},
            AITask.compare_official_evidence: {
                "comparisons": [],
                "contradictions": [],
                "evidence": [],
            },
            AITask.extract_eligibility_requirements: {"rules": [], "evidence": []},
        },
        record=discovery(
            raw_title="Example Award",
            raw_excerpt="Worth £5,000 closing March 1, 2026.",
            source_url="https://provider.test/award",
            approved_domains=["provider.test"],
            authority_grade="A",
        ),
        official="Worth £5,000 closing April 30, 2026.",
    )

    result = await run()

    candidate = result["candidates"][0]
    assert candidate["deadline_agrees"] is False
    assert candidate["outcome"] == AgentOutcome.REJECT_RECOMMENDED.value


async def test_agreement_is_decided_deterministically_not_by_the_model(wire):
    """The model is asked to locate passages. Whether two values agree is
    computed by fact_matching - asking a model would be asking it to
    verify."""
    wire(
        answers={
            AITask.classify_source_page: {"page_type": "individual", "evidence": []},
            AITask.extract_scholarship_facts: {"candidate": {}, "evidence": []},
            # The model claims everything agrees. It does not get a vote.
            AITask.compare_official_evidence: {
                "comparisons": [{"claim_path": "funding.amount", "relationship": "supported"}],
                "contradictions": [],
                "evidence": [],
            },
            AITask.extract_eligibility_requirements: {"rules": [], "evidence": []},
        },
        record=discovery(
            raw_title="Example Award",
            raw_excerpt="Worth £13,000 closing March 1, 2026.",
            source_url="https://provider.test/award",
            approved_domains=["provider.test"],
            authority_grade="A",
        ),
        official="Worth £16,750 closing March 1, 2026.",
    )

    result = await run()

    candidate = result["candidates"][0]
    assert candidate["amount_agrees"] is False, "the model's claim overrode the comparison"
    assert candidate["outcome"] == AgentOutcome.REJECT_RECOMMENDED.value


# --- eligibility -------------------------------------------------------


async def test_a_non_numeric_academic_requirement_is_kept_verbatim(wire):
    """ "A strong academic record" is not a number and must not become one.
    Inventing an equivalence makes an applicant's real result
    unrecoverable."""
    wire(
        answers={
            AITask.classify_source_page: {"page_type": "individual", "evidence": []},
            AITask.extract_scholarship_facts: {"candidate": {}, "evidence": []},
            AITask.compare_official_evidence: {
                "comparisons": [],
                "contradictions": [],
                "evidence": [],
            },
            AITask.extract_eligibility_requirements: {
                "rules": [
                    {
                        "requirement_type": "academic_result",
                        "raw_value": "a strong academic record",
                        "scale_type": "qualitative",
                        "source_wording": "Applicants should have a strong academic record.",
                    }
                ],
                "evidence": [],
            },
        },
        record=discovery(
            raw_title="Example Award",
            raw_excerpt="Details.",
            source_url="https://provider.test/award",
            authority_grade="A",
        ),
        official="Applicants should have a strong academic record.",
    )

    result = await run()

    rule = result["candidates"][0]["eligibility_rules"][0]
    assert rule["raw_value"] == "a strong academic record"
    assert rule["scale_type"] == "qualitative"
    assert rule["scale_max"] is None
    assert rule["equivalency_status"] == "not_converted"


async def test_an_unrecognised_scale_is_recorded_as_unknown_not_guessed(wire):
    """Guessing is how a percentage becomes a GPA."""
    wire(
        answers={
            AITask.classify_source_page: {"page_type": "individual", "evidence": []},
            AITask.extract_scholarship_facts: {"candidate": {}, "evidence": []},
            AITask.compare_official_evidence: {
                "comparisons": [],
                "contradictions": [],
                "evidence": [],
            },
            AITask.extract_eligibility_requirements: {
                "rules": [
                    {
                        "requirement_type": "academic_result",
                        "raw_value": "第一級",
                        "scale_type": "japanese_first_class",
                        "source_wording": "第一級の成績",
                    }
                ],
                "evidence": [],
            },
        },
        record=discovery(
            raw_title="Example Award",
            raw_excerpt="Details.",
            source_url="https://provider.test/award",
            authority_grade="A",
        ),
        official="Requirements.",
    )

    result = await run()

    rule = result["candidates"][0]["eligibility_rules"][0]
    assert rule["scale_type"] == "unknown"
    assert rule["raw_value"] == "第一級"


# --- resilience --------------------------------------------------------


async def test_a_failed_split_falls_back_to_one_candidate(wire):
    """A reviewer seeing one under-decomposed record can act on it; a
    failed job leaves the page unprocessed entirely."""
    from app.integrations.ai_router import AIRouterError

    wire(
        answers={
            AITask.classify_source_page: {"page_type": "list", "evidence": []},
            AITask.split_list_candidates: AIRouterError("router unavailable"),
            AITask.extract_scholarship_facts: {"candidate": {}, "evidence": []},
            AITask.extract_eligibility_requirements: {"rules": [], "evidence": []},
        }
    )

    result = await run()

    assert len(result["candidates"]) == 1
    assert any("list splitting failed" in r for r in result["uncertainty_reasons"])


async def test_a_failed_classification_does_not_abandon_the_run(wire):
    wire(
        answers={
            AITask.classify_source_page: router_response(
                None, outcome=AIRouterOutcome.provider_unavailable
            ),
            AITask.extract_scholarship_facts: {"candidate": {}, "evidence": []},
            AITask.extract_eligibility_requirements: {"rules": [], "evidence": []},
        }
    )

    result = await run()

    assert result["page_type"] == PageType.INDIVIDUAL.value
    assert result["outcome"] is not None
    assert any("classification unavailable" in r for r in result["uncertainty_reasons"])


async def test_one_failing_candidate_does_not_discard_the_others(wire):
    """Per-candidate isolation. This ecosystem lost a whole harvest batch
    to one bad row once."""
    from app.integrations.ai_router import AIRouterError

    def eligibility(request):
        if request.idempotency_key.endswith("eligibility:0"):
            return AIRouterError("boom")
        return {"rules": [{"requirement_type": "nationality", "raw_value": "any"}], "evidence": []}

    wire(
        answers={
            AITask.classify_source_page: {"page_type": "list", "evidence": []},
            AITask.split_list_candidates: split_output(3),
            AITask.extract_scholarship_facts: {"candidate": {}, "evidence": []},
            AITask.compare_official_evidence: {
                "comparisons": [],
                "contradictions": [],
                "evidence": [],
            },
            AITask.extract_eligibility_requirements: eligibility,
        },
        official="Worth £1,000 closing March 1, 2026.",
    )

    result = await run()

    assert len(result["candidates"]) == 3
    failed = result["candidates"][0]
    assert any("eligibility extraction failed" in r for r in failed["uncertainty_reasons"])
    assert all(c["eligibility_rules"] for c in result["candidates"][1:])


# --- provenance --------------------------------------------------------


async def test_model_and_deterministic_facts_are_never_merged(wire):
    """The product's own rule, for the reason that survives here: they have
    different provenance, and merging leaves a reviewer unable to tell
    which part came from where."""
    wire(
        answers={
            AITask.classify_source_page: {"page_type": "individual", "evidence": []},
            AITask.extract_scholarship_facts: {
                "candidate": {"funding_amount": "GBP 5,000"},
                "evidence": [],
            },
            AITask.compare_official_evidence: {
                "comparisons": [],
                "contradictions": [],
                "evidence": [],
            },
            AITask.extract_eligibility_requirements: {"rules": [], "evidence": []},
        },
        record=discovery(
            raw_title="Award",
            raw_excerpt="Worth £5,000.",
            source_url="https://provider.test/award",
            authority_grade="A",
        ),
        official="Worth £5,000.",
    )

    result = await run()

    candidate = result["candidates"][0]
    assert candidate["deterministic_facts"]["funding_mentions"] == ["£5,000"]
    assert candidate["model_facts"]["funding_amount"] == "GBP 5,000"
    assert candidate["deterministic_facts"]["needs_human_review"] is True


async def test_model_calls_use_deterministic_idempotency_keys(wire):
    """A retried node replays the router's stored response rather than
    paying for the same call twice."""
    router = wire(
        answers={
            AITask.classify_source_page: {"page_type": "individual", "evidence": []},
            AITask.extract_scholarship_facts: {"candidate": {}, "evidence": []},
            AITask.extract_eligibility_requirements: {"rules": [], "evidence": []},
        }
    )
    job_id = str(uuid.uuid4())

    await run(job_id=job_id)

    keys = [key for _, key in router.calls]
    assert all(key.startswith(job_id) for key in keys)
    assert len(keys) == len(set(keys)), "two calls shared an idempotency key"


async def test_page_bodies_do_not_leak_into_the_checkpointed_state(wire):
    """Whole page bodies in a checkpoint would bloat every resume. The
    official text is used and dropped."""
    wire(
        answers={
            AITask.classify_source_page: {"page_type": "individual", "evidence": []},
            AITask.extract_scholarship_facts: {"candidate": {}, "evidence": []},
            AITask.compare_official_evidence: {
                "comparisons": [],
                "contradictions": [],
                "evidence": [],
            },
            AITask.extract_eligibility_requirements: {"rules": [], "evidence": []},
        },
        record=discovery(
            raw_title="Award",
            raw_excerpt="Worth £5,000.",
            source_url="https://provider.test/award",
            authority_grade="A",
        ),
        official="A very long official page body." * 100,
    )

    result = await run()

    assert "_official_text" not in result["candidates"][0]["model_facts"]
