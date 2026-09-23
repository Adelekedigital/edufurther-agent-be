"""The scholarship verification workflow.

```text
load_discovery → fetch_source → classify_page
   ├─ individual / aggregator → single_candidate
   ├─ list                    → split_candidates
   └─ not_a_scholarship       → decide  (short-circuit)
→ dedupe_candidates → extract_facts → find_official_source
→ fetch_official → compare_evidence → extract_eligibility → decide
→ submit_to_product
```

The branch after classification is the reason this service exists. The
product has no notion of a page holding many awards, so a blog roundup of
ten scholarships arrives as one discovery and leaves as one - which is
exactly what dominates the backlog.

`submit_to_product` is the only node that writes anything the product can
see, and in shadow mode - the default, and how Stage 1 of the rollout runs -
it writes only to tables the product's review, approval and publication
paths do not read.
"""

from langgraph.graph import END, START, StateGraph

from app.usecases.scholarship_finder import nodes, submission
from app.usecases.scholarship_finder.schemas import PageType
from app.usecases.scholarship_finder.state import ScholarshipState

USE_CASE_ID = "scholarship_verification"


def route_after_fetch(state: ScholarshipState) -> str:
    """Skip classification when the fetch produced nothing to classify.

    Saves a model call per unreadable page, and - far more importantly -
    keeps a classification of bot-mitigation boilerplate out of the
    decision entirely, rather than making `decide` responsible for
    distrusting an answer it should never have asked for.
    """
    if state.get("page_usable") is False:
        return "decide"
    return "classify_page"


def route_after_classification(state: ScholarshipState) -> str:
    page_type = state.get("page_type", "")
    if page_type == PageType.NOT_A_SCHOLARSHIP.value:
        # Nothing to extract. Reaching a reviewer with an empty candidate
        # would be worse than saying plainly that the page holds no award.
        return "decide"
    if page_type == PageType.LIST.value:
        return "split_candidates"
    # An aggregator page links out rather than describing awards in full,
    # so it is treated as one candidate here. Following its links needs a
    # search tool this build does not have yet.
    return "single_candidate"


def build() -> StateGraph:
    graph = StateGraph(ScholarshipState)

    graph.add_node("load_discovery", nodes.load_discovery)
    graph.add_node("fetch_source", nodes.fetch_source)
    graph.add_node("classify_page", nodes.classify_page)
    graph.add_node("split_candidates", nodes.split_candidates)
    graph.add_node("single_candidate", nodes.single_candidate)
    graph.add_node("dedupe_candidates", nodes.dedupe_candidates)
    graph.add_node("extract_facts", nodes.extract_facts)
    graph.add_node("find_official_source", nodes.find_official_source)
    graph.add_node("fetch_official", nodes.fetch_official)
    graph.add_node("compare_evidence", nodes.compare_evidence)
    graph.add_node("extract_eligibility", nodes.extract_eligibility)
    graph.add_node("decide", nodes.decide)
    graph.add_node("submit_to_product", submission.submit_to_product)

    graph.add_edge(START, "load_discovery")
    graph.add_edge("load_discovery", "fetch_source")
    graph.add_conditional_edges(
        "fetch_source",
        route_after_fetch,
        {"classify_page": "classify_page", "decide": "decide"},
    )
    graph.add_conditional_edges(
        "classify_page",
        route_after_classification,
        {
            "split_candidates": "split_candidates",
            "single_candidate": "single_candidate",
            "decide": "decide",
        },
    )
    graph.add_edge("split_candidates", "dedupe_candidates")
    graph.add_edge("single_candidate", "dedupe_candidates")
    graph.add_edge("dedupe_candidates", "extract_facts")
    graph.add_edge("extract_facts", "find_official_source")
    graph.add_edge("find_official_source", "fetch_official")
    graph.add_edge("fetch_official", "compare_evidence")
    graph.add_edge("compare_evidence", "extract_eligibility")
    graph.add_edge("extract_eligibility", "decide")
    graph.add_edge("decide", "submit_to_product")
    graph.add_edge("submit_to_product", END)

    return graph
