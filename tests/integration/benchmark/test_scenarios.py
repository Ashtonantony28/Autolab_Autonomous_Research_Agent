"""Three benchmark scenarios complete end-to-end without raising."""

from __future__ import annotations

import pytest

from autolab.config import HITL_COST_USD, HITL_DURATION_MIN
from scripts.run_benchmark import _scenarios, run_all


@pytest.mark.asyncio
async def test_all_three_scenarios_complete_cleanly() -> None:
    summary = await run_all()
    designs = {sc.id: sc.design for sc in _scenarios()}
    for row in summary:
        design = designs[row["scenario"]]
        if design.estimated_cost_usd > HITL_COST_USD:
            assert len(row["hitl"]) == 1
            assert "estimated cost" in row["hitl"][0]
        elif design.estimated_duration_minutes > HITL_DURATION_MIN:
            assert row["hitl"] == [f"estimated duration 120 min > {HITL_DURATION_MIN:.0f} min"]
        else:
            assert row["hitl"] == []
    assert sum(1 for row in summary if row["hitl"]) == 3
    assert len(summary) == 3
    scenario_ids = {row["scenario"] for row in summary}
    assert scenario_ids == {
        "protein_crystallization",
        "compound_activity",
        "materials_optimization",
    }
    for row in summary:
        assert row["confidence"] >= 0.7, f"{row['scenario']}: low confidence"
        assert row["hypotheses"] == 1
        assert row["results"] == 1
        assert row["traces"] == 4
        assert row["verifications"] == 4
