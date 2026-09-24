"""The workflow's steps.

Two rules hold throughout, and most of the care in this module is spent on
them:

**The model proposes; deterministic code decides.** A model classifies a
page, splits a list and reads facts out of prose. It never decides whether
two values agree, whether a candidate is a duplicate, or which outcome
applies. Those are `fact_matching`, `normalization` and `policies`.

**One bad item must not discard the batch.** A list page yields many
candidates and any of them can fail. Each is handled in isolation, with the
failure recorded on that candidate as an uncertainty reason rather than
raised - Scholarship Finder lost a whole harvest to one bad row once.
"""

import logging
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.core.config import get_settings
from app.infra.database import get_sessionmaker
from app.integrations.ai_router import (
    AIRouterClient,
    AIRouterError,
    AIRouterRequest,
    AIRouterResponse,
    AITask,
    client_from_settings,
)
from app.integrations.scholarship_finder import (
    client_from_settings as sf_client_from_settings,
)
from app.tools.retrieval import MIN_USABLE_BODY_CHARS, fetch_official_page, fetch_page
from app.usecases.scholarship_finder import prompts
from app.usecases.scholarship_finder.extraction import extract_candidate_facts
from app.usecases.scholarship_finder.fact_matching import amounts_match, deadlines_match
from app.usecases.scholarship_finder.normalization import normalize_discovery
from app.usecases.scholarship_finder.policies import (
    decide_outcome,
    outcome_for_page_type,
    roll_up,
)
from app.usecases.scholarship_finder.schemas import (
    AgentOutcome,
    Candidate,
    EligibilityRule,
    Evidence,
    PageType,
    ScaleType,
    SourceType,
)
from app.usecases.scholarship_finder.state import ScholarshipState

logger = logging.getLogger("app.usecases.scholarship_finder")

FEATURE_ID = "scholarship_verification"

#: A page describing more awards than this is far likelier to be a
#: decomposition failure - or an injected one - than a real listing, and
#: each candidate carries a real per-item cost in model calls and fetches.
MAX_CANDIDATES_PER_PAGE = 50

#: Official page text between `fetch_official` and `compare_evidence`,
#: keyed by (job, candidate). Deliberately outside graph state: LangGraph
#: checkpoints state after every node, and a 2 MB page body per candidate
#: would be rewritten into storage on each one. Losing it on a restart is
#: fine - the resumed run re-fetches.
_official_text: dict[tuple[str, str], str] = {}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _candidates(state: ScholarshipState) -> list[Candidate]:
    return [Candidate.from_dict(item) for item in state.get("candidates") or []]


def _dump(candidates: list[Candidate]) -> list[dict[str, Any]]:
    return [candidate.to_dict() for candidate in candidates]


async def _ask(
    client: AIRouterClient, task: AITask, *, state: ScholarshipState, key: str, source_data: dict
) -> AIRouterResponse:
    """One model call, with a deterministic idempotency key.

    The key is derived from the job and the step rather than random, so a
    retried node replays the router's stored response instead of paying for
    the same call twice.

    The `X-Request-ID` seeds the router's Langfuse trace and is the only
    caller-controlled input to it, so it keeps the correlation id as a
    prefix - but it has to be unique per call. The router treats one request
    id as belonging to one idempotency key, and sending the job's id for
    every step meant only the first model call in a job ever succeeded.
    """
    return await client.execute(
        AIRouterRequest(
            task=task,
            feature_id=FEATURE_ID,
            correlation_id=state["correlation_id"],
            idempotency_key=f"{state['job_id']}:{key}",
            # Prefixed with the correlation id so a run's traces still group,
            # but suffixed per step: the router binds one X-Request-ID to one
            # idempotency key, so reusing the job's id across calls made every
            # request after the first a 409 REQUEST_ID_COLLISION.
            request_id=f"{state['correlation_id']}:{key}",
            source_data=source_data,
        )
    )


# --- 1. load -----------------------------------------------------------


async def load_discovery(state: ScholarshipState) -> ScholarshipState:
    """Read the product's record.

    Over HTTP, never from its database. The Source's approved domains come
    with it, so the agent fetches under the same policy the product would
    rather than a parallel one of its own.
    """
    client = sf_client_from_settings()
    discovery = await client.get_discovery(state["input_reference"])
    return ScholarshipState(
        discovery=discovery,
        approved_domains=list(discovery.get("approved_domains") or []),
        source_url=discovery["source_url"],
        notes=[f"loaded discovery {discovery['discovery_id']}"],
    )


# --- 2. fetch ----------------------------------------------------------


async def fetch_source(state: ScholarshipState) -> ScholarshipState:
    """Retrieve the page the discovery points at.

    Direct first, Jina on failure. A domain or SSRF refusal propagates
    rather than falling back - Jina fetches from its own infrastructure, so
    falling back after the guard said no would step around the guard.
    """
    async with get_sessionmaker()() as db:
        page = await fetch_page(
            db, state["source_url"], state["approved_domains"], job_id=state["job_id"]
        )
    usable = page.status_code == 200 and len(page.text.strip()) >= MIN_USABLE_BODY_CHARS
    note = f"fetched source via {page.fetch_method} ({page.byte_length} bytes)"
    if not usable:
        note += " - not a usable page"
    return ScholarshipState(
        page_text=page.text,
        page_fetch_method=page.fetch_method,
        page_usable=usable,
        notes=[note],
    )


# --- 3. classify -------------------------------------------------------


async def classify_page(state: ScholarshipState) -> ScholarshipState:
    """Decide what kind of page this is.

    The branch that makes list decomposition possible at all: the product
    has no notion of a page holding many awards, so nothing downstream can
    split one unless this step identifies it.
    """
    discovery = state["discovery"]
    client = client_from_settings()
    response = await _ask(
        client,
        AITask.classify_source_page,
        state=state,
        key="classify",
        source_data=prompts.classify_source_page(
            title=discovery.get("raw_title"),
            excerpt=discovery.get("raw_excerpt"),
            page_text=state.get("page_text", ""),
        ),
    )
    if not response.completed or not response.output:
        # Treated as an individual page rather than failing the run: a
        # single candidate that a human reviews is a worse-but-usable
        # result, where an abandoned job is no result at all.
        return ScholarshipState(
            page_type=PageType.INDIVIDUAL.value,
            uncertainty_reasons=[f"page classification unavailable ({response.outcome.value})"],
            notes=["classification fell back to individual"],
        )
    page_type = str(response.output.get("page_type"))
    if page_type not in {member.value for member in PageType}:
        # Validated against the enum rather than trusted. An unrecognised
        # value used to fall through the router to `single_candidate`,
        # which was fail-safe by accident - and "not_a_scholarship" is a
        # short-circuit straight to REJECT_RECOMMENDED, so the string this
        # node returns has real authority. It should be one we recognise.
        return ScholarshipState(
            page_type=PageType.INDIVIDUAL.value,
            uncertainty_reasons=[f"unrecognised page type {page_type!r}"],
            notes=["classification fell back to individual"],
        )
    return ScholarshipState(
        page_type=page_type,
        notes=[f"classified as {page_type}"],
    )


# --- 4. split ----------------------------------------------------------


def _new_candidate(state: ScholarshipState, *, title: str, **extra: Any) -> Candidate:
    discovery = state["discovery"]
    return Candidate(
        title=title,
        identity_key=normalize_discovery(title).identity_key,
        parent_source_url=state["source_url"],
        parent_discovery_id=str(discovery["discovery_id"]),
        **extra,
    )


async def single_candidate(state: ScholarshipState) -> ScholarshipState:
    """An individual page is one candidate: the discovery itself."""
    discovery = state["discovery"]
    candidate = _new_candidate(
        state,
        title=discovery.get("raw_title") or "",
        url=state["source_url"],
        excerpt=discovery.get("raw_excerpt"),
        # The candidate *is* this discovery, so it already has the
        # product's id - which is what lets evidence attach even in shadow
        # mode, where nothing new is created.
        discovery_id=str(discovery["discovery_id"]),
    )
    return ScholarshipState(candidates=[candidate.to_dict()], notes=["single candidate"])


#: A list position at the start of a heading: "1.", "17)", "#3", "3 -".
_LIST_ORDINAL = re.compile(r"^\s*(?:#\s*\d{1,3}\s+|\d{1,3}\s*[.)\];:-]\s+)")


def _strip_list_ordinal(title: str) -> str:
    """Drop the position a roundup gave an award, keep the award.

    The number is about the page, not the scholarship, and it reaches
    further than it looks: the identity key is derived from the title, so
    "1. Chevening Scholarships" keyed as `1|chevening|scholarships` while
    the same award at position 17 on another page keyed as
    `17|chevening|scholarships`. Two keys, one scholarship - and the
    identity key is what makes cross-source dedupe, corroboration counting
    and supersedes lineage work at all.

    It fails silently, which is the dangerous part. Fifty clean-looking
    rows are created and nothing errors; the duplication only surfaces
    later as a review queue full of the same award listed several times.

    Deliberately conservative. A trailing separator and whitespace are
    required, so "2026 Chevening Scholarships" and "50 Fully Funded
    Awards" keep their numbers - those are part of the name.
    """
    stripped = _LIST_ORDINAL.sub("", title).strip()
    # Never return empty: a heading that was *only* a number is better kept
    # verbatim than dropped, since the candidate would vanish silently.
    return stripped or title.strip()


async def split_candidates(state: ScholarshipState) -> ScholarshipState:
    """Separate a list page into one candidate per named award."""
    discovery = state["discovery"]
    client = client_from_settings()
    try:
        response = await _ask(
            client,
            AITask.split_list_candidates,
            state=state,
            key="split",
            source_data=prompts.split_list_candidates(
                source_url=state["source_url"],
                title=discovery.get("raw_title"),
                page_text=state.get("page_text", ""),
            ),
        )
    except AIRouterError as exc:
        # Falls back to treating the page as one candidate. A reviewer
        # seeing one under-decomposed record can act on it; a failed job
        # leaves the page unprocessed entirely.
        logger.warning("split_failed", extra={"job_id": state["job_id"], "error": str(exc)})
        fallback = await single_candidate(state)
        return ScholarshipState(
            candidates=fallback["candidates"],
            uncertainty_reasons=[f"list splitting failed: {exc}"],
        )

    if not response.completed or not response.output:
        fallback = await single_candidate(state)
        return ScholarshipState(
            candidates=fallback["candidates"],
            uncertainty_reasons=[f"list splitting unavailable ({response.outcome.value})"],
        )

    items = response.output.get("candidates") or []
    truncated = 0
    if len(items) > MAX_CANDIDATES_PER_PAGE:
        # Each candidate costs one extract call, one official-page fetch,
        # one compare call and one eligibility call. Uncapped, an injected
        # page that induces thousands of candidates turns one discovery
        # into thousands of model calls and fetches - and blowing the
        # workflow ceiling raises TimeoutError, which is retryable, so it
        # would do it up to five times.
        truncated = len(items) - MAX_CANDIDATES_PER_PAGE
        items = items[:MAX_CANDIDATES_PER_PAGE]
    candidates = [
        _new_candidate(
            state,
            title=_strip_list_ordinal(str(item.get("title") or "")),
            url=str(item["url"]) if item.get("url") else None,
            excerpt=item.get("excerpt"),
            heading=item.get("heading"),
            prompt_versions={"split": response.prompt_version or ""},
        )
        for item in items
        if _strip_list_ordinal(str(item.get("title") or ""))
    ]
    if not candidates:
        # A list page that yields nothing is a real answer, not a crash -
        # but it is also not a candidate, so say so rather than inventing
        # one from the page title.
        return ScholarshipState(
            candidates=[],
            uncertainty_reasons=["page classified as a list but no candidates were extracted"],
        )
    notes = [f"split into {len(candidates)} candidates"]
    uncertainty = (
        [f"page yielded more than {MAX_CANDIDATES_PER_PAGE} candidates; {truncated} not processed"]
        if truncated
        else []
    )
    return ScholarshipState(
        candidates=_dump(candidates), notes=notes, uncertainty_reasons=uncertainty
    )


# --- 5. dedupe ---------------------------------------------------------


async def dedupe_candidates(state: ScholarshipState) -> ScholarshipState:
    """Collapse candidates sharing an identity key.

    Deterministic, using the product's own key derivation - a model asked
    "are these the same award?" would answer differently across runs, and
    the agent's idea of a duplicate has to match the product's or the
    product will simply re-split what the agent merged.
    """
    seen: dict[str, Candidate] = {}
    duplicates = 0
    for candidate in _candidates(state):
        existing = seen.get(candidate.identity_key)
        if existing is None:
            seen[candidate.identity_key] = candidate
            continue
        duplicates += 1
        # Keep whichever has a link of its own: a candidate that points at
        # its award is strictly more useful downstream than one that does
        # not.
        if existing.url is None and candidate.url is not None:
            seen[candidate.identity_key] = candidate
    notes = [f"deduplicated {duplicates} candidate(s)"] if duplicates else []
    return ScholarshipState(candidates=_dump(list(seen.values())), notes=notes)


# --- 6. extract --------------------------------------------------------


async def extract_facts(state: ScholarshipState) -> ScholarshipState:
    """Read facts out of each candidate, two ways.

    Deterministic extraction and the model's extraction are both stored,
    and never merged. That is the product's rule for `extracted_facts`
    versus `ai_extracted_facts`, for the reason that survives here too:
    they have different provenance, and merging them leaves a reviewer
    unable to tell which part came from where.
    """
    client = client_from_settings()
    candidates = _candidates(state)
    for index, candidate in enumerate(candidates):
        candidate.deterministic_facts = extract_candidate_facts(candidate.title, candidate.excerpt)
        try:
            response = await _ask(
                client,
                AITask.extract_scholarship_facts,
                state=state,
                key=f"extract:{index}",
                source_data=prompts.extract_scholarship_facts(
                    title=candidate.title, excerpt=candidate.excerpt
                ),
            )
        except AIRouterError as exc:
            candidate.uncertainty_reasons.append(f"fact extraction failed: {exc}")
            continue
        if response.completed and response.output:
            candidate.model_facts = response.output.get("candidate") or {}
            candidate.prompt_versions["extract"] = response.prompt_version or ""
        else:
            candidate.uncertainty_reasons.append(
                f"fact extraction unavailable ({response.outcome.value})"
            )
    return ScholarshipState(candidates=_dump(candidates))


# --- 7. official source ------------------------------------------------


def _registrable(host: str) -> str:
    parts = host.lower().split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host.lower()


#: The product's own grading: A and B are authoritative sources, C and D are
#: aggregators and marketplaces. Reused rather than re-derived, and it is
#: the same split `auto_approval_scoring` penalises on.
AUTHORITATIVE_GRADES = frozenset({"A", "B"})


def _same_page(a: str, b: str) -> bool:
    """Same document, ignoring fragment and trailing-slash noise.

    Query strings are significant: DAAD addresses every award through one
    path with a `detail` parameter, so dropping the query would collapse
    the whole database into a single page.
    """
    if not a or not b:
        return False

    def key(url: str) -> tuple[str, str, str]:
        parts = urlsplit(url.strip())
        return (
            (parts.hostname or "").lower().removeprefix("www."),
            parts.path.rstrip("/"),
            parts.query,
        )

    return key(a) == key(b)


async def find_official_source(state: ScholarshipState) -> ScholarshipState:
    """Identify each candidate's official page.

    Two signals, in the absence of a search tool:

    * a link that *leaves* the source's domain points at the provider, and
      counts;
    * a link that stays on it counts only when the source is itself
      authoritative - the product grades sources A-D precisely so this
      question can be answered, and grade C or D means an aggregator.

    The distinction matters most on a list page. An aggregator linking to
    its own page is not corroboration, however plausible the link looks;
    accepting it is exactly how one list page ends up standing as proof of
    every award on it.

    Deliberately limited, and the limit should be read into the pilot's
    numbers: without a search tool, a candidate whose official page cannot
    be identified this way comes out as MORE_EVIDENCE_REQUIRED. That is
    correct behaviour, not a failure, but it caps the official-source
    discovery rate.
    """
    parent_host = _registrable(urlsplit(state["source_url"]).hostname or "")
    grade = str((state.get("discovery") or {}).get("authority_grade") or "")
    source_is_authoritative = grade.upper() in AUTHORITATIVE_GRADES
    candidates = _candidates(state)
    for candidate in candidates:
        if not candidate.url:
            candidate.uncertainty_reasons.append("candidate has no link of its own")
            continue
        host = _registrable(urlsplit(candidate.url).hostname or "")
        if not host:
            continue
        # A page cannot corroborate itself. The same-domain rule exists to
        # allow a provider's *other* page, not the identical one - and for
        # a grade-A source whose discovery URL is already the award's page,
        # the two are the same URL. Sixteen records in a row were checked
        # against themselves, which then reported "the official page
        # corroborates funding and deadline" about a single document
        # agreeing with its own excerpt.
        #
        # Same reasoning the aggregator rule already states, one level up:
        # a link to oneself is not a second source.
        if _same_page(candidate.url, state["source_url"]):
            candidate.uncertainty_reasons.append(
                "the only candidate link is the discovery's own page, which cannot corroborate "
                "itself"
            )
            continue
        if host != parent_host or source_is_authoritative:
            candidate.official_url = candidate.url
            continue
        candidate.uncertainty_reasons.append(
            f"candidate link stays on a grade-{grade or '?'} source domain, "
            "so it is not official evidence"
        )
    return ScholarshipState(candidates=_dump(candidates))


async def fetch_official(state: ScholarshipState) -> ScholarshipState:
    """Retrieve each official page. Jina first, for clean text.

    Per candidate in isolation: one unreachable page must not cost the
    other nine their verification.
    """
    settings = get_settings()
    candidates = _candidates(state)
    async with get_sessionmaker()() as db:
        for candidate in candidates:
            if not candidate.official_url:
                continue
            try:
                # The allowlist used to be `[host]`, derived from the very
                # URL being fetched - a check that could never fail, on a
                # URL originating in model output over untrusted page text.
                # Asking for the open-ended mode explicitly is honest about
                # what this is; the network guard still refuses a private
                # or reserved host.
                page = await fetch_official_page(
                    db,
                    candidate.official_url,
                    state["approved_domains"],
                    job_id=state["job_id"],
                    allow_any_public_domain=True,
                )
            except (ValueError, httpx.HTTPError, Exception) as exc:  # noqa: B014
                candidate.uncertainty_reasons.append(f"official page fetch failed: {exc}")
                continue
            candidate.official_page_fetched = True
            candidate.official_fetch_method = page.fetch_method
            candidate.evidence.append(
                Evidence(
                    claim_path="source.official_page",
                    value=candidate.official_url,
                    source_url=candidate.official_url,
                    source_type=SourceType.OFFICIAL_PAGE.value,
                    observed_at=_now(),
                    fetch_method=page.fetch_method,
                    confidence="explicit",
                    excerpt=page.text[:500],
                ).to_dict()
            )
            # Held outside graph state. It used to be stashed on
            # `candidate.model_facts`, which `to_dict()` serialises - so
            # every official page body (up to fetch_max_bytes) was written
            # into the checkpoint after this node, exactly what the comment
            # claimed it avoided. This dict is per-process and never
            # checkpointed; a resumed run simply re-fetches.
            _official_text[(state["job_id"], candidate.identity_key)] = page.text
    del settings
    return ScholarshipState(candidates=_dump(candidates))


# --- 8. compare --------------------------------------------------------


def _first(values: Any) -> str | None:
    if isinstance(values, list) and values:
        return str(values[0])
    return None


async def compare_evidence(state: ScholarshipState) -> ScholarshipState:
    """Check reported facts against the official page.

    The agreement verdict is computed here, deterministically, by
    `fact_matching`. The model is asked only to locate and quote the
    corresponding passages. Asking it whether two values agree would be
    asking it to verify, and no fuzzy tolerance is applied on purpose: a
    claimed amount and a real one either match or they do not corroborate.
    """
    client = client_from_settings()
    candidates = _candidates(state)
    for index, candidate in enumerate(candidates):
        official_text = _official_text.pop((state["job_id"], candidate.identity_key), None)
        if not candidate.official_page_fetched or not official_text:
            continue

        official_facts = extract_candidate_facts(candidate.title, official_text)
        model_pairs: dict[str, tuple[str | None, str | None]] = {}
        deterministic = {
            "funding.amount": (
                _values(candidate.deterministic_facts.get("funding_mentions")),
                _values(official_facts.get("funding_mentions")),
                amounts_match,
            ),
            "deadline.date": (
                _values(candidate.deterministic_facts.get("deadline_mentions")),
                _values(official_facts.get("deadline_mentions")),
                deadlines_match,
            ),
        }

        try:
            response = await _ask(
                client,
                AITask.compare_official_evidence,
                state=state,
                key=f"compare:{index}",
                source_data=prompts.compare_official_evidence(
                    title=candidate.title,
                    reported_facts=candidate.deterministic_facts,
                    official_url=candidate.official_url or "",
                    official_text=official_text,
                ),
            )
        except AIRouterError as exc:
            candidate.uncertainty_reasons.append(f"evidence comparison failed: {exc}")
            _settle_agreement(candidate, deterministic, model_pairs)
            continue
        if response.completed and response.output:
            # Recorded, but note what it does not do: a model-reported
            # contradiction is a reason for a human to look, and it reaches
            # the outcome only through the deterministic policy.
            candidate.contradictions = [
                str(item) for item in (response.output.get("contradictions") or [])
            ]
            candidate.prompt_versions["compare"] = response.prompt_version or ""
            model_pairs = _model_value_pairs(response.output)

        _settle_agreement(candidate, deterministic, model_pairs)
    return ScholarshipState(candidates=_dump(candidates))


def _model_value_pairs(output: dict[str, Any]) -> dict[str, tuple[str | None, str | None]]:
    """The (reported, official) strings the model located, per claim.

    Only the strings. The `relationship` it also returns is deliberately
    ignored for the verdict: deciding whether two values agree is the one
    judgement this workflow never delegates, because a model asked to grade
    its own extraction can answer differently over identical evidence with
    nothing to point at.
    """
    pairs: dict[str, tuple[str | None, str | None]] = {}
    for item in output.get("comparisons") or []:
        if not isinstance(item, dict):
            continue
        claim = str(item.get("claim_path") or "")
        if not claim:
            continue
        reported, official = item.get("reported_value"), item.get("official_value")
        pairs[claim] = (
            str(reported) if isinstance(reported, str | int | float) else None,
            str(official) if isinstance(official, str | int | float) else None,
        )
    return pairs


def _settle_agreement(
    candidate: Candidate,
    deterministic: dict[str, tuple[Any, Any, Any]],
    model_pairs: dict[str, tuple[str | None, str | None]],
) -> None:
    """Decide agreement per claim, preferring deterministically read values.

    The deterministic extractor is ported verbatim from the product so the
    two services agree about identity, and it is narrow: `parse_amount`
    reads currency symbols but not ISO codes, so "EUR 992" and
    "GBP 10,000" - which is how most of the official pages we actually
    fetch write it - yield nothing at all. Sixteen grade-A records in a
    row reached the official page, read it successfully, and reported "no
    evidence for funding" about text that plainly stated the funding.

    So where deterministic extraction found a value on both sides, it
    still decides. Where it did not, the *model's* located strings are
    used as the values - and the comparison itself is still made here, by
    `fact_matching`, never by the model.

    Evidence recorded from model-located values is marked
    `model_extracted` rather than `explicit`. The two are not equally
    strong, and a reviewer reading the row months from now has no other
    way to tell them apart - the same reason the product keeps
    `extracted_facts` and `ai_extracted_facts` separate instead of merging
    them.
    """

    verdicts: dict[str, bool | None] = {}
    for claim, (reported, official, comparator) in deterministic.items():
        agrees, value = _compare_sets(reported, official, comparator)
        confidence = "explicit"
        if agrees is None:
            model_reported, model_official = model_pairs.get(claim, (None, None))
            agrees, value = _compare_sets(
                [model_reported] if model_reported else [],
                [model_official] if model_official else [],
                comparator,
            )
            confidence = "model_extracted"
        verdicts[claim] = agrees
        if agrees and value:
            candidate.evidence.append(
                Evidence(
                    claim_path=claim,
                    value=value,
                    source_url=candidate.official_url or "",
                    source_type=SourceType.OFFICIAL_PAGE.value,
                    observed_at=_now(),
                    fetch_method=candidate.official_fetch_method,
                    confidence=confidence,
                ).to_dict()
            )
    candidate.amount_agrees = verdicts.get("funding.amount")
    candidate.deadline_agrees = verdicts.get("deadline.date")


def _values(mentions: Any) -> list[str]:
    """Every distinct mention, in order. Previously only the first was kept."""
    if not isinstance(mentions, list):
        return []
    seen: list[str] = []
    for m in mentions:
        s = str(m)
        if s and s not in seen:
            seen.append(s)
    return seen


def _compare_sets(
    reported: list[str], official: list[str], comparator: Any
) -> tuple[bool | None, str | None]:
    """Agreement across two sets of values, and the value that carried it.

    Only the *first* mention from each side used to be compared. A full
    official page lists several figures - a monthly stipend, a travel
    allowance, an insurance contribution - so first-against-first pairs two
    numbers that were never about the same thing, and a mismatch there was
    reported as "the official page states a different funding amount".
    Four of five rejections in one batch came from that.

    So: a claim is supported if it appears anywhere on the official page.
    It is contradicted only when each side offers exactly one value and
    they differ - the one case where we can be sure the two are about the
    same thing. Anything else is ambiguous, and ambiguous is None, because
    a rejection asserts the official page says otherwise and we would not
    know that.
    """
    if not reported or not official:
        return None, None
    for r in reported:
        for o in official:
            if comparator(r, o) is True:
                return True, o
    if len(reported) == 1 and len(official) == 1:
        # Both sides unambiguous and no match: a real contradiction, unless
        # one of them simply could not be parsed.
        return comparator(reported[0], official[0]), official[0]
    return None, None


def _agreement(reported: Any, official: Any, comparator=amounts_match) -> bool | None:
    """None means "nothing to compare", which is not the same as False.

    The product draws the same distinction in its corroboration checks: a
    candidate that asserts no deadline has not been contradicted about one.

    Both directions of "nothing to compare" count. This once returned False
    when the *official* page was silent, which reads as "the official page
    says otherwise" - and False on a deadline is exactly what drives
    REJECT_RECOMMENDED, the outcome asserting the official page contradicts
    the claim. An official page that simply does not print a deadline
    contradicts nothing; plenty state funding and link the dates elsewhere.

    It cost a real rejection: a Canadian federal award whose official page
    the workflow fetched successfully, found no parseable deadline on, and
    rejected for disagreeing with a deadline it had never read.
    """
    reported_value = _first(reported)
    official_value = _first(official)
    if reported_value is None or official_value is None:
        return None
    return comparator(reported_value, official_value)


# --- 9. eligibility ----------------------------------------------------


async def extract_eligibility(state: ScholarshipState) -> ScholarshipState:
    """Read academic and eligibility requirements, in their own scale.

    Never converted between grading systems. A 3.0/4.0 is not a 75% and a
    2:1 is not a GPA; inventing an equivalence would make an applicant's
    real result unrecoverable, so every rule keeps its original wording and
    is marked `not_converted`.
    """
    client = client_from_settings()
    candidates = _candidates(state)
    for index, candidate in enumerate(candidates):
        source_text = candidate.excerpt or ""
        if not candidate.official_page_fetched and not source_text:
            continue
        try:
            response = await _ask(
                client,
                AITask.extract_eligibility_requirements,
                state=state,
                key=f"eligibility:{index}",
                source_data=prompts.extract_eligibility_requirements(
                    title=candidate.title,
                    source_url=candidate.official_url or candidate.parent_source_url,
                    page_text=source_text,
                ),
            )
        except AIRouterError as exc:
            candidate.uncertainty_reasons.append(f"eligibility extraction failed: {exc}")
            continue
        if not response.completed or not response.output:
            candidate.uncertainty_reasons.append(
                f"eligibility extraction unavailable ({response.outcome.value})"
            )
            continue
        candidate.eligibility_rules = [
            _rule(item, candidate) for item in (response.output.get("rules") or [])
        ]
        candidate.prompt_versions["eligibility"] = response.prompt_version or ""
    return ScholarshipState(candidates=_dump(candidates))


def _rule(item: dict[str, Any], candidate: Candidate) -> dict[str, Any]:
    scale = str(item.get("scale_type") or ScaleType.UNKNOWN.value)
    if scale not in {member.value for member in ScaleType}:
        # An unrecognised scale is recorded as unknown rather than guessed
        # at. Guessing is how a percentage becomes a GPA.
        scale = ScaleType.UNKNOWN.value
    raw_scope = item.get("scope")
    scope: dict[str, Any] = raw_scope if isinstance(raw_scope, dict) else {}
    return EligibilityRule(
        requirement_type=str(item.get("requirement_type") or "unknown"),
        raw_value=str(item.get("raw_value") or ""),
        source_wording=str(item.get("source_wording") or ""),
        scale_type=scale,
        scale_max=item.get("scale_max") if isinstance(item.get("scale_max"), int | float) else None,
        measurement_basis=str(item.get("measurement_basis") or "unknown"),
        scope={
            "programmes": list(scope.get("programmes") or []),
            "countries": list(scope.get("countries") or []),
        },
        evidence_url=candidate.official_url or candidate.parent_source_url,
        evidence_excerpt=str(item.get("source_wording") or "") or None,
        verified_at=_now(),
    ).to_dict()


# --- 10. decide --------------------------------------------------------


async def decide(state: ScholarshipState) -> ScholarshipState:
    """Assign each candidate an outcome, and the job one overall.

    A pure function of what the workflow gathered. No model call: the
    recommendation has to be reproducible and explainable, and a model
    asked to grade its own work could answer differently over identical
    evidence with nothing to point at.
    """
    candidates = _candidates(state)

    # Before anything a model said. When neither fetcher returned a usable
    # page there is no evidence either way, and the one outcome that must
    # never be reachable from here is REJECT_RECOMMENDED - it means "the
    # official page contradicts the claim", which is a statement about a
    # page nobody managed to read.
    #
    # This is not hypothetical. A site answering bot mitigation with 202
    # and a 200-byte body had that body classified `not_a_scholarship`,
    # which short-circuited to REJECT_RECOMMENDED for ten real awards in a
    # single batch. The classification was reasonable given its input; the
    # mistake was asking at all.
    if state.get("page_usable") is False:
        for candidate in candidates:
            candidate.outcome = AgentOutcome.MORE_EVIDENCE_REQUIRED.value
            candidate.uncertainty_reasons.append("source page could not be retrieved")
        return ScholarshipState(
            candidates=_dump(candidates),
            outcome=AgentOutcome.MORE_EVIDENCE_REQUIRED.value,
            notes=["outcome MORE_EVIDENCE_REQUIRED (source page could not be retrieved)"],
        )

    short_circuit = outcome_for_page_type(state.get("page_type", ""))

    if short_circuit is not None:
        for candidate in candidates:
            candidate.outcome = short_circuit.value
            candidate.uncertainty_reasons.append("page describes no funding opportunity")
        return ScholarshipState(
            candidates=_dump(candidates),
            outcome=short_circuit.value,
            notes=[f"outcome {short_circuit.value} (page type)"],
        )

    for candidate in candidates:
        outcome, reasons = decide_outcome(candidate)
        candidate.outcome = outcome.value
        candidate.uncertainty_reasons.extend(reasons)

    overall = roll_up(candidates) if candidates else AgentOutcome.MORE_EVIDENCE_REQUIRED
    logger.info(
        "workflow_decided",
        extra={
            "job_id": state["job_id"],
            "candidate_count": len(candidates),
            "agent_outcome": overall.value,
        },
    )
    return ScholarshipState(
        candidates=_dump(candidates),
        outcome=overall.value,
        notes=[f"outcome {overall.value} over {len(candidates)} candidate(s)"],
    )
