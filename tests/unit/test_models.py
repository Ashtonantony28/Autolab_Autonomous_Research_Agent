"""Unit tests for the core Pydantic models."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from autolab.state.models import (
    Citation,
    ConfidenceScore,
    ExperimentDesign,
    ExperimentParameter,
    ExperimentResult,
    HITLRequest,
    Hypothesis,
    Measurement,
    ReasoningStep,
    ReasoningTrace,
    StepVerdict,
    VerificationResult,
)


def _trace(steps: int = 3) -> ReasoningTrace:
    return ReasoningTrace(
        agent="research",
        steps=tuple(
            ReasoningStep(index=i, claim=f"step {i}", claim_type="inference") for i in range(steps)
        ),
    )


class TestRoundtrip:
    """Every model survives a JSON round-trip."""

    def test_hypothesis(self) -> None:
        h = Hypothesis(
            question="why does X?",
            statement="X because Y",
            rationale="Z",
            citations=(Citation(source="arxiv", identifier="1234.5678", title="paper"),),
            confidence=0.7,
        )
        assert Hypothesis.model_validate_json(h.model_dump_json()) == h

    def test_experiment_design(self) -> None:
        d = ExperimentDesign(
            hypothesis_id=uuid4(),
            protocol="add A to B at 25C",
            parameters=(ExperimentParameter(name="temperature", value=25.0, unit="C"),),
            estimated_cost_usd=12.5,
            estimated_duration_minutes=45,
            success_criteria=("yield > 80%",),
        )
        assert ExperimentDesign.model_validate_json(d.model_dump_json()) == d

    def test_experiment_result(self) -> None:
        r = ExperimentResult(
            design_id=uuid4(),
            status="success",
            measurements=(Measurement(name="yield", value=0.91, unit="ratio"),),
            raw_artifacts=("/data/run_1.csv",),
            notes="ok",
        )
        assert ExperimentResult.model_validate_json(r.model_dump_json()) == r

    def test_reasoning_trace(self) -> None:
        t = _trace(4)
        assert ReasoningTrace.model_validate_json(t.model_dump_json()) == t

    def test_verification_result(self) -> None:
        t = _trace(2)
        v = VerificationResult(
            trace_id=t.id,
            step_verdicts=(
                StepVerdict(step_index=0, verdict="VALID"),
                StepVerdict(step_index=1, verdict="INVALID", details="bad inference"),
            ),
            consistency_score=0.5,
        )
        assert VerificationResult.model_validate_json(v.model_dump_json()) == v

    def test_hitl_request(self) -> None:
        h = HITLRequest(
            agent="design",
            reason="cost > threshold",
            payload={"estimated_cost_usd": 50.0},
        )
        assert HITLRequest.model_validate_json(h.model_dump_json()) == h


class TestValidation:
    def test_confidence_out_of_range(self) -> None:
        with pytest.raises(ValidationError):
            Hypothesis(
                question="q",
                statement="s",
                rationale="r",
                confidence=1.5,
            )

    def test_reasoning_step_forward_reference_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ReasoningStep(index=1, claim="c", claim_type="inference", referenced_step_indices=(2,))

    def test_reasoning_step_negative_reference_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ReasoningStep(index=2, claim="c", claim_type="inference", referenced_step_indices=(-1,))

    def test_reasoning_trace_indices_must_be_dense(self) -> None:
        with pytest.raises(ValidationError):
            ReasoningTrace(
                agent="x",
                steps=(
                    ReasoningStep(index=0, claim="a", claim_type="premise"),
                    ReasoningStep(index=2, claim="b", claim_type="inference"),
                ),
            )

    def test_verification_result_rejects_duplicate_indices(self) -> None:
        trace = _trace(2)
        with pytest.raises(ValidationError):
            VerificationResult(
                trace_id=trace.id,
                step_verdicts=(
                    StepVerdict(step_index=0, verdict="VALID"),
                    StepVerdict(step_index=0, verdict="VALID"),
                ),
                consistency_score=0.9,
            )

    def test_experiment_design_cost_must_be_non_negative(self) -> None:
        with pytest.raises(ValidationError):
            ExperimentDesign(
                hypothesis_id=uuid4(),
                protocol="p",
                estimated_cost_usd=-1.0,
                estimated_duration_minutes=1.0,
            )

    def test_unknown_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Hypothesis(
                question="q",
                statement="s",
                rationale="r",
                bogus_field=1,  # type: ignore[call-arg]
            )


class TestSemantics:
    def test_hypotheses_are_hashable_and_frozen(self) -> None:
        h1 = Hypothesis(question="q", statement="s", rationale="r")
        h2 = Hypothesis(
            id=h1.id,
            question="q",
            statement="s",
            rationale="r",
            confidence=h1.confidence,
            created_at=h1.created_at,
        )
        assert hash(h1) == hash(h2)

    def test_confidence_score_basis_preserved(self) -> None:
        c = ConfidenceScore(value=0.8, basis="z3 verified all steps")
        assert c.basis.startswith("z3")

    def test_default_timestamps_are_utc(self) -> None:
        h = Hypothesis(question="q", statement="s", rationale="r")
        assert h.created_at.tzinfo is not None
        # Reasonable bound: created within the last minute.
        assert (datetime.now(UTC) - h.created_at).total_seconds() < 60
