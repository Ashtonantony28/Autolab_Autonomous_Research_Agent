"""SG9: ids, timestamps and cross-references come from code, never from the model."""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from anthropic.lib._parse._transform import transform_schema
from pydantic import BaseModel

from autolab.agents.common import DESIGN_REPLY, HYPOTHESIS_REPLY, REPORT_REPLY
from autolab.agents.design import DesignAgent, build_design_prompt, build_design_trace_prompt
from autolab.orchestrator.graph import build_graph
from autolab.state.models import ExperimentDesign, ExperimentParameter, Hypothesis
from autolab.state.schema import empty_state
from autolab.tools.lab_hardware.simulator import LabSimulator
from autolab.verification.llm import FakeLLMClient, LLMPurpose, SchemaT
from autolab.verification.vericot import Verifier
from tests.fixtures.agent_helpers import CODE_OWNED, register_trivial_trace
from tests.integration.orchestrator.test_graph import (
    QUESTION,
    RESULT,
    FixedResultSimulator,
    _seed_full_run,
)

PAYLOAD_PURPOSES = {
    "research.hypothesis": HYPOTHESIS_REPLY,
    "design.experiment": DESIGN_REPLY,
    "analysis.report": REPORT_REPLY,
}


class RecordingLLM(FakeLLMClient):
    """Records the schema each structured call asks the model to fill."""

    def __init__(self) -> None:
        super().__init__()
        self.schemas: dict[LLMPurpose, type[BaseModel]] = {}

    async def call_structured(
        self, purpose: LLMPurpose, prompt: str, schema: type[SchemaT]
    ) -> SchemaT:
        self.schemas[purpose] = schema
        return await super().call_structured(purpose, prompt, schema)


async def test_every_agent_payload_schema_sent_to_the_model_omits_code_owned_fields() -> None:
    llm = RecordingLLM()
    _seed_full_run(llm)
    graph = build_graph(llm=llm, simulator=FixedResultSimulator(RESULT), retry_backoff_seconds=0)

    await graph.ainvoke(empty_state(QUESTION), {"configurable": {"thread_id": str(uuid4())}})

    assert {p: llm.schemas[p] for p in PAYLOAD_PURPOSES} == PAYLOAD_PURPOSES
    for schema in PAYLOAD_PURPOSES.values():
        assert not CODE_OWNED & set(transform_schema(schema)["properties"])
    assert "question" not in HYPOTHESIS_REPLY.model_fields


@pytest.mark.parametrize(
    ("schema", "content"),
    [
        (HYPOTHESIS_REPLY, {"statement": "s", "rationale": "r"}),
        (DESIGN_REPLY, {"protocol": "p", "estimated_cost_usd": 0, "estimated_duration_minutes": 0}),
        (REPORT_REPLY, {"interpretations": [], "summary": "s"}),
    ],
)
def test_reply_schemas_keep_content_and_ignore_code_owned_keys(
    schema: type[BaseModel], content: dict[str, object]
) -> None:
    reply = schema.model_validate({**dict.fromkeys(CODE_OWNED, "x"), **content})
    assert set(content) <= set(reply.model_fields_set)
    assert not CODE_OWNED & set(reply.model_dump())


async def test_identical_model_replies_get_distinct_ids_and_independent_noise() -> None:
    hypothesis = Hypothesis(question="Q?", statement="S", rationale="R")
    # The model repeats one example id; the simulator seed must not follow it.
    reply = ExperimentDesign(
        hypothesis_id=hypothesis.id,
        protocol="measure yield",
        parameters=(ExperimentParameter(name="noise_sigma", value=1.0),),
        estimated_cost_usd=1.0,
        estimated_duration_minutes=1.0,
    )
    fake = FakeLLMClient()
    fake.register(
        "design.experiment",
        build_design_prompt(hypothesis),
        json.dumps(reply.model_dump(mode="json")),
    )
    register_trivial_trace(
        fake,
        agent="design",
        trace_purpose="design.trace",
        trace_prompt=build_design_trace_prompt(hypothesis, reply),
        conclusion=reply.protocol,
    )
    state = empty_state("Q?")
    state["hypotheses"] = [hypothesis]
    agent = DesignAgent(verifier=Verifier(fake), llm=fake)

    designs = [(await agent(state))["current_experiment"] for _ in range(2)]

    assert len({d.id for d in designs} | {reply.id}) == 3
    values = [LabSimulator().run(d).measurements[0].value for d in designs]
    assert values[0] != values[1]
