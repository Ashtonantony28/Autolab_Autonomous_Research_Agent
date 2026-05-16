"""Error-injection harness — verifier catches >=80% of the invalid traces."""

from __future__ import annotations

import pytest

from scripts.error_injection import run
from tests.fixtures.golden_traces import ALL


@pytest.mark.asyncio
async def test_catch_rate_meets_dod_threshold() -> None:
    report = await run()
    invalid_total = sum(1 for g in ALL if g.expected_overall == "INVALID")
    valid_total = sum(1 for g in ALL if g.expected_overall == "VALID")

    catch_rate = report.invalid_caught / invalid_total
    assert catch_rate >= 0.8, f"catch rate {catch_rate:.2%} below 80% target"
    assert report.valid_classified_valid == valid_total, "valid traces should be admitted"
