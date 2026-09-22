"""Deterministic fact extraction, ported from Scholarship Finder.

Kept alongside the model's extraction rather than replaced by it. The
product stores `extracted_facts` and `ai_extracted_facts` separately and
never merges them, because they have different provenance and neither one
confers verification. The agent preserves that: this is the cross-check the
model's output is read against, and the fallback when the model is
unavailable.

Every field is a literal substring match or None. Nothing here infers,
normalizes against a taxonomy, or resolves ambiguity - which is why the
result always carries `needs_human_review`, never a verdict.
"""

import re
from typing import Any

EXTRACTION_VERSION = "extract-v1"

CURRENCY_AMOUNT_PATTERN = re.compile(r"[£$€]\s?[\d][\d,]*(?:\.\d+)?")
MONTH_NAME_DATE_PATTERN = re.compile(
    r"\b(?:January|February|March|April|May|June|July|August|September|October|November|"
    r"December)\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}\b",
    re.IGNORECASE,
)
_LEVEL_KEYWORDS = {
    "doctorate": ("phd", "doctoral", "doctorate"),
    "masters": ("master's", "masters", "msc", "ma ", "mba"),
    "bachelors": ("bachelor's", "bachelors", "undergraduate", "bsc"),
}
_ELIGIBILITY_PHRASES = (
    "all countries",
    "all nationalities",
    "international students",
    "citizens of",
    "residents of",
    "open to",
)


def extract_candidate_facts(raw_title: str | None, raw_excerpt: str | None) -> dict[str, Any]:
    text = " ".join(part for part in (raw_title, raw_excerpt) if part)
    lowered = text.lower()

    funding_mentions = CURRENCY_AMOUNT_PATTERN.findall(text)
    deadline_mentions = MONTH_NAME_DATE_PATTERN.findall(text)

    levels = sorted(
        level
        for level, keywords in _LEVEL_KEYWORDS.items()
        if any(keyword in lowered for keyword in keywords)
    )

    eligibility_snippet = next(
        (phrase for phrase in _ELIGIBILITY_PHRASES if phrase in lowered), None
    )

    return {
        "extraction_version": EXTRACTION_VERSION,
        "needs_human_review": True,
        "funding_mentions": funding_mentions,
        "deadline_mentions": deadline_mentions,
        "level_mentions": levels,
        "eligibility_phrase": eligibility_snippet,
    }
