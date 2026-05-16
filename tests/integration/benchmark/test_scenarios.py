"""Three benchmark scenarios complete end-to-end without raising."""

from __future__ import annotations

import pytest

from scripts.run_benchmark import run_all


@pytest.mark.asyncio
async def test_all_three_scenarios_complete_cleanly() -> None:
    summary = await run_all()
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
