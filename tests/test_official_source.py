"""What a search result has to prove before it counts as official.

A search tool is the first thing in this workflow that volunteers a URL
nobody asked for. Every other source of a candidate URL was at least on a
page we chose to fetch; a search engine's ranking is a stranger's opinion
about relevance, and relevance is not provenance.

So the tests here are mostly about what is *refused*. The one that matters
most is the aggregator trap: the word "scholarship" appears in the domain of
nearly every aggregator in the queue - scholarshipsads, scholarshipregion,
scholarshiproar - and in almost no real provider's. A naive name match would
therefore promote precisely the sites this pipeline exists to see past, and
would look like it was working while doing it.
"""

import pytest

from app.tools.jina import SearchResult
from app.usecases.scholarship_finder.official_source import (
    distinctive_words,
    host_labels,
    is_public_body,
    names_match,
    pick_official,
    search_query,
)


def result(url: str, title: str = "") -> SearchResult:
    return SearchResult(title=title or url, url=url, description="")


# --- reading a host -----------------------------------------------------


@pytest.mark.parametrize(
    "host,expected",
    [
        ("www.chevening.org", {"chevening"}),
        ("chevening.org", {"chevening"}),
        ("www2.daad.de", {"daad"}),
        # The name sits at a different depth under a multi-part suffix. A
        # rule that took the second-to-last label would read this as a
        # Turkish university called "edu".
        ("ku.edu.tr", {"ku"}),
        ("www.soton.ac.uk", {"soton"}),
        ("turkiyeburslari.gov.tr", {"turkiyeburslari"}),
        ("studyinsweden.se", {"studyinsweden"}),
        ("localhost", set()),
    ],
)
def test_the_parts_of_a_host_that_could_name_somebody(host, expected):
    assert host_labels(host) == expected


# --- reading a title ----------------------------------------------------


def test_generic_words_are_not_names():
    """The whole rule rests on this. Every one of these appears in the
    domain of some aggregator."""
    assert distinctive_words("Fully Funded International Scholarships 2026") == set()


def test_a_providers_name_survives():
    assert "chevening" in distinctive_words("Chevening Scholarships 2026")
    # Four characters is the floor, and DAAD sits exactly on it - which is
    # the case the floor was chosen around, since daad.de is a real
    # provider domain reached by a real award title.
    assert distinctive_words("DAAD Scholarship") == {"daad"}


def test_a_name_below_the_floor_is_dropped():
    """ENS is a real provider at ens.psl.eu, and three characters is not
    enough to tell it from a coincidence. Missed rather than guessed."""
    assert distinctive_words("ENS Scholarships") == set()


def test_an_accented_name_folds_to_match_its_domain():
    """`Türkiye` has to reach `turkiyeburslari.gov.tr`, and the domain is
    never going to carry the diaeresis."""
    assert "turkiye" in distinctive_words("Türkiye Scholarships 2026")


def test_a_year_is_not_a_name():
    assert distinctive_words("2026 Scholarships") == set()


# --- matching -----------------------------------------------------------


@pytest.mark.parametrize(
    "word,label",
    [
        ("chevening", "chevening"),
        ("turkiye", "turkiyeburslari"),
        ("erasmus", "erasmusplus"),
    ],
)
def test_a_name_matches_a_longer_form_of_itself(word, label):
    assert names_match(word, label)


@pytest.mark.parametrize(
    "word,label",
    [
        # Substring anywhere would make this a hit. Prefixes only.
        ("ens", "students"),
        ("gates", "postgraduate"),
        ("art", "smartscholar"),
        ("oxford", "cambridge"),
    ],
)
def test_a_coincidence_is_not_a_match(word, label):
    assert not names_match(word, label)


# --- choosing -----------------------------------------------------------


def test_the_aggregator_trap():
    """The test this module exists for. An aggregator ranks first and its
    domain contains the word "scholarship"; the provider ranks third. A
    rule that matched on generic words would take the first and report an
    official source it had not found."""
    chosen = pick_official(
        "Chevening Scholarships 2026",
        [
            result("https://scholarshipsads.com/chevening-scholarships/"),
            result("https://scholarshipregion.com/chevening/"),
            result("https://www.chevening.org/apply/"),
        ],
        source_host_labels=set(),
    )

    assert chosen is not None
    assert chosen.url == "https://www.chevening.org/apply/"


def test_rank_is_followed_only_among_results_that_already_passed():
    chosen = pick_official(
        "Chevening Scholarships",
        [
            result("https://www.chevening.org/apply/"),
            result("https://www.chevening.org/scholarships/"),
        ],
        source_host_labels=set(),
    )

    assert chosen is not None and chosen.url.endswith("/apply/")


def test_a_result_on_the_source_domain_is_refused():
    """The aggregator we are trying to see past, arriving by a new route.
    `find_official_source` already refuses its links; a search result is
    the one path in that did not come from the page."""
    chosen = pick_official(
        "Chevening Scholarships",
        [result("https://scholarshipregion.com/chevening-scholarships/")],
        source_host_labels={"scholarshipregion"},
    )

    assert chosen is None


def test_a_title_with_no_distinctive_name_finds_nothing():
    """ "Fully Funded Scholarship in France" names no provider, so no
    domain can be shown to be its provider's. Returning nothing is the
    honest answer and leaves the candidate where it already was."""
    chosen = pick_official(
        "Fully Funded Scholarships in France 2026",
        [result("https://campusfrance.org/en/scholarships")],
        source_host_labels=set(),
    )

    assert chosen is None


def test_results_that_name_nobody_are_refused_even_when_plausible():
    chosen = pick_official(
        "Chevening Scholarships",
        [
            result("https://en.wikipedia.org/wiki/Chevening_Scholarship"),
            result("https://www.timeshighereducation.com/chevening"),
        ],
        source_host_labels=set(),
    )

    assert chosen is None, "a page *about* the award is not the provider's page"


@pytest.mark.parametrize(
    "url", ["ftp://chevening.org/x", "javascript:alert(1)", "file:///etc/passwd", "not a url"]
)
def test_only_http_urls_are_considered(url):
    assert pick_official("Chevening Scholarships", [result(url)], source_host_labels=set()) is None


def test_an_excluded_host_is_refused():
    chosen = pick_official(
        "Chevening Scholarships",
        [result("https://www.chevening.org/apply/")],
        source_host_labels=set(),
        excluded_hosts={"www.chevening.org"},
    )

    assert chosen is None


def test_no_results_is_not_a_match():
    assert pick_official("Chevening Scholarships", [], source_host_labels=set()) is None


# --- the query ----------------------------------------------------------


def test_the_query_is_deterministic():
    """Two runs of the same candidate must ask the same question, or an
    evaluation cannot compare them and a rerun pays twice."""
    assert search_query("Chevening Scholarships") == search_query("Chevening Scholarships")


def test_the_provider_is_added_only_when_it_is_not_already_there():
    assert search_query("Chevening Scholarships", "Chevening") == (
        "Chevening Scholarships official site"
    )
    assert "FCDO" in search_query("Chevening Scholarships", "FCDO")


# --- preferring the body that actually runs the award -------------------


def test_a_public_body_outranking_a_vanity_domain_wins():
    """Measured, not supposed. Official bodies are called DFAT, SBFI and
    MEXT - never after the award - so the naming test walks past them and
    settles on whichever domain does echo the award. On the pilot's own
    candidates this took australiaawards.com.au over dfat.gov.au, and
    clarendonscholarsassociation.co.uk over ox.ac.uk."""
    chosen = pick_official(
        "Australia Awards Scholarships",
        [
            result("https://www.dfat.gov.au/people-to-people/australia-awards/"),
            result("https://australiaawards.com.au/"),
        ],
        source_host_labels=set(),
    )

    assert chosen is not None
    assert chosen.url.startswith("https://www.dfat.gov.au/")


def test_a_public_body_ranked_below_the_name_match_does_not_displace_it():
    """The promotion follows the search engine's own ranking rather than
    overriding it. A university page that ranks *under* the provider's own
    site is a page about the award, and mastercardfdn.org losing to a
    Carnegie Mellon page is the regression this avoids."""
    chosen = pick_official(
        "Mastercard Foundation Scholars Program",
        [
            result("https://mastercardfdn.org/en/what-we-do/"),
            result("https://www.africa.engineering.cmu.edu/impact/mastercard/"),
        ],
        source_host_labels=set(),
    )

    assert chosen is not None and "mastercardfdn.org" in chosen.url


def test_a_public_body_alone_is_still_not_enough():
    """The guard that keeps precision. Admitting any government or academic
    domain took the pilot from 40 matches to 65, and most of the extra 25
    were some institution's page *about* an award - nsf.gov for a DAAD
    award, an Indian consulate for Italian scholarships. Recording one of
    those as `source.official_page` is a false claim about provenance."""
    chosen = pick_official(
        "Study Scholarships for STEM Disciplines",
        [result("https://www.nsf.gov/funding/opportunities/s-stem/")],
        source_host_labels=set(),
    )

    assert chosen is None


@pytest.mark.parametrize(
    "host,expected",
    [
        ("www.dfat.gov.au", True),
        ("www.ox.ac.uk", True),
        ("studyinkorea.go.kr", True),
        ("cihr-irsc.gc.ca", True),
        ("www.sbfi.admin.ch", True),
        ("erasmus-plus.ec.europa.eu", True),
        ("stanford.edu", True),
        ("australiaawards.com.au", False),
        ("chevening.org", False),
        ("scholarshipsads.com", False),
    ],
)
def test_recognising_a_public_body(host, expected):
    assert is_public_body(host) is expected
