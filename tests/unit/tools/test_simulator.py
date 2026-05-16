"""LabSimulator determinism, noise, and instrument routing."""

from __future__ import annotations

from uuid import uuid4

from autolab.state.models import ExperimentDesign, ExperimentParameter
from autolab.tools.lab_hardware import HardwareAdapter, LabSimulator


def _design(protocol: str, *params: ExperimentParameter) -> ExperimentDesign:
    return ExperimentDesign(
        hypothesis_id=uuid4(),
        protocol=protocol,
        parameters=tuple(params),
        estimated_cost_usd=1.0,
        estimated_duration_minutes=1.0,
    )


def test_simulator_satisfies_protocol() -> None:
    assert isinstance(LabSimulator(), HardwareAdapter)


def test_same_seed_same_result() -> None:
    design = _design(
        "liquid handler sweep",
        ExperimentParameter(name="vol", value=10.0),
        ExperimentParameter(name="noise_sigma", value=0.5),
    )
    s1 = LabSimulator(seed=42).run(design)
    s2 = LabSimulator(seed=42).run(design)
    assert s1.measurements[0].value == s2.measurements[0].value
    assert s1.measurements[0].name == s2.measurements[0].name


def test_different_seed_different_result() -> None:
    design = _design(
        "liquid handler sweep",
        ExperimentParameter(name="vol", value=10.0),
        ExperimentParameter(name="noise_sigma", value=0.5),
    )
    s1 = LabSimulator(seed=1).run(design)
    s2 = LabSimulator(seed=2).run(design)
    assert s1.measurements[0].value != s2.measurements[0].value


def test_zero_sigma_is_deterministic_baseline() -> None:
    design = _design(
        "plate reader run",
        ExperimentParameter(name="a", value=10.0),
        ExperimentParameter(name="b", value=20.0),
    )
    result = LabSimulator(seed=99).run(design)
    assert result.measurements[0].value == 15.0  # mean of params
    assert result.measurements[0].name == "absorbance"


def test_imaging_instrument_routing() -> None:
    design = _design("imaging at 488nm", ExperimentParameter(name="x", value=5.0))
    result = LabSimulator(seed=1).run(design)
    assert result.measurements[0].name == "intensity"


def test_fallback_measurement_when_no_keyword_matches() -> None:
    design = _design("custom procedure", ExperimentParameter(name="x", value=1.0))
    result = LabSimulator(seed=1).run(design)
    assert result.measurements[0].name == "synthetic_yield"


def test_no_numeric_params_returns_zero_baseline() -> None:
    design = _design("liquid mix", ExperimentParameter(name="label", value="manual"))
    result = LabSimulator(seed=1).run(design)
    assert result.measurements[0].value == 0.0


def test_design_seeded_rng_when_no_seed_given() -> None:
    design = _design(
        "imaging",
        ExperimentParameter(name="a", value=1.0),
        ExperimentParameter(name="noise_sigma", value=0.1),
    )
    s = LabSimulator()
    r1 = s.run(design)
    r2 = s.run(design)
    # Same design ID -> same RNG seed -> identical noisy measurement value.
    assert r1.measurements[0].value == r2.measurements[0].value
