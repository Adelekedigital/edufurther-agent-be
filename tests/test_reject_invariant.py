"""One rule, asserted over every path that can reach it.

REJECT_RECOMMENDED is the only outcome that asserts something positive
about the world: *the official page contradicts this claim*. Every other
outcome says what we do or do not know.

Six separate defects in one pilot run all violated the same rule - a gap
in the evidence was treated as evidence against:

  * a page nobody could read was classified and then rejected;
  * a bot-mitigation stub was classified as content;
  * an official page that was simply silent read as disagreement;
  * three failed model calls still produced a rejection;
  * a value the comparators could not parse read as a mismatch;
  * a page was compared against itself.

Each was fixed on its own, and each was found the same way: a real batch
rejecting something real. This file states the rule once, so the seventh
instance fails here instead.

The rule: a rejection requires two values that were actually read, from
two documents that are not the same document.
"""

import pytest

from app.usecases.scholarship_finder.fact_matching import amounts_match, deadlines_match
from app.usecases.scholarship_finder.nodes import (
    _compare_sets,
    _same_page,
    _settle_agreement,
    decide,
)
from app.usecases.scholarship_finder.schemas import AgentOutcome, Candidate

COMPARATORS = [amounts_match, deadlines_match]

#: Values a page might carry that cannot be read as a figure or a date.
#: Each of these produced `False` at some point, and `False` is what
#: reaches REJECT_RECOMMENDED.
UNREADABLE = ["", "rolling", "varies", "see website", "TBC", "next spring", "N/A"]


def candidate() -> Candidate:
    return Candidate(
        title="Award",
        url="https://provider.test/award",
        excerpt="",
        identity_key="award",
        parent_source_url="https://aggregator.test/list",
        parent_discovery_id="d-1",
    )


# --- the comparators ----------------------------------------------------


@pytest.mark.parametrize("comparator", COMPARATORS)
@pytest.mark.parametrize("value", UNREADABLE)
def test_an_unreadable_value_never_reads_as_disagreement(comparator, value):
    """Not `False`, which means "these differ". `None` means "nothing to
    compare", and only one of those can end in a rejection."""
    assert comparator(value, value) is not False
    assert comparator(value, "£10,000") is not False
    assert comparator("£10,000", value) is not False


@pytest.mark.parametrize("comparator", COMPARATORS)
def test_a_missing_side_is_never_disagreement(comparator):
    """An official page that does not mention a deadline has not
    contradicted one. Plenty state funding and link the dates elsewhere."""
    assert _compare_sets([], ["£10,000"], comparator)[0] is None
    assert _compare_sets(["£10,000"], [], comparator)[0] is None
    assert _compare_sets([], [], comparator)[0] is None


@pytest.mark.parametrize("comparator", COMPARATORS)
def test_an_ambiguous_pairing_is_never_disagreement(comparator):
    """A full official page lists several figures. Which one the claim was
    about is not knowable by position, so no pairing can be asserted."""
    agrees, _ = _compare_sets(["£10,000"], ["£1,000", "£5,000", "£50,000"], comparator)

    assert agrees is None


def test_disagreement_still_requires_exactly_one_value_each_side():
    """The guard against over-correcting: a real conflict must still be
    reachable, or the outcome would be dead code."""
    assert _compare_sets(["£13,000"], ["£16,750"], amounts_match)[0] is False
    assert _compare_sets(["1 June 2026"], ["30 April 2026"], deadlines_match)[0] is False


# --- the agreement step -------------------------------------------------


def test_a_model_that_located_nothing_cannot_cause_a_rejection():
    """When the model call fails, `model_pairs` is empty. Three failed
    calls once produced a rejection anyway."""
    c = candidate()
    _settle_agreement(c, {"funding.amount": ([], [], amounts_match)}, {})

    assert c.amount_agrees is None
    assert c.evidence == []


@pytest.mark.parametrize("value", UNREADABLE)
def test_model_located_values_are_held_to_the_same_rule(value):
    """Model-supplied values go through the same comparators, so an
    unreadable one must fail the same way - otherwise the fallback path
    would be a route around the rule."""
    c = candidate()
    _settle_agreement(
        c, {"funding.amount": ([], [], amounts_match)}, {"funding.amount": (value, value)}
    )

    assert c.amount_agrees is not False


# --- the document itself ------------------------------------------------


def test_a_page_is_never_its_own_corroboration():
    """Sixteen records were verified against themselves before this was
    noticed - and one of them reported that the official page corroborated
    both funding and deadline."""
    url = "https://www2.daad.de/db/21148-scholarship-database?detail=57378178"

    assert _same_page(url, url) is True
    assert _same_page(url, url + "#section") is True
    assert _same_page(url.replace("https://", "https://www."), url) is True


def test_distinct_pages_are_still_distinct():
    """The query string is significant: DAAD addresses every award through
    one path, so ignoring it would collapse a whole database into a page."""
    base = "https://www2.daad.de/db/21148-scholarship-database?detail="

    assert _same_page(base + "1", base + "2") is False
    assert _same_page("https://a.test/x", "https://a.test/y") is False


# --- the decision --------------------------------------------------------


async def test_an_unreadable_page_cannot_reach_a_rejection():
    """Whatever a model made of a stub, the page was never read."""
    result = await decide(
        {
            "job_id": "job-1",
            "page_usable": False,
            # The most dangerous classification, because it short-circuits.
            "page_type": "not_a_scholarship",
            "candidates": [],
        }
    )

    assert result["outcome"] == AgentOutcome.MORE_EVIDENCE_REQUIRED.value


@pytest.mark.parametrize("page_type", ["individual", "list", "aggregator", "not_a_scholarship"])
async def test_no_page_type_can_reject_an_unreadable_page(page_type):
    """Across every classification the model can return."""
    result = await decide(
        {"job_id": "job-1", "page_usable": False, "page_type": page_type, "candidates": []}
    )

    assert result["outcome"] != AgentOutcome.REJECT_RECOMMENDED.value


async def test_a_readable_page_holding_no_award_still_rejects():
    """The rule is about unread evidence, not about never rejecting. A page
    we read and understood to hold no award should say so."""
    result = await decide(
        {
            "job_id": "job-1",
            "page_usable": True,
            "page_type": "not_a_scholarship",
            "candidates": [],
        }
    )

    assert result["outcome"] == AgentOutcome.REJECT_RECOMMENDED.value
