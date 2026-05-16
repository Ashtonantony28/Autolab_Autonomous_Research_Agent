"""Minimal stub `LabSimulator`. Full implementation lands in Phase 5.

Phase 3 only needs a deterministic "run a design, get measurements back"
seam so the ExecuteAgent has something to call. The Phase 5 version will
implement a proper `HardwareAdapter` protocol with noise models per
instrument; for now we return one measurement per numeric parameter.
"""

from __future__ import annotations

from dataclasses import dataclass

from autolab.state.models import ExperimentDesign, ExperimentResult, Measurement


@dataclass
class LabSimulator:
    """Deterministic simulator: emits a `yield` measurement derived from params."""

    def run(self, design: ExperimentDesign) -> ExperimentResult:
        numeric = [float(p.value) for p in design.parameters if isinstance(p.value, (int, float))]
        synthetic_yield = sum(numeric) / max(len(numeric), 1) if numeric else 0.0
        return ExperimentResult(
            design_id=design.id,
            status="success",
            measurements=(Measurement(name="synthetic_yield", value=synthetic_yield, unit="arb."),),
            raw_artifacts=(),
            notes="simulator stub (Phase 3); replaced in Phase 5",
        )


__all__ = ["LabSimulator"]
