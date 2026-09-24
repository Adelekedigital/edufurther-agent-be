"""What each model task receives.

The prompts themselves are not here, and deliberately cannot be: they live
in the AI Router as server-side policy, versioned there and returned on
every response as `prompt_version`. A caller cannot send a prompt - the
router's request model forbids unknown fields - so this module holds the
other half, the `source_data` each registered task is given.

Worth keeping in one place because the size limits are per task and differ
by orders of magnitude. Splitting a list page needs the whole page;
classifying one needs a sample. Truncating in the wrong place is how the
tenth scholarship on a page silently stops existing.
"""

import re
from typing import Any

#: Kept below each task's `max_source_bytes` in the router, with room for
#: the surrounding JSON. Exceeding it is a 422, and the router measures the
#: serialized payload, not the text alone.
CLASSIFY_PAGE_CHARS = 40_000
SPLIT_PAGE_CHARS = 200_000
EXTRACT_PAGE_CHARS = 40_000
COMPARE_PAGE_CHARS = 60_000


def head(text: str | None, limit: int) -> str:
    """Truncate from the front.

    Honest about what it is: a page longer than the limit loses its tail,
    and for `split_list_candidates` that would mean losing awards. That is
    why that task's limit is the page-sized one rather than this being
    cleverer.
    """
    if not text:
        return ""
    return text if len(text) <= limit else text[:limit]


#: Blocks that never carry award prose and routinely dominate the start of
#: a modern page. A 290 KB editorial roundup opened with 31,000 characters
#: of JSON-LD: the first 40,000 characters sent to the classifier
#: contained the word "deadline" zero times, while the page as a whole
#: contained it forty-eight times. The model was reading metadata and
#: reasonably concluded nothing, so two roundups in three fell back to
#: `individual` and were never split.
_BOILERPLATE = re.compile(
    r"<(script|style|noscript|svg|template|iframe)\b[^>]*>.*?</\1>",
    re.IGNORECASE | re.DOTALL,
)
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_WHITESPACE = re.compile(r"[^\S\n]+")


def readable(text: str | None, limit: int) -> str:
    """Drop what cannot be content, then truncate.

    Cheaper than being clever about *where* to truncate, and it addresses
    the real problem: the head of a page is only unrepresentative because
    so much of it is not page. Stripping took one real roundup from 289,000
    characters to 92,000 - and its first 40,000 from no mention of a
    stipend to eight.

    Not a parser. It removes whole elements whose content is never prose
    and collapses runs of spaces; tags themselves are left alone, since
    headings are exactly the signal a classifier wants on a list page.
    """
    if not text:
        return ""
    cleaned = _BOILERPLATE.sub(" ", text)
    cleaned = _COMMENT.sub(" ", cleaned)
    cleaned = _WHITESPACE.sub(" ", cleaned)
    return head(cleaned, limit)


def classify_source_page(
    *, title: str | None, excerpt: str | None, page_text: str
) -> dict[str, Any]:
    return {
        "title": title or "",
        "excerpt": excerpt or "",
        "page_text": readable(page_text, CLASSIFY_PAGE_CHARS),
    }


def split_list_candidates(*, source_url: str, title: str | None, page_text: str) -> dict[str, Any]:
    return {
        "source_url": source_url,
        "title": title or "",
        "page_text": readable(page_text, SPLIT_PAGE_CHARS),
    }


def extract_scholarship_facts(
    *, title: str, excerpt: str | None, page_text: str | None = None
) -> dict[str, Any]:
    payload: dict[str, Any] = {"title": title, "excerpt": excerpt or ""}
    if page_text:
        payload["page_text"] = readable(page_text, EXTRACT_PAGE_CHARS)
    return payload


def compare_official_evidence(
    *, title: str, reported_facts: dict[str, Any], official_url: str, official_text: str
) -> dict[str, Any]:
    return {
        "title": title,
        "reported_facts": reported_facts,
        "official_url": official_url,
        "official_page_text": readable(official_text, COMPARE_PAGE_CHARS),
    }


def extract_eligibility_requirements(
    *, title: str, source_url: str, page_text: str
) -> dict[str, Any]:
    return {
        "title": title,
        "source_url": source_url,
        "page_text": readable(page_text, EXTRACT_PAGE_CHARS),
    }
