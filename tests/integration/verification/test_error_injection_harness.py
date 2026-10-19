"""SG1 bar for the error-injection harness: every injected error caught, no false alarms."""

from __future__ import annotations

import pytest

from scripts.error_injection import CatchRateReport, format_report, run


async def test_all_invalid_caught_and_all_valid_admitted() -> None:
    report = await run()
    assert report.invalid_total > 0
    assert report.catch_rate == 1.0
    assert report.precision == 1.0
    assert report.valid_classified_valid == report.valid_total


def test_precision_counts_false_alarms() -> None:
    report = CatchRateReport(invalid_total=4, valid_total=6, invalid_caught=3, valid_flagged=1)
    assert report.catch_rate == pytest.approx(0.75)
    assert report.precision == pytest.approx(0.75)
    assert report.valid_classified_valid == 5
    assert CatchRateReport(0, 1, 0, 0).precision == 1.0


async def test_report_prints_precision() -> None:
    out = format_report(await run())
    assert "Catch rate:" in out
    assert "(100.0%)" in out
    assert "Precision: 100.0%" in out
