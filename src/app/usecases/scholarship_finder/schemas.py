"""Types the scholarship workflow carries between nodes.

Plain dataclasses with `to_dict`, not Pydantic models, because every one of
these ends up inside a LangGraph checkpoint and has to round-trip through
JSON to be resumable.
"""

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class PageType(StrEnum):
    INDIVIDUAL = "individual"
    LIST = "list"
    AGGREGATOR = "aggregator"
    NOT_A_SCHOLARSHIP = "not_a_scholarship"


class AgentOutcome(StrEnum):
    """The four outcomes, and only these four.

    `AUTO_CHECK_ELIGIBLE` says "complete enough for the product's
    deterministic gates to consider". It is not a publication command, and
    the product records it without acting on it.
    """

    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    MORE_EVIDENCE_REQUIRED = "MORE_EVIDENCE_REQUIRED"
    AUTO_CHECK_ELIGIBLE = "AUTO_CHECK_ELIGIBLE"
    REJECT_RECOMMENDED = "REJECT_RECOMMENDED"


class SourceType(StrEnum):
    OFFICIAL_PAGE = "official_page"
    OFFICIAL_DOCUMENT = "official_document"
    AGGREGATOR = "aggregator"
    MARKETPLACE = "marketplace"
    BLOG_LIST = "blog_list"
    SEARCH_RESULT = "search_result"


class ScaleType(StrEnum):
    """How an academic requirement is expressed, in its own terms.

    Never converted between. A 3.0/4.0 is not a 75%, a 2:1 is not a GPA,
    and inventing an equivalence makes an applicant's real result
    unrecoverable - so the scale is recorded and comparison is left to
    whoever has an authoritative mapping.
    """

    GPA_4_0 = "gpa_4_0"
    GPA_5_0 = "gpa_5_0"
    PERCENTAGE = "percentage"
    UK_HONOURS = "uk_honours"
    LETTER_GRADE = "letter_grade"
    QUALITATIVE = "qualitative"
    NOT_STATED = "not_stated"
    UNKNOWN = "unknown"


@dataclass
class EligibilityRule:
    """One stated requirement, in the source's own words and scale.

    `equivalency_status` defaults to `not_converted` and nothing in this
    build sets anything else: converting without an authoritative mapping
    is the failure this field exists to make visible.
    """

    requirement_type: str
    raw_value: str
    source_wording: str
    scale_type: str = ScaleType.UNKNOWN.value
    scale_max: float | None = None
    measurement_basis: str = "unknown"
    scope: dict[str, list[str]] = field(default_factory=lambda: {"programmes": [], "countries": []})
    evidence_url: str | None = None
    evidence_excerpt: str | None = None
    equivalency_status: str = "not_converted"
    verified_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Evidence:
    """One claim, and where it came from.

    Shaped to match the product's `discovery_evidence` contract exactly, so
    submission is a direct mapping rather than a translation that could
    quietly drop a field.
    """

    claim_path: str
    value: Any
    source_url: str
    source_type: str
    observed_at: str
    excerpt: str | None = None
    fetch_method: str | None = None
    confidence: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Candidate:
    """One award, carried through the workflow accumulating what we learn.

    Every provenance field spec §7 requires lives here, including the ones
    that only matter when something goes wrong: `uncertainty_reasons`
    explains why a candidate did not reach a confident outcome, which is
    what makes "we do not know" a usable result for a reviewer rather than
    an empty field.
    """

    title: str
    identity_key: str
    #: The list page this was extracted from. Never the candidate's own URL.
    parent_source_url: str
    parent_discovery_id: str
    url: str | None = None
    excerpt: str | None = None
    heading: str | None = None
    #: The product's discovery for this candidate, once one exists. Set
    #: from the start for an individual page - where the candidate *is* the
    #: parent discovery - and only after submission for a split candidate.
    #: Evidence cannot be attached without it, which is why shadow mode
    #: records a list page's breakdown against the parent instead.
    discovery_id: str | None = None

    #: Kept separate, never merged - the product's own rule, for the same
    #: reason: different provenance, and neither confers verification.
    deterministic_facts: dict[str, Any] = field(default_factory=dict)
    model_facts: dict[str, Any] = field(default_factory=dict)

    official_url: str | None = None
    official_page_fetched: bool = False
    official_fetch_method: str | None = None

    #: Deterministic agreement, computed by fact_matching. None means "not
    #: applicable" - the candidate asserted nothing to compare - which is
    #: deliberately distinct from False.
    amount_agrees: bool | None = None
    deadline_agrees: bool | None = None
    contradictions: list[str] = field(default_factory=list)

    eligibility_rules: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)

    outcome: str | None = None
    uncertainty_reasons: list[str] = field(default_factory=list)

    #: Keyed by task, because the router picks a model per call and may
    #: fall back on one and not another - so "the model that produced this
    #: candidate" is not a single answer. Written in step with
    #: `prompt_versions`: a prompt version without the model that ran it
    #: answers half the question, and the half it answers is the half that
    #: rarely changes.
    prompt_versions: dict[str, str] = field(default_factory=dict)
    models: dict[str, str] = field(default_factory=dict)

    @property
    def model(self) -> str | None:
        """The one model name the product's single column should hold.

        Extraction first, because the facts a row asserts are what a later
        accuracy question is actually about - and it matches the existing
        choice of `prompt_versions["extract"]` for the same columns. The
        rest are a fallback so a candidate that never reached extraction
        still attributes to something.
        """
        for task in ("extract", "compare", "eligibility", "split", "classify"):
            if self.models.get(task):
                return self.models[task]
        return None

    def to_dict(self) -> dict[str, Any]:
        # `model` is derived, so `asdict` omits it. Added back because the
        # dict form is what crosses into the checkpoint and the product,
        # and a reader there should not have to know it is a property.
        return asdict(self) | {"model": self.model}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Candidate":
        known = {key: data[key] for key in data if key in cls.__dataclass_fields__}
        return cls(**known)
