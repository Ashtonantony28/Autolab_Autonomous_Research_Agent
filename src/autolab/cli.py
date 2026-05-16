"""Command-line entry point for AutoLab."""

from __future__ import annotations

import sys

from autolab import __version__


def main() -> None:
    """Print a smoke-test banner. Real CLI lands once orchestration exists (Phase 4)."""
    sys.stdout.write(f"AutoLab v{__version__} - skeleton OK\n")
