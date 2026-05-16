"""Hardware adapter contract.

Any concrete adapter (simulator or real instrument driver) implements a
single async-or-sync `run(design)` method. Keeping the surface tiny means
swapping a simulator for a real driver in production is a one-line change.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from autolab.state.models import ExperimentDesign, ExperimentResult


@runtime_checkable
class HardwareAdapter(Protocol):
    """The contract every lab-hardware adapter must satisfy."""

    def run(self, design: ExperimentDesign) -> ExperimentResult: ...


__all__ = ["HardwareAdapter"]
