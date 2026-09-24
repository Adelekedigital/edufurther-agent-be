"""What actually reaches the model, once boilerplate is out of the way.

Two roundups in three fell back to `individual` and were never split. The
pages were fetched fine and the model answered fine; it was simply reading
the wrong 40,000 characters. A 290 KB editorial page opened with 31,000
characters of JSON-LD, so the sample sent to the classifier mentioned
"deadline" zero times while the page mentioned it forty-eight.
"""

import pytest

from app.usecases.scholarship_finder.prompts import (
    CLASSIFY_PAGE_CHARS,
    classify_source_page,
    compare_official_evidence,
    readable,
    split_list_candidates,
)

PAGE_WITH_METADATA = (
    '<script type="application/ld+json">{"@context":"https://schema.org",'
    + ('"filler":"x",' * 4000)
    + '"headline":"Scholarships"}</script>'
    "<h2>DAAD Award</h2><p>A monthly stipend of 992 EUR. Deadline 15 March 2026.</p>"
)


def test_a_metadata_block_does_not_crowd_out_the_page():
    """The whole defect, in one assertion: the award text is past the
    truncation point until the JSON-LD is removed."""
    assert "stipend" not in PAGE_WITH_METADATA[:CLASSIFY_PAGE_CHARS]

    sent = readable(PAGE_WITH_METADATA, CLASSIFY_PAGE_CHARS)

    assert "stipend" in sent
    assert "992 EUR" in sent
    assert "15 March 2026" in sent


@pytest.mark.parametrize("tag", ["script", "style", "noscript", "svg", "template", "iframe"])
def test_elements_that_never_carry_prose_are_dropped(tag):
    page = f"<{tag}>noise noise noise</{tag}><p>Award worth 992 EUR</p>"

    sent = readable(page, 1_000)

    assert "noise" not in sent
    assert "992 EUR" in sent


def test_headings_survive():
    """Tags are left alone on purpose - a run of award headings is exactly
    the signal that tells a list page from an individual one."""
    sent = readable("<h2>1. First Award</h2><h2>2. Second Award</h2>", 1_000)

    assert "<h2>" in sent
    assert "First Award" in sent and "Second Award" in sent


def test_an_html_comment_is_dropped():
    assert "hidden" not in readable("<!-- hidden -->visible", 1_000)


def test_runs_of_spaces_collapse_but_lines_do_not():
    """Newlines carry structure a model can use; runs of spaces do not."""
    sent = readable("a     b\n\nc", 1_000)

    assert "a b" in sent
    assert "\n" in sent


@pytest.mark.parametrize("value", [None, ""])
def test_nothing_in_nothing_out(value):
    assert readable(value, 100) == ""


def test_the_limit_is_still_enforced():
    assert len(readable("<p>" + ("x" * 5_000) + "</p>", 100)) == 100


def test_every_task_that_receives_a_page_gets_the_cleaned_one():
    """A fix applied to one task and not the others would leave the split
    reading metadata while the classifier reads the page."""
    payloads = [
        classify_source_page(title="t", excerpt="e", page_text=PAGE_WITH_METADATA)["page_text"],
        split_list_candidates(source_url="https://x.test", title="t", page_text=PAGE_WITH_METADATA)[
            "page_text"
        ],
        compare_official_evidence(
            title="t",
            reported_facts={},
            official_url="https://x.test",
            official_text=PAGE_WITH_METADATA,
        )["official_page_text"],
    ]

    for sent in payloads:
        assert "stipend" in sent, "a task is still being sent the raw page"
