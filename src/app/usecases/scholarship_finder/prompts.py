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


def classify_source_page(
    *, title: str | None, excerpt: str | None, page_text: str
) -> dict[str, Any]:
    return {
        "title": title or "",
        "excerpt": excerpt or "",
        "page_text": head(page_text, CLASSIFY_PAGE_CHARS),
    }


def split_list_candidates(*, source_url: str, title: str | None, page_text: str) -> dict[str, Any]:
    return {
        "source_url": source_url,
        "title": title or "",
        "page_text": head(page_text, SPLIT_PAGE_CHARS),
    }


def extract_scholarship_facts(
    *, title: str, excerpt: str | None, page_text: str | None = None
) -> dict[str, Any]:
    payload: dict[str, Any] = {"title": title, "excerpt": excerpt or ""}
    if page_text:
        payload["page_text"] = head(page_text, EXTRACT_PAGE_CHARS)
    return payload


def compare_official_evidence(
    *, title: str, reported_facts: dict[str, Any], official_url: str, official_text: str
) -> dict[str, Any]:
    return {
        "title": title,
        "reported_facts": reported_facts,
        "official_url": official_url,
        "official_page_text": head(official_text, COMPARE_PAGE_CHARS),
    }


def extract_eligibility_requirements(
    *, title: str, source_url: str, page_text: str
) -> dict[str, Any]:
    return {
        "title": title,
        "source_url": source_url,
        "page_text": head(page_text, EXTRACT_PAGE_CHARS),
    }
