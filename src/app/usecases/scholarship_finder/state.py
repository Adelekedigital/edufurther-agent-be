"""State for the scholarship verification workflow.

Extends the common `WorkflowState`. Everything here is JSON-serializable
because it all ends up in a checkpoint - candidates are carried as plain
dicts and rehydrated into dataclasses inside each node, rather than kept as
objects that would not survive a resume.
"""

from typing import Any

from app.runtime.task_state import WorkflowState


class ScholarshipState(WorkflowState, total=False):
    #: The product's record, as read from its agent API. Never modified
    #: here; the agent proposes changes through the API, not by mutating
    #: its copy.
    discovery: dict[str, Any]
    #: The Source's fetch allowlist, taken from the product so the agent
    #: applies the same domain policy rather than inventing its own.
    approved_domains: list[str]
    source_url: str

    page_text: str
    page_fetch_method: str
    page_type: str

    #: `Candidate.to_dict()` each. A list rather than a keyed map because
    #: order is meaningful on a list page - "the third award on the page"
    #: is part of how a reviewer finds it again.
    candidates: list[dict[str, Any]]
