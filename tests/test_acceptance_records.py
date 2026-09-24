"""The acceptance records from the build spec, run through the whole graph.

Every defect found in the first real pilot run got through a suite of four
hundred passing tests, because those tests exercised the parts and this one
exercises the path. They also used tidy fixtures: `£5,000`, `March 1 2026`,
a title with no list position, a URL already canonical. Real pages are not
tidy, and each of those conveniences hid a fault.

So the fixtures here are deliberately awkward in the ways real pages are:
money written as an ISO code, dates written day-first, headings carrying
the roundup's numbering, an official page that lists several figures. A
fixture that reads oddly is usually a fixture that earned its place.

One rule holds across all of them and is asserted separately in
`test_reject_invariant.py`: REJECT_RECOMMENDED asserts that an official
page contradicts a claim, so it must never be reachable from evidence we
failed to read.
"""

from typing import Any

import pytest

from app.integrations.ai_router import AITask
from app.usecases.scholarship_finder.schemas import AgentOutcome
from tests.test_scholarship_workflow_integration import (
    LIST_URL,
    discovery,
    run,
    split_output,
    wire,  # noqa: F401  (fixture)
)

# A body large enough to count as a page. The usability check treats a thin
# response as a failed fetch, which is what stopped a bot-mitigation stub
# being classified as content.
PAGE = "<html><body>" + ("real page content " * 200) + "</body></html>"


def answers(**overrides: Any) -> dict[AITask, Any]:
    """The model saying nothing useful, so each test adds only what it needs."""
    base = {
        AITask.classify_source_page: {"page_type": "individual", "evidence": []},
        AITask.extract_scholarship_facts: {"candidate": {}, "evidence": []},
        AITask.compare_official_evidence: {
            "comparisons": [],
            "contradictions": [],
            "evidence": [],
        },
        AITask.extract_eligibility_requirements: {"rules": [], "evidence": []},
    }
    return base | overrides


# --- 1. an individual page ---------------------------------------------


async def test_an_individual_page_yields_one_candidate(wire):
    wire(
        answers=answers(),
        record=discovery(
            raw_title="Example Doctoral Award",
            raw_excerpt="Worth 992 EUR monthly, closing 15 March 2026.",
            source_url="https://provider.test/award",
            approved_domains=["provider.test"],
            authority_grade="A",
        ),
        source_text=PAGE,
    )

    result = await run()

    assert result["page_type"] == "individual"
    assert len(result["candidates"]) == 1


async def test_an_individual_page_cannot_corroborate_itself(wire):
    """Its candidate URL *is* its discovery URL, so the only available
    "official page" is the page we already have. Sixteen grade-A records
    were verified this way before it was noticed."""
    wire(
        answers=answers(),
        record=discovery(
            raw_title="Example Doctoral Award",
            raw_excerpt="Worth 992 EUR monthly.",
            source_url="https://provider.test/award",
            approved_domains=["provider.test"],
            authority_grade="A",
        ),
        source_text=PAGE,
        official="Worth 992 EUR monthly.",
    )

    result = await run()

    candidate = result["candidates"][0]
    assert candidate["official_url"] is None
    assert result["outcome"] == AgentOutcome.MORE_EVIDENCE_REQUIRED.value


# --- 2. a ten-item list page -------------------------------------------


async def test_a_list_page_becomes_one_candidate_per_award(wire):
    wire(
        answers=answers(
            **{
                AITask.classify_source_page: {"page_type": "list", "evidence": []},
                AITask.split_list_candidates: split_output(10),
            }
        ),
        source_text=PAGE,
    )

    result = await run()

    assert len(result["candidates"]) == 10
    assert len({c["identity_key"] for c in result["candidates"]}) == 10
    # Lineage back to the page each came from - what the product records as
    # split_from_discovery_id.
    assert all(c["parent_source_url"] == LIST_URL for c in result["candidates"])


async def test_a_roundups_numbering_does_not_reach_the_identity_key(wire):
    """The real split produced fifty good candidates and fifty broken keys:
    "1. Chevening" and "17. Chevening" are one award, and keyed as two."""
    numbered = {
        "candidates": [
            {
                "title": f"{index}. Chevening Scholarships",
                "url": f"https://official.test/award-{index}",
                "excerpt": "Worth GBP 10,000.",
                "heading": f"{index}. Chevening Scholarships",
            }
            for index in (1, 17)
        ],
        "evidence": [],
    }
    wire(
        answers=answers(
            **{
                AITask.classify_source_page: {"page_type": "list", "evidence": []},
                AITask.split_list_candidates: numbered,
            }
        ),
        source_text=PAGE,
    )

    result = await run()

    keys = {c["identity_key"] for c in result["candidates"]}
    assert len(keys) == 1, f"one award keyed {len(keys)} ways: {keys}"
    assert not any(c["title"][0].isdigit() for c in result["candidates"])


# --- 3. money and dates as real pages write them ------------------------


async def test_funding_stated_as_an_iso_code_is_corroborated(wire):
    """ "992 EUR" is how official pages overwhelmingly write it. Reading
    only `€992` meant sixteen records reported "no evidence for funding"
    about text that stated the funding plainly."""
    wire(
        answers=answers(
            **{
                AITask.classify_source_page: {"page_type": "list", "evidence": []},
                AITask.split_list_candidates: {
                    "candidates": [
                        {
                            "title": "Cotutelle Award",
                            "url": "https://official.test/cotutelle",
                            "excerpt": "A monthly stipend of 992 EUR.",
                            "heading": "Cotutelle Award",
                        }
                    ],
                    "evidence": [],
                },
            }
        ),
        source_text=PAGE,
        official="The award pays a monthly stipend of 992 EUR to doctoral candidates.",
    )

    result = await run()

    assert result["candidates"][0]["amount_agrees"] is True


async def test_a_deadline_written_day_first_is_corroborated(wire):
    """`deadlines_match("1 June 2026", "1 June 2026")` returned False, because
    only "June 1 2026" ever parsed - two identical strings called a conflict."""
    wire(
        answers=answers(
            **{
                AITask.classify_source_page: {"page_type": "list", "evidence": []},
                AITask.split_list_candidates: {
                    "candidates": [
                        {
                            "title": "Summer Award",
                            "url": "https://official.test/summer",
                            "excerpt": "Applications close 15 March 2026.",
                            "heading": "Summer Award",
                        }
                    ],
                    "evidence": [],
                },
            }
        ),
        source_text=PAGE,
        official="Applications close 15 March 2026 for all applicants.",
    )

    result = await run()

    assert result["candidates"][0]["deadline_agrees"] is True


# --- 4. conflicting evidence -------------------------------------------


async def test_a_genuine_conflict_is_rejected(wire):
    """One value each side, and they differ. This is what the outcome is
    for, and the guard against over-correcting the other five fixes."""
    wire(
        answers=answers(
            **{
                AITask.classify_source_page: {"page_type": "list", "evidence": []},
                AITask.split_list_candidates: {
                    "candidates": [
                        {
                            "title": "Contested Award",
                            "url": "https://official.test/contested",
                            "excerpt": "Applications close 1 March 2026.",
                            "heading": "Contested Award",
                        }
                    ],
                    "evidence": [],
                },
            }
        ),
        source_text=PAGE,
        official="Applications close 30 April 2026.",
    )

    result = await run()

    assert result["candidates"][0]["deadline_agrees"] is False
    assert result["outcome"] == AgentOutcome.REJECT_RECOMMENDED.value


async def test_several_figures_on_the_official_page_is_not_a_conflict(wire):
    """A full page lists a stipend, a travel allowance and an insurance
    contribution. Comparing the first of each side pairs two numbers that
    were never about the same thing - four of five rejections in one batch
    came from exactly that."""
    wire(
        answers=answers(
            **{
                AITask.classify_source_page: {"page_type": "list", "evidence": []},
                AITask.split_list_candidates: {
                    "candidates": [
                        {
                            "title": "Multi Figure Award",
                            "url": "https://official.test/multi",
                            "excerpt": "Worth 992 EUR monthly.",
                            "heading": "Multi Figure Award",
                        }
                    ],
                    "evidence": [],
                },
            }
        ),
        source_text=PAGE,
        official="Travel allowance GBP 1,000. Insurance 500 EUR. Stipend 992 EUR monthly.",
    )

    result = await run()

    candidate = result["candidates"][0]
    assert candidate["amount_agrees"] is True, "a claim present on the page is corroborated"
    assert result["outcome"] != AgentOutcome.REJECT_RECOMMENDED.value


# --- 5. a page that could not be read ----------------------------------


async def test_a_page_that_could_not_be_read_is_never_a_rejection(wire):
    """A bot-mitigation stub is a valid 200 carrying no page. It reached a
    model, was reasonably classified `not_a_scholarship`, and rejected ten
    real awards in one batch."""
    wire(
        answers=answers(
            **{AITask.classify_source_page: {"page_type": "not_a_scholarship", "evidence": []}}
        ),
        # Under the padding threshold in `wire`, so it stays a stub.
        source_text="<html>x</html>",
    )

    result = await run()

    assert result["outcome"] == AgentOutcome.MORE_EVIDENCE_REQUIRED.value
    assert any("could not be retrieved" in n for n in result["notes"])


async def test_a_page_that_was_read_and_holds_no_award_still_says_so(wire):
    """The other side of it: this must not over-correct into asking for
    more evidence about pages we understood perfectly well."""
    wire(
        answers=answers(
            **{AITask.classify_source_page: {"page_type": "not_a_scholarship", "evidence": []}}
        ),
        source_text=PAGE,
    )

    result = await run()

    assert result["outcome"] == AgentOutcome.REJECT_RECOMMENDED.value


# --- 6. one bad candidate must not discard the rest ---------------------


async def test_a_candidate_without_a_link_does_not_cost_the_others(wire):
    """Inline roundups name awards in prose without linking them - 46 of 50
    in the first real split had no URL. Each is handled in isolation."""
    mixed = {
        "candidates": [
            {
                "title": "Linked Award",
                "url": "https://official.test/linked",
                "excerpt": "Worth 992 EUR.",
                "heading": "Linked Award",
            },
            {
                "title": "Unlinked Award",
                "url": None,
                "excerpt": "Worth GBP 5,000.",
                "heading": "Unlinked Award",
            },
        ],
        "evidence": [],
    }
    wire(
        answers=answers(
            **{
                AITask.classify_source_page: {"page_type": "list", "evidence": []},
                AITask.split_list_candidates: mixed,
            }
        ),
        source_text=PAGE,
        official="Worth 992 EUR monthly.",
    )

    result = await run()

    assert len(result["candidates"]) == 2
    unlinked = next(c for c in result["candidates"] if c["title"] == "Unlinked Award")
    assert any("no link of its own" in r for r in unlinked["uncertainty_reasons"])


# --- 7. a model failure never becomes a verdict -------------------------


@pytest.mark.parametrize(
    "failing",
    [
        AITask.extract_scholarship_facts,
        AITask.compare_official_evidence,
        AITask.extract_eligibility_requirements,
    ],
)
async def test_a_failed_model_call_never_produces_a_rejection(wire, failing):
    """Three calls failed with REQUEST_ID_COLLISION and the run still
    rejected a real award, on a deadline nobody had read."""
    from app.integrations.ai_router import AIRouterOutcome
    from tests.test_scholarship_workflow_integration import router_response

    wire(
        answers=answers(
            **{
                AITask.classify_source_page: {"page_type": "list", "evidence": []},
                AITask.split_list_candidates: split_output(1),
                failing: router_response(None, outcome=AIRouterOutcome.provider_unavailable),
            }
        ),
        source_text=PAGE,
        # Matches what `split_output(1)` claims, so any rejection here
        # would come from the failed call rather than a real conflict.
        official="Worth £1,000 closing March 1, 2026.",
    )

    result = await run()

    assert result["outcome"] != AgentOutcome.REJECT_RECOMMENDED.value
