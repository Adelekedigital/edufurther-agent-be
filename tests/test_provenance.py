"""One rule: nothing is recorded without saying what produced it.

Spec §16 asks for the workflow version, the prompt version and the model on
every job. Two of the three were plumbed. `model` was not, and the Stage 1
pilot ran to completion with it NULL in every row of both databases - 62
`agent_runs`, 39 `discovery_evidence`, every `agent_outputs` - while 503
tests passed.

They passed because none of them looked. The router returned the field, the
client had no slot for it, `Candidate.model` was declared and never
assigned, and every writer downstream faithfully stored the `None` it was
handed. Each link was individually reasonable, which is why the gap ran the
length of the chain.

The failure is quiet by nature: nothing breaks, and the cost arrives later,
when quality moves after a model change and nothing on the record says which
model wrote what. So the rule is asserted at every link rather than at the
end - a test that only checks the last one passes just as happily when the
first is broken.
"""

from typing import Any

import pytest

from app.integrations.ai_router import AITask
from app.usecases.scholarship_finder import nodes, submission
from app.usecases.scholarship_finder.schemas import AgentOutcome, Candidate

# The harness these reuse, so the fakes stay in one place.
from tests.test_ai_router_client import (  # noqa: F401
    keypair,
    make_client,
    ok_response,
    sample_request,
)
from tests.test_scholarship_workflow_integration import (  # noqa: F401
    discovery,
    router_response,
    run,
    split_output,
    wire,
)
from tests.test_submission_integration import PARENT_ID, FakeFinder, finder, state  # noqa: F401

PAGE = "<html><body>" + ("real page content " * 200) + "</body></html>"


def award(**overrides: Any) -> Candidate:
    """The required lineage, so each test states only what it is about."""
    base: dict[str, Any] = {
        "title": "Award One",
        "identity_key": "award|one",
        "parent_source_url": "https://aggregator.test/list",
        "parent_discovery_id": PARENT_ID,
    }
    return Candidate(**(base | overrides))


@pytest.fixture
def captured() -> dict:
    return {}


# --- link 1: the router's answer ---------------------------------------


async def test_the_client_reads_the_model_off_the_response(keypair, monkeypatch, captured):
    """The root cause. `AIRouterResponse` had no `model` field, so the
    router sent it and nothing read it - and every NULL downstream follows
    from this one omission."""
    private, _ = keypair
    body = {
        "request_id": "req-1",
        "status": "completed",
        "output": {"page_type": "list", "evidence": []},
        "model_policy_version": "ai-policy-v1",
        "prompt_version": "classify_source_page-v1",
        "model": "claude-sonnet-5",
        "trace_reference": None,
    }
    client = make_client(private, monkeypatch, captured, ok_response(captured, body))

    response = await client.execute(sample_request())

    assert response.model == "claude-sonnet-5"


async def test_a_response_without_a_model_is_none_not_a_failure(keypair, monkeypatch, captured):
    """The router's own field is optional, so an older deploy replaying a
    stored response omits it. Missing provenance is a gap to record, never
    a reason to fail a run that otherwise worked."""
    private, _ = keypair
    body = {
        "request_id": "req-1",
        "status": "completed",
        "output": {"page_type": "list", "evidence": []},
        "model_policy_version": "ai-policy-v1",
    }
    client = make_client(private, monkeypatch, captured, ok_response(captured, body))

    response = await client.execute(sample_request())

    assert response.model is None
    assert response.completed


def test_the_model_is_not_confused_with_the_policy_that_chose_it():
    """`model_policy_version` names the routing policy. A policy that falls
    back reports one version for two different models, which is why it
    cannot stand in for this."""
    from app.integrations.ai_router import AIRouterResponse

    response = AIRouterResponse(
        request_id="r",
        outcome=router_response(None).outcome,
        output=None,
        model_policy_version="ai-policy-v1",
        prompt_version="task-v1",
        model="claude-haiku-4-5",
        trace_reference=None,
    )

    assert response.model != response.model_policy_version


# --- link 2: the candidate ---------------------------------------------


def test_a_candidate_attributes_to_the_model_that_read_its_facts():
    """The product stores one model per row, and extraction is the call
    that produced the facts the row asserts - matching the existing choice
    of `prompt_versions["extract"]` for those same columns."""
    c = award()
    c.models = {"split": "model-a", "extract": "model-b", "compare": "model-c"}

    assert c.model == "model-b"


def test_a_candidate_that_never_reached_extraction_still_attributes():
    """A split candidate whose extraction call failed has facts from
    nowhere but the split. Falling back to None would record a candidate
    that no model appears to have touched."""
    c = award()
    c.models = {"split": "model-a"}

    assert c.model == "model-a"


def test_a_candidate_no_model_touched_says_so():
    c = award()

    assert c.model is None


def test_the_model_survives_the_checkpoint_round_trip():
    """Candidates cross into LangGraph state as plain dicts and are
    rehydrated in the next node. Provenance held only in memory would be
    lost on the first resume."""
    c = award()
    c.models = {"extract": "model-b"}

    revived = Candidate.from_dict(c.to_dict())

    assert revived.models == {"extract": "model-b"}
    assert revived.model == "model-b"
    assert c.to_dict()["model"] == "model-b", "the dict form still carries it directly"


# --- link 3: the nodes record it ---------------------------------------


@pytest.mark.parametrize(
    "task,key",
    [
        (AITask.extract_scholarship_facts, "extract"),
        (AITask.compare_official_evidence, "compare"),
        (AITask.extract_eligibility_requirements, "eligibility"),
    ],
)
async def test_every_task_records_its_prompt_and_its_model_together(task, key):
    """Asserted as a pair. They were separate assignments before, and all
    four call sites set one and none set the other."""
    c = award()

    nodes._note_provenance(c, key, router_response({}, model="model-x"))

    assert c.prompt_versions[key] == "task-v1"
    assert c.models[key] == "model-x"


async def test_a_task_that_reported_no_model_records_no_model():
    """Rather than an empty string, which would read as a model named ""
    once it reached a column."""
    c = award()

    nodes._note_provenance(c, "extract", router_response({}, model=None))

    assert "extract" not in c.models
    assert c.prompt_versions["extract"] == "task-v1"


# --- link 4: the page-level decision -----------------------------------


async def test_the_page_records_what_classified_it(wire):
    """`agent_runs` for a list page is about the page, not about any one
    candidate, so it cannot borrow a candidate's attribution. This row was
    0 of 62 for both fields."""
    wire(
        answers={AITask.classify_source_page: {"page_type": "individual", "evidence": []}},
        source_text=PAGE,
    )

    result = await run()

    assert result["page_model"] == "claude-test-1"
    assert result["page_prompt_version"] == "task-v1"


@pytest.mark.parametrize(
    "answer,why",
    [
        ({"page_type": "nonsense", "evidence": []}, "an unrecognised page type"),
        (router_response(None), "a model that did not answer"),
    ],
)
async def test_a_classification_that_fell_back_is_still_attributed(wire, answer, why):
    """Both fallbacks return `individual`. A run that fell back because the
    model failed is precisely the run someone will later want to attribute,
    so the provenance must not be attached only to the happy path."""
    wire(answers={AITask.classify_source_page: answer}, source_text=PAGE)

    result = await run()

    assert result["page_type"] == "individual"
    assert result.get("page_prompt_version") == "task-v1", why


# --- link 5: what the product is actually handed ------------------------


async def test_the_page_run_reaches_the_product_with_its_provenance(finder):
    """The end of the chain, and the row the pilot found empty."""
    client = finder(shadow=True)

    await submission.submit_to_product(
        state([], page_model="model-p", page_prompt_version="classify-v2")
    )

    run_record = client.runs[0]
    assert run_record["model"] == "model-p"
    assert run_record["prompt_version"] == "classify-v2"


async def test_a_candidate_run_reaches_the_product_with_its_provenance(finder):
    client = finder(shadow=False)
    c = award(outcome=AgentOutcome.REVIEW_REQUIRED.value)
    c.models = {"extract": "model-e"}
    c.prompt_versions = {"extract": "extract-v3"}

    await submission.submit_to_product(state([c.to_dict()]))

    candidate_runs = [r for r in client.runs if r["discovery_id"] != PARENT_ID]
    assert candidate_runs, "the candidate's own run was not recorded"
    assert candidate_runs[0]["model"] == "model-e"
    assert candidate_runs[0]["prompt_version"] == "extract-v3"


async def test_evidence_reaches_the_product_with_its_provenance(finder):
    """A claim is the thing a later accuracy question is actually about, so
    this is the attribution that matters most."""
    client = finder(shadow=False)
    c = award(
        outcome=AgentOutcome.REVIEW_REQUIRED.value,
        evidence=[
            {
                "claim_path": "funding.amount",
                "value": "£10,000",
                "source_url": "https://official.test/a",
                "source_type": "official_page",
                "observed_at": "2026-09-21T00:00:00+00:00",
            }
        ],
    )
    c.models = {"extract": "model-e"}
    c.prompt_versions = {"extract": "extract-v3"}

    await submission.submit_to_product(state([c.to_dict()]))

    assert client.evidence, "no evidence was written"
    assert client.evidence[0]["model"] == "model-e"
    assert client.evidence[0]["prompt_version"] == "extract-v3"


# --- the whole chain ----------------------------------------------------


async def test_a_real_run_hands_the_product_a_model_at_every_level(wire, finder, monkeypatch):
    """The test that would have caught it. Every assertion above can hold
    link by link while the run as a whole still records nothing, because
    the failure was that no link ever passed the value on."""
    client = finder(shadow=False)
    wire(
        answers={
            AITask.classify_source_page: {"page_type": "list", "evidence": []},
            AITask.split_list_candidates: split_output(2),
            AITask.extract_scholarship_facts: {"candidate": {}, "evidence": []},
            AITask.compare_official_evidence: {
                "comparisons": [],
                "contradictions": [],
                "evidence": [],
            },
            AITask.extract_eligibility_requirements: {"rules": [], "evidence": []},
        },
        source_text=PAGE,
        official="Worth £1,000 closing March 1, 2026.",
    )

    await run()

    # Two candidates and the page they came from. Pinned, because an
    # assertion over `client.runs` holds trivially if write-back quietly
    # stops recording the candidate rows.
    assert len(client.runs) == 3, [r["discovery_id"] for r in client.runs]
    assert all(r["model"] for r in client.runs), (
        f"a run reached the product unattributed: {[r['model'] for r in client.runs]}"
    )
    assert all(r["prompt_version"] for r in client.runs)
    assert client.evidence and all(e["model"] for e in client.evidence)
