"""The deterministic half of extraction, ported from Scholarship Finder.

These are the functions that decide agreement and identity. The model never
gets a say in either: it reads pages and proposes facts, and this code
decides whether two values match and whether two candidates are the same
award. Ported verbatim so the agent's answers and the product's cannot
diverge.
"""

import pytest

from app.usecases.scholarship_finder.extraction import (
    EXTRACTION_VERSION,
    extract_candidate_facts,
)
from app.usecases.scholarship_finder.fact_matching import (
    amounts_match,
    deadlines_match,
    parse_amount,
    parse_deadline,
)
from app.usecases.scholarship_finder.normalization import normalize_discovery

# --- identity ---------------------------------------------------------


def test_identity_ignores_case_and_whitespace():
    assert (
        normalize_discovery("  Chevening   Scholarship ").identity_key
        == normalize_discovery("chevening scholarship").identity_key
    )


def test_identity_ignores_word_order():
    """Token-sorted on purpose: a list page and a feed may name the same
    award in different orders, and they are still one award."""
    assert (
        normalize_discovery("Scholarship Chevening").identity_key
        == normalize_discovery("Chevening Scholarship").identity_key
    )


def test_identity_ignores_punctuation():
    assert (
        normalize_discovery("Gates-Cambridge Scholarship!").identity_key
        == normalize_discovery("Gates Cambridge Scholarship").identity_key
    )


def test_different_awards_get_different_identities():
    assert (
        normalize_discovery("Chevening Scholarship").identity_key
        != normalize_discovery("Rhodes Scholarship").identity_key
    )


def test_the_provider_participates_in_identity():
    assert (
        normalize_discovery("Excellence Award", "Oxford").identity_key
        != normalize_discovery("Excellence Award", "Cambridge").identity_key
    )


# --- amounts ----------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("£13,000", ("£", 13000)), ("$1,500.50", ("$", 1500.5)), ("€ 900", ("€", 900))],
)
def test_amounts_parse(raw, expected):
    parsed = parse_amount(raw)
    assert parsed is not None
    assert (parsed[0], float(parsed[1])) == expected


def test_a_near_miss_amount_is_not_agreement():
    """The verification standard's own worst case: a claimed GBP 13,000
    against a real GBP 16,750. Any fuzzy tolerance would call that
    corroboration."""
    assert amounts_match("£13,000", "£16,750") is False


def test_the_same_amount_written_differently_still_matches():
    assert amounts_match("£13,000", "£ 13,000") is True


def test_the_same_number_in_different_currencies_is_not_agreement():
    """A currency-blind comparison would treat GBP 10,000 and USD 10,000 as
    the same award value. They are not."""
    assert amounts_match("£10,000", "$10,000") is False


@pytest.mark.parametrize("raw", ["ten thousand pounds", "10000", "£", ""])
def test_an_unparseable_amount_is_not_agreement(raw):
    """Unparseable is not agreement - but it is not disagreement either.

    It returned False, which reads as "these differ", and False is what
    drives REJECT_RECOMMENDED. None says what is actually true: there is
    nothing here to compare.
    """
    assert parse_amount(raw) is None
    assert amounts_match(raw, raw) is not True
    assert amounts_match(raw, raw) is None


@pytest.mark.parametrize("raw", ["GBP 10,000", "EUR 992", "10,000 GBP", "USD 5,000"])
def test_an_iso_currency_code_is_now_readable(raw):
    """Official pages overwhelmingly write "EUR 992", not "€992". Reading
    symbols only meant sixteen grade-A records in a row reported "no
    evidence for funding" about pages that stated the funding plainly."""
    assert parse_amount(raw) is not None
    assert amounts_match(raw, raw) is True


# --- deadlines --------------------------------------------------------


@pytest.mark.parametrize(
    "raw", ["March 15, 2026", "March 15 2026", "March 15th 2026", "march 15th, 2026"]
)
def test_deadline_spellings_parse_to_one_date(raw):
    parsed = parse_deadline(raw)
    assert parsed is not None
    assert (parsed.year, parsed.month, parsed.day) == (2026, 3, 15)


def test_the_same_deadline_written_differently_matches():
    assert deadlines_match("March 15, 2026", "March 15th 2026") is True


def test_a_different_year_is_not_agreement():
    """The commonest stale-listing failure: last cycle's page, this
    cycle's claim."""
    assert deadlines_match("March 15, 2026", "March 15, 2025") is False


@pytest.mark.parametrize("raw", ["next spring", "soon", "rolling", ""])
def test_an_unparseable_deadline_is_not_agreement(raw):
    """Same distinction as amounts: unreadable is not disagreement."""
    assert parse_deadline(raw) is None
    assert deadlines_match(raw, raw) is None


@pytest.mark.parametrize(
    "raw", ["1 June 2026", "1 Jun 2026", "2026-06-01", "15/03/2026", "1st June 2026"]
)
def test_dates_written_the_other_way_round_are_now_readable(raw):
    """`deadlines_match("1 June 2026", "1 June 2026")` returned False - two
    identical strings called a contradiction, because only "June 1 2026"
    ever parsed. That is a rejection waiting to happen."""
    assert parse_deadline(raw) is not None
    assert deadlines_match(raw, raw) is True


def test_the_same_date_written_two_ways_agrees():
    assert deadlines_match("1 June 2026", "June 1 2026") is True
    assert deadlines_match("1 June 2026", "2026-06-01") is True


# --- deterministic extraction ----------------------------------------


def test_amounts_and_dates_are_pulled_from_prose():
    facts = extract_candidate_facts(
        "PhD Funding", "Worth £10,000 closing March 1, 2026 for doctoral study."
    )

    assert facts["funding_mentions"] == ["£10,000"]
    assert facts["deadline_mentions"] == ["March 1, 2026"]
    assert facts["level_mentions"] == ["doctorate"]


def test_a_trailing_comma_in_prose_is_captured_but_harmless():
    r"""Scholarship Finder's currency pattern ends in `[\d,]*`, so an amount
    followed by a comma keeps it: "£10,000, closing" extracts "£10,000,".

    Ported verbatim rather than corrected, because the agent's
    `extracted_facts` has to equal the product's for the same text.
    Correctness is unaffected - `parse_amount` strips commas before
    comparing - and this pins that, since it is the only reason the quirk
    is safe to carry over.
    """
    facts = extract_candidate_facts("Award", "Worth £10,000, closing soon.")

    assert facts["funding_mentions"] == ["£10,000,"]
    assert amounts_match("£10,000,", "£10,000") is True


def test_extraction_never_returns_a_verdict():
    """It explains what the text says; it never decides whether the
    candidate is real, eligible or publishable."""
    facts = extract_candidate_facts("Award", "£1,000")

    assert facts["needs_human_review"] is True
    assert "verified" not in facts
    assert facts["extraction_version"] == EXTRACTION_VERSION


def test_empty_input_yields_empty_facts_not_an_error():
    facts = extract_candidate_facts(None, None)

    assert facts["funding_mentions"] == []
    assert facts["deadline_mentions"] == []
    assert facts["needs_human_review"] is True


def test_an_eligibility_phrase_is_captured_verbatim():
    facts = extract_candidate_facts("Award", "Open to international students.")

    assert facts["eligibility_phrase"] == "international students"


# --- how official pages actually write money and dates ------------------


def test_funding_written_with_an_iso_code_is_found():
    """A DAAD excerpt reading "a monthly stipend of approximately 992 EUR"
    produced no funding mention at all. Sixteen grade-A records in a row
    then reported "no evidence for funding" about text stating it plainly -
    the extractor matched currency symbols only."""
    facts = extract_candidate_facts(
        "DAAD Award", "Covers a monthly stipend of approximately 992 EUR plus insurance."
    )

    assert facts["funding_mentions"] == ["992 EUR"]


def test_funding_with_the_code_before_the_number_is_found():
    facts = extract_candidate_facts("Award", "The award is worth GBP 10,000 in total.")

    assert facts["funding_mentions"] == ["GBP 10,000"]


def test_a_date_written_day_first_is_found():
    """Only "March 15 2026" matched, so the order most of the world uses -
    and most of the pages we fetch - was not a deadline at all."""
    facts = extract_candidate_facts("Award", "Applications close 15 March 2026.")

    assert facts["deadline_mentions"] == ["15 March 2026"]


def test_an_iso_date_is_found():
    facts = extract_candidate_facts("Award", "Deadline: 2026-06-01 for all applicants.")

    assert facts["deadline_mentions"] == ["2026-06-01"]


def test_a_bare_three_letter_word_is_not_funding():
    """`[A-Z]{3}` next to a number would make "ROOM 101" an amount. The
    word boundary and the digits have to line up."""
    facts = extract_candidate_facts("Award", "Report to ROOM 101 and see THE 2026 handbook.")

    assert facts["funding_mentions"] == []
