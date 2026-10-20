"""Imported by the solver's forkserver only (`solver_workers`), never by the caller.

A worker re-runs a main module started with `-m` as `__mp_main__`; it is preloaded
in the forkserver, so `runpy` warns that it is already imported. That is expected.
"""

import warnings

warnings.filterwarnings(
    "ignore",
    message=r".* found in sys\.modules after import of package .*",
    category=RuntimeWarning,
)
