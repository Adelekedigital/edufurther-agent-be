"""The state a workflow carries between nodes.

Kept flat and JSON-serializable on purpose: every value here is written to a
checkpoint after each node, and a checkpoint that cannot round-trip through
JSON is a checkpoint that cannot resume. No ORM objects, no open clients, no
sessions - those belong to the node that opens them and must not survive
into state.
"""

from typing import Annotated, Any, TypedDict


def merge_notes(existing: list[str] | None, incoming: list[str] | None) -> list[str]:
    """Append rather than replace.

    LangGraph's default reducer overwrites a key when two nodes both return
    it. For an audit trail that is exactly wrong: the second node's note
    would silently erase the first node's.
    """
    return [*(existing or []), *(incoming or [])]


class WorkflowState(TypedDict, total=False):
    """Common state for every use case.

    `total=False` because nodes fill this in progressively; a node reading a
    key another node has not written yet gets a missing key, not a lie.
    """

    # --- Set at job start, never mutated by a node ---
    job_id: str
    product_id: str
    use_case_id: str
    input_reference: str
    correlation_id: str
    workflow_version: str
    payload: dict[str, Any]

    # --- Accumulated during the run ---
    notes: Annotated[list[str], merge_notes]
    #: Why the workflow could not reach a confident answer. Carried into the
    #: agent outcome rather than discarded - "we do not know, and here is
    #: why" is a usable result for a human reviewer, an empty field is not.
    uncertainty_reasons: Annotated[list[str], merge_notes]
    outcome: str | None


def initial_state(
    *,
    job_id: str,
    product_id: str,
    use_case_id: str,
    input_reference: str,
    correlation_id: str,
    workflow_version: str,
    payload: dict[str, Any] | None = None,
) -> WorkflowState:
    return WorkflowState(
        job_id=job_id,
        product_id=product_id,
        use_case_id=use_case_id,
        input_reference=input_reference,
        correlation_id=correlation_id,
        workflow_version=workflow_version,
        payload=payload or {},
        notes=[],
        uncertainty_reasons=[],
        outcome=None,
    )
