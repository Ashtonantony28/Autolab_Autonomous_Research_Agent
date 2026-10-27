"""A full `autolab run` against Claude, and the same graph driven to END."""

from __future__ import annotations

from typing import Final
from uuid import uuid4

import pytest
from langgraph.types import Command

from autolab.cli import main
from autolab.orchestrator.graph import build_graph
from autolab.state.models import HITLResolution
from autolab.state.schema import empty_state
from autolab.verification.llm import ClaudeLLMClient

QUESTION: Final = (
    "Does increasing NaCl concentration from 0.1 M to 0.5 M speed lysozyme crystallization?"
)
MAX_RESUMES: Final = 8


def test_autolab_run_finishes(capsys: pytest.CaptureFixture[str]) -> None:
    main(["run", QUESTION])
    out = capsys.readouterr().out
    assert "Run complete." in out or "Paused for human review" in out, out


async def test_run_reaches_end_when_pauses_are_approved(api_key: str) -> None:
    graph = build_graph(llm=ClaudeLLMClient(api_key))
    config = {"configurable": {"thread_id": str(uuid4())}}
    state = await graph.ainvoke(empty_state(QUESTION), config=config)
    resumes = 0
    while "__interrupt__" in state:
        assert resumes < MAX_RESUMES, state["hitl_pending"]
        approve: Command[str] = Command(resume=HITLResolution(action="approve"))
        state = await graph.ainvoke(approve, config=config)
        resumes += 1
    assert state["hypotheses"] and state["results"] and state["analysis_reports"]
