"""Choosing an official page out of search results.

A search tool changes what `find_official_source` can reach, not what it is
allowed to believe. The rule the rest of the workflow rests on is unchanged:
corroboration means a *second document*, from the provider, that a
deterministic comparator could read. A search engine returning a plausible
link is not that, and must not become it by being ranked first.

So this module only ever narrows. It rejects on grounds it can state, and
accepts on one signal it can justify - the award's own distinctive name
appearing in the domain. Where neither applies it returns nothing, and the
candidate stays MORE_EVIDENCE_REQUIRED, which is the same answer it gets
today.

The stoplist below is the load-bearing part, and it is worth saying why.
Without it the matching rule prefers exactly the wrong domains: the token
"scholarship" appears in the name of almost every aggregator in the queue -
scholarshipsads, scholarshipregion, scholarshiproar - and in almost no
provider's. A rule that matched on it would systematically promote the
aggregators this pipeline exists to see past, and would do so while looking
like it was working.
"""

import re
from urllib.parse import urlsplit

from app.tools.jina import SearchResult
from app.usecases.scholarship_finder.normalization import fold

#: Host labels that identify nobody. The last label of a host is dropped
#: separately as the TLD, so this is the middle of a public suffix
#: (`ku.edu.tr`, `soton.ac.uk`) plus the usual serving prefixes.
_NON_DISTINCTIVE_LABELS = frozenset(
    {"www", "www1", "www2", "www3", "web", "ac", "edu", "gov", "co", "com", "org", "net", "int"}
)

#: Words that appear in award titles and identify no particular provider.
#: Matching on any of these is worse than not matching at all - see the
#: module docstring.
_GENERIC_TITLE_WORDS = frozenset(
    {
        "scholarship",
        "scholarships",
        "scholar",
        "scholars",
        "fellowship",
        "fellowships",
        "bursary",
        "bursaries",
        "grant",
        "grants",
        "award",
        "awards",
        "prize",
        "prizes",
        "funding",
        "funded",
        "fully",
        "full",
        "free",
        "study",
        "studies",
        "student",
        "students",
        "international",
        "global",
        "abroad",
        "overseas",
        "foreign",
        "university",
        "universities",
        "college",
        "school",
        "institute",
        "academy",
        "faculty",
        "master",
        "masters",
        "bachelor",
        "bachelors",
        "doctoral",
        "doctorate",
        "phd",
        "mba",
        "msc",
        "undergraduate",
        "postgraduate",
        "graduate",
        "program",
        "programme",
        "programs",
        "programmes",
        "course",
        "courses",
        "degree",
        "degrees",
        "apply",
        "application",
        "applications",
        "deadline",
        "deadlines",
        "online",
        "list",
        "best",
        "top",
        "latest",
        "opportunity",
        "opportunities",
    }
)

#: Below this, a shared prefix says nothing. "ens" matching "ensemble" is
#: the kind of coincidence a four-character floor removes.
_MIN_MATCH_CHARS = 4

_WORD = re.compile(r"[a-z0-9]+")

#: Labels marking a government or academic body. Used only to *reorder*
#: results that already passed the naming test - never to admit one that
#: did not. Measured on the pilot's own candidates: admitting them widened
#: 40 matches to 65, but most of the 25 were some university's page about
#: an award rather than the award's own, and recording one of those as
#: `source.official_page` is a false claim about provenance, not a
#: slightly optimistic one.
_PUBLIC_BODY_LABELS = frozenset({"gov", "edu", "ac", "gc", "go", "mil", "int", "admin"})
_PUBLIC_BODY_TLDS = frozenset({"edu", "gov", "int", "mil"})


def is_public_body(host: str) -> bool:
    """Whether a host belongs to a government or an academic institution."""
    parts = [part for part in host.lower().strip().split(".") if part]
    if len(parts) < 2:
        return False
    if parts[-1] in _PUBLIC_BODY_TLDS or host.lower().endswith("europa.eu"):
        return True
    return any(part in _PUBLIC_BODY_LABELS for part in parts[1:-1])


def host_labels(host: str) -> set[str]:
    """The parts of a host that could name somebody.

    The final label is always dropped - it is the TLD and identifies a
    registry, not a provider. What remains is filtered rather than reduced
    to one "registrable" label, because the meaningful name sits in
    different positions in different countries: `chevening` is second-level
    in `chevening.org` and `ku` is third-level in `ku.edu.tr`, and a rule
    that took a fixed position would read `edu` as the name of a Turkish
    university.
    """
    parts = [part for part in host.lower().strip().split(".") if part]
    if len(parts) < 2:
        return set()
    return {part for part in parts[:-1] if part not in _NON_DISTINCTIVE_LABELS}


def distinctive_words(title: str) -> set[str]:
    """The words in a title that could name a provider.

    Folded first, so an accented name matches its unaccented domain -
    `Türkiye` against `turkiyeburslari.gov.tr`.
    """
    words = _WORD.findall(fold(title).lower())
    return {
        word
        for word in words
        if len(word) >= _MIN_MATCH_CHARS and word not in _GENERIC_TITLE_WORDS and not word.isdigit()
    }


def names_match(word: str, label: str) -> bool:
    """Whether a title word and a host label name the same thing.

    Prefixes only, in either direction: `turkiye` against
    `turkiyeburslari` is the same body under a longer name, while a
    substring match anywhere would make `ens` a hit inside `students`.
    """
    if len(word) < _MIN_MATCH_CHARS or len(label) < _MIN_MATCH_CHARS:
        return word == label and len(word) >= _MIN_MATCH_CHARS
    return word.startswith(label) or label.startswith(word)


def search_query(title: str, provider: str | None = None) -> str:
    """The query to send. Deterministic, so a rerun is free at the router
    and comparable in an evaluation."""
    parts = [title.strip()]
    if provider and provider.strip().lower() not in title.strip().lower():
        parts.append(provider.strip())
    parts.append("official site")
    return " ".join(part for part in parts if part)


def pick_official(
    title: str,
    results: list[SearchResult],
    *,
    source_host_labels: set[str],
    excluded_hosts: set[str] = frozenset(),  # type: ignore[assignment]
) -> SearchResult | None:
    """The first result whose domain carries the award's own name.

    Rank order is followed only among results that already passed the test,
    so a better-ranked but unnamed result never wins. `None` means no
    result could be justified, which is a real answer and the common one.

    `source_host_labels` are the discovery's own domain. A search result
    living there is the aggregator we are trying to see past, and the
    same-domain rule in `find_official_source` already refuses it - this
    refuses it again rather than relying on the caller, because a search
    result is the one path into that function that did not come from the
    page itself.
    """
    words = distinctive_words(title)
    if not words:
        return None

    usable = [
        (result, host, labels)
        for result in results
        for host in [(urlsplit(result.url).hostname or "").lower()]
        for labels in [host_labels(host)]
        if urlsplit(result.url).scheme in {"http", "https"}
        and host
        and host not in excluded_hosts
        and labels
        and not labels & source_host_labels
    ]

    named = next(
        (
            index
            for index, (_, _, labels) in enumerate(usable)
            if any(names_match(word, label) for word in words for label in labels)
        ),
        None,
    )
    if named is None:
        return None

    # A public body ranked above the name match was the better answer all
    # along. This is the one correction the measurement demanded: official
    # bodies are called DFAT, SBFI and MEXT, never after the award, so the
    # naming test walks past them and settles on whichever vanity domain
    # does echo the award - australiaawards.com.au over dfat.gov.au,
    # clarendonscholarsassociation.co.uk over ox.ac.uk. It corrected seven
    # of forty picks on the pilot's candidates and changed the reach by
    # nothing, which is the point: acceptance is untouched.
    for result, host, _ in usable[:named]:
        if is_public_body(host):
            return result
    return usable[named][0]
