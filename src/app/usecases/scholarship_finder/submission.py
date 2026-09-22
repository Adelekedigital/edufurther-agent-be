"""Handing results back to the product.

The last node, and the only one that writes anything the product can see.
Everything it does is a proposal: create candidates, attach evidence, ask
for a review, record an outcome. None of it decides - the product's
credential for this service cannot resolve a review or publish a cycle, so
the boundary holds even if this module is wrong.

## Shadow mode

`SHADOW_MODE=true` is the default, and it is the setting Stage 1 of the
rollout runs under. It restricts writes to what the product's existing
pipeline cannot see:

* `agent_runs` and `discovery_evidence` are written - both are tables the
  agent owns, which nothing in review, approval or publication reads;
* candidates are **not** created, because each one becomes a real
  `Discovery` and enters the review queue, which is a change to product
  state;
* reviews are **not** requested.

That is a slightly stricter reading than "never creates or updates a review
task": creating a discovery does not touch a review task directly, but
`link_canonical` will give it one moments later, so submitting candidates in
shadow mode would change the queue by a longer route.

The consequence is worth stating: in shadow mode, candidates split from a
list page have no discovery of their own to attach evidence to, so their
evidence waits. The parent's `agent_runs` row carries the full breakdown in
its recommendation, so nothing is lost - it is recorded against the page
rather than against ten records that do not exist yet.
"""

import logging
from typing import Any

from app.core.config import get_settings
from app.integrations.scholarship_finder import (
    ScholarshipFinderError,
    client_from_settings,
)
from app.usecases.scholarship_finder.schemas import AgentOutcome, Candidate
from app.usecases.scholarship_finder.state import ScholarshipState

logger = logging.getLogger("app.usecases.scholarship_finder.submission")

#: Only REVIEW_REQUIRED asks for a task explicitly. The others are recorded
#: and left to the product's own pipeline, which gives every new candidate a
#: review task through `link_canonical` anyway - asking again for each one
#: would flood the queue this service exists to shorten. Requesting is
#: idempotent regardless, so a task the product already made is not doubled.
REVIEW_ON = frozenset({AgentOutcome.REVIEW_REQUIRED.value})


def _candidates(state: ScholarshipState) -> list[Candidate]:
    return [Candidate.from_dict(item) for item in state.get("candidates") or []]


def _recommendation(state: ScholarshipState, candidates: list[Candidate]) -> dict[str, Any]:
    """The run's full breakdown, for a human reading it afterwards.

    Deliberately not written to `ReviewTask.draft_recommendation`: that
    field is the product's own deterministic output, and overwriting it
    would merge two provenances into one.
    """
    return {
        "workflow_version": state["workflow_version"],
        "page_type": state.get("page_type"),
        "page_fetch_method": state.get("page_fetch_method"),
        "candidate_count": len(candidates),
        "uncertainty_reasons": state.get("uncertainty_reasons") or [],
        "candidates": [
            {
                "title": candidate.title,
                "url": candidate.url,
                "identity_key": candidate.identity_key,
                "outcome": candidate.outcome,
                "official_url": candidate.official_url,
                "official_page_fetched": candidate.official_page_fetched,
                "amount_agrees": candidate.amount_agrees,
                "deadline_agrees": candidate.deadline_agrees,
                "eligibility_rule_count": len(candidate.eligibility_rules),
                "uncertainty_reasons": candidate.uncertainty_reasons,
            }
            for candidate in candidates
        ],
    }


async def submit_to_product(state: ScholarshipState) -> ScholarshipState:
    """Write the run back. Never raises - a submission failure must not
    discard the analysis that produced it.

    The job's own `agent_outputs` row already holds the full result, so a
    failure here is recoverable by resubmitting rather than by re-running
    the whole workflow and re-paying for every model call.
    """
    settings = get_settings()
    candidates = _candidates(state)
    parent_id = str(state["discovery"]["discovery_id"])
    run_id = state["job_id"]
    notes: list[str] = []

    try:
        client = client_from_settings()
    except ScholarshipFinderError as exc:
        return ScholarshipState(
            uncertainty_reasons=[f"submission skipped: {exc}"],
            notes=["product not configured; nothing submitted"],
        )

    if not settings.shadow_mode:
        candidates = await _submit_candidates(client, state, candidates, notes)

    submitted_evidence = 0
    for candidate in candidates:
        if not candidate.discovery_id:
            continue
        submitted_evidence += await _attach(client, state, candidate, run_id, notes)
        await _record_candidate_run(client, state, candidate, notes)
        if not settings.shadow_mode and candidate.outcome in REVIEW_ON:
            await _request_review(client, candidate, notes)

    # Always recorded, in either mode. This is what makes a shadow run
    # auditable from the product's side rather than only from this
    # service's logs.
    await _record_run(
        client,
        discovery_id=parent_id,
        state=state,
        outcome=str(state.get("outcome") or AgentOutcome.MORE_EVIDENCE_REQUIRED.value),
        recommendation=_recommendation(state, candidates),
        notes=notes,
    )

    logger.info(
        "submission_complete",
        extra={
            "job_id": state["job_id"],
            "shadow_mode": settings.shadow_mode,
            "candidate_count": len(candidates),
            "evidence_submitted": submitted_evidence,
        },
    )
    return ScholarshipState(candidates=[c.to_dict() for c in candidates], notes=notes)


async def _submit_candidates(
    client, state: ScholarshipState, candidates: list[Candidate], notes: list[str]
) -> list[Candidate]:
    """Create a discovery for each split candidate, and keep its id.

    Candidates that already have one - an individual page, where the
    candidate *is* the parent discovery - are left alone.
    """
    pending = [c for c in candidates if not c.discovery_id and c.title]
    if not pending:
        return candidates
    payload = [
        {
            "title": candidate.title,
            "url": candidate.url,
            "excerpt": candidate.excerpt,
            "heading": candidate.heading,
        }
        for candidate in pending
    ]
    try:
        result = await client.submit_candidates(
            parent_discovery_id=str(state["discovery"]["discovery_id"]),
            workflow_version=state["workflow_version"],
            candidates=payload,
        )
    except ScholarshipFinderError as exc:
        logger.warning("candidate_submission_failed", extra={"job_id": state["job_id"]})
        notes.append(f"candidate submission failed: {exc}")
        return candidates

    # Matched back by title, which is what the product echoes. Duplicates
    # carry an id too, so a resubmitted run reattaches to the existing rows
    # rather than losing track of them.
    by_title: dict[str, str] = {}
    for item in result.get("results") or []:
        if item.get("discovery_id"):
            by_title[str(item.get("title"))] = str(item["discovery_id"])
    for candidate in pending:
        candidate.discovery_id = by_title.get(candidate.title)
    notes.append(
        f"submitted {result.get('created', 0)} candidate(s), "
        f"{result.get('duplicates', 0)} already present"
    )
    return candidates


async def _attach(
    client, state: ScholarshipState, candidate: Candidate, run_id: str, notes: list[str]
) -> int:
    if not candidate.evidence or not candidate.discovery_id:
        return 0
    try:
        await client.attach_evidence(
            candidate.discovery_id,
            workflow_run_id=run_id,
            workflow_version=state["workflow_version"],
            evidence=candidate.evidence,
            prompt_version=candidate.prompt_versions.get("extract"),
            model=candidate.model,
        )
    except ScholarshipFinderError as exc:
        notes.append(f"evidence submission failed for {candidate.title!r}: {exc}")
        return 0
    return len(candidate.evidence)


async def _record_candidate_run(
    client, state: ScholarshipState, candidate: Candidate, notes: list[str]
) -> None:
    if not candidate.discovery_id or not candidate.outcome:
        return
    await _record_run(
        client,
        discovery_id=candidate.discovery_id,
        state=state,
        outcome=candidate.outcome,
        recommendation={
            "title": candidate.title,
            "official_url": candidate.official_url,
            "amount_agrees": candidate.amount_agrees,
            "deadline_agrees": candidate.deadline_agrees,
            "eligibility_rules": candidate.eligibility_rules,
            "uncertainty_reasons": candidate.uncertainty_reasons,
        },
        notes=notes,
        model=candidate.model,
    )


async def _record_run(
    client,
    *,
    discovery_id: str,
    state: ScholarshipState,
    outcome: str,
    recommendation: dict[str, Any],
    notes: list[str],
    model: str | None = None,
) -> None:
    try:
        await client.record_run(
            discovery_id=discovery_id,
            workflow_version=state["workflow_version"],
            agent_outcome=outcome,
            recommendation=recommendation,
            model=model,
            correlation_id=state["correlation_id"],
        )
    except ScholarshipFinderError as exc:
        notes.append(f"run record failed for {discovery_id}: {exc}")


async def _request_review(client, candidate: Candidate, notes: list[str]) -> None:
    if not candidate.discovery_id:
        return
    try:
        result = await client.request_review(candidate.discovery_id, reason="agent_review_required")
    except ScholarshipFinderError as exc:
        notes.append(f"review request failed for {candidate.title!r}: {exc}")
        return
    if result.get("created"):
        notes.append(f"opened a review task for {candidate.title!r}")
