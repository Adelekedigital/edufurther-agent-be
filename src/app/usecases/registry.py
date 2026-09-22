"""Use-case registry.

A workflow is addressed by `use_case_id` over the wire, so something has to
map that string to a graph. Making it an explicit registry rather than an
if/elif chain means an unknown use case is rejected at the API boundary,
where the caller can act on it, instead of being queued as a job that is
guaranteed to fail later.
"""

from collections.abc import Callable

from langgraph.graph.state import CompiledStateGraph, StateGraph

GraphBuilder = Callable[[], StateGraph]

_REGISTRY: dict[str, GraphBuilder] = {}


class UnknownUseCase(LookupError):
    """Raised for a use case this build does not implement."""


def register(use_case_id: str, builder: GraphBuilder) -> None:
    if use_case_id in _REGISTRY:
        raise ValueError(f"use case already registered: {use_case_id}")
    _REGISTRY[use_case_id] = builder


def is_known(use_case_id: str) -> bool:
    return use_case_id in _REGISTRY


def known_use_cases() -> tuple[str, ...]:
    return tuple(sorted(_REGISTRY))


def build_graph(use_case_id: str, *, checkpointer=None) -> CompiledStateGraph:
    """Compile the graph for `use_case_id`.

    Compiled per call rather than cached, because the checkpointer is bound
    at compile time and a cached graph would pin whichever one happened to
    exist first - including, in tests, one belonging to a closed pool.
    """
    builder = _REGISTRY.get(use_case_id)
    if builder is None:
        raise UnknownUseCase(
            f"no workflow registered for {use_case_id!r}; known: {known_use_cases()}"
        )
    return builder().compile(checkpointer=checkpointer)
