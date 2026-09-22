"""A workflow that exercises the runtime and nothing else.

Deploying a job runner and having no way to answer "can this environment
actually claim, run, checkpoint and finish a job?" without pointing it at
production discoveries is how a broken worker gets found by the backlog
rather than by the person who deployed it. This graph calls no tool, makes
no model call and writes to no product - it only proves the machinery.

It is also how the failure paths get tested honestly: a runtime whose retry
and lease handling has only ever been exercised by a mock is a runtime whose
retry and lease handling is untested.
"""

import asyncio

from langgraph.graph import END, START, StateGraph

from app.runtime.task_state import WorkflowState

USE_CASE_ID = "probe"


async def begin(state: WorkflowState) -> WorkflowState:
    payload = state.get("payload") or {}

    delay = float(payload.get("delay_seconds") or 0)
    if delay > 0:
        # Used to verify that a run outliving its lease is kept alive by the
        # heartbeat rather than reclaimed from under itself.
        await asyncio.sleep(delay)

    # Deliberate failures, so the retry classifier and the durable
    # failed_review state can be verified in a real environment.
    if payload.get("fail_transient"):
        raise TimeoutError("probe: simulated transient failure")
    if payload.get("fail_permanent"):
        raise ValueError("probe: simulated permanent failure")

    return WorkflowState(notes=["probe: begin"])


async def finish(state: WorkflowState) -> WorkflowState:
    return WorkflowState(notes=["probe: finish"], outcome="PROBE_OK")


def build() -> StateGraph:
    graph = StateGraph(WorkflowState)
    graph.add_node("begin", begin)
    graph.add_node("finish", finish)
    graph.add_edge(START, "begin")
    graph.add_edge("begin", "finish")
    graph.add_edge("finish", END)
    return graph
