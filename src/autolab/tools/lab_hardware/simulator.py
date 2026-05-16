"""Deterministic lab simulator covering the three Phase 5 instruments.

Routes by a keyword in `design.protocol`:
  - "liquid"   → one `volume_dispensed_ul` measurement.
  - "plate"    → one `absorbance` measurement.
  - "imaging"  → one `intensity` measurement.
  - else       → fallback `synthetic_yield` (Phase 3 behaviour).

Each measurement is the mean of the numeric parameters plus Gaussian noise
with sigma drawn from the optional `noise_sigma` parameter on the design
(or 0 if absent). The RNG is seeded so tests are reproducible.
"""

from __future__ import annotations

import hashlib
import math
import random
from dataclasses import dataclass, field
from typing import Final

from autolab.state.models import ExperimentDesign, ExperimentResult, Measurement

_INSTRUMENT_KEYWORDS: Final[dict[str, str]] = {
    "liquid": "volume_dispensed_ul",
    "plate": "absorbance",
    "imaging": "intensity",
}


@dataclass
class LabSimulator:
    """Deterministic, seeded simulator implementing `HardwareAdapter`.

    `seed` controls the RNG. If `seed=None`, a stable hash of `design.id` is
    used so identical designs produce identical results across runs.
    """

    seed: int | None = None
    _instrument_keywords: dict[str, str] = field(default_factory=lambda: dict(_INSTRUMENT_KEYWORDS))

    def run(self, design: ExperimentDesign) -> ExperimentResult:
        rng = self._rng_for(design)
        baseline = self._baseline(design)
        sigma = self._sigma(design)
        measurement_name = self._instrument_for(design.protocol)

        value = baseline + rng.gauss(0.0, sigma) if sigma > 0 else baseline
        return ExperimentResult(
            design_id=design.id,
            status="success",
            measurements=(Measurement(name=measurement_name, value=value, unit="arb."),),
            notes=f"simulator(seed={self.seed!r}, sigma={sigma})",
        )

    def _rng_for(self, design: ExperimentDesign) -> random.Random:
        if self.seed is not None:
            return random.Random(self.seed)
        # Stable, design-derived seed when caller didn't pin one.
        digest = hashlib.sha256(str(design.id).encode()).digest()
        return random.Random(int.from_bytes(digest[:8], "big"))

    def _baseline(self, design: ExperimentDesign) -> float:
        numeric = [
            float(p.value)
            for p in design.parameters
            if isinstance(p.value, (int, float)) and p.name != "noise_sigma"
        ]
        if not numeric:
            return 0.0
        return sum(numeric) / len(numeric)

    def _sigma(self, design: ExperimentDesign) -> float:
        for p in design.parameters:
            if p.name == "noise_sigma" and isinstance(p.value, (int, float)):
                return max(0.0, float(p.value))
        return 0.0

    def _instrument_for(self, protocol: str) -> str:
        lowered = protocol.lower()
        for keyword, measurement in self._instrument_keywords.items():
            if keyword in lowered:
                return measurement
        return "synthetic_yield"


def is_finite(x: float) -> bool:
    """Re-export of `math.isfinite` for tests that don't want to import math."""
    return math.isfinite(x)


__all__ = ["LabSimulator", "is_finite"]
