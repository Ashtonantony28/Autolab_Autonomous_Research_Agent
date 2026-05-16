# Phase 7 — Benchmark & Hardening Plan

**Goal:** Three benchmark scenarios + an error-injection harness for the verifier (must catch ≥80% of injected errors) + a small hardening pass.

## Modules

- `scripts/run_benchmark.py` — CLI entry point. Builds three scenarios:
  1. `protein_crystallization` — temperature sweep against the `LabSimulator`.
  2. `compound_activity` — multiple ligand parameters.
  3. `materials_optimization` — multi-instrument routing.
  Each scenario constructs a fully-stubbed `FakeLLMClient`, runs the graph, and records per-agent metrics.
- `scripts/error_injection.py` — programmatic error-injection harness for the verifier:
  - Generate `N` reasoning traces with deliberate logical fallacies (affirming consequent, etc.).
  - Run each through the `Verifier`.
  - Measure catch rate (fraction classified `INVALID` overall).
  - Print summary.

## Tests

- `tests/integration/benchmark/test_error_injection.py` — runs the harness with 20 known-invalid traces and asserts the catch rate is ≥80%.
- `tests/integration/benchmark/test_scenarios.py` — each of the three benchmark scenarios completes without raising, produces a final state with `confidence ≥ 0.7`, and records metrics for all four agents.

## Hardening (within scope)

- A top-level `autolab run "<question>"` CLI that wires real `ClaudeLLMClient` (if `ANTHROPIC_API_KEY` is set) or `FakeLLMClient` otherwise. Documented but not exercised in tests beyond a smoke import.
- Structlog initialization helper in `monitoring/__init__.py`.

## DoD

1. Error-injection harness shows ≥80% catch rate on the 20-fixture set (we already classify 20/20 in Phase 2's golden traces; this re-uses + extends them).
2. All three benchmark scenarios complete without unhandled exceptions.
3. `make lint typecheck test` all green.
4. `docs/benchmarks/v1.md` summary written.
