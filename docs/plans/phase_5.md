# Phase 5 — Tools & Integrations Plan

**Goal:** A `HardwareAdapter` protocol + a richer `LabSimulator` with parameter-driven noise. A minimal literature tool surface. Skip multimodal RAG / chromadb for now (would pull heavy onnxruntime + need a corpus; defer to a "Phase 5b" if needed).

**Reality check:**
- The dev container has no outbound network — live arxiv/PubMed cannot be tested here. We build the client + RAG-stub against an injected `HTTPClient` protocol so the live path exists but tests run offline.
- Real chromadb is `[project.optional-dependencies].rag`; we do **not** import it in core code paths.

## Modules

- `tools/lab_hardware/__init__.py` — exports `HardwareAdapter` protocol + `LabSimulator`.
- `tools/lab_hardware/protocol.py` — `HardwareAdapter` protocol: `run(design) -> ExperimentResult`. Existing `LabSimulator` already implements this duck-typed; make it explicit via `@runtime_checkable`.
- `tools/lab_hardware/simulator.py` — extend to: (a) a seeded RNG so tests are deterministic, (b) per-parameter noise (Gaussian, configurable σ), (c) a per-instrument dispatch table (`liquid_handler`, `plate_reader`, `imaging`) chosen by `design.protocol` keywords.
- `tools/literature.py` — `LiteratureClient` async class wrapping an injected `httpx.AsyncClient`. Methods: `search_arxiv(query, max_results)`, `search_pubmed(query, max_results)`. Both return `tuple[Citation, ...]`. Live tests gated; unit tests use httpx's `MockTransport`.

## Tests

- `tests/unit/tools/test_simulator.py`:
  - Determinism: same seed → same measurements.
  - Different seed → different measurements.
  - Noise σ=0 reduces to the deterministic baseline.
  - Each instrument keyword produces the expected measurement shape.
  - Negative parameters handled (don't crash).
- `tests/unit/tools/test_literature.py`:
  - arxiv query produces a `LiteratureClient.search_arxiv` HTTP call to the expected URL, parses the canned XML response into `Citation` tuples.
  - PubMed equivalent with canned JSON.
  - Empty response returns `()`.
  - HTTP error surfaces as a typed exception.

## DoD

1. `HardwareAdapter` protocol exists; `LabSimulator` and `FixedResultSimulator` (test helper) both pass `isinstance(sim, HardwareAdapter)`.
2. Simulator noise is deterministic given a seed.
3. Literature client tests cover arxiv + PubMed parsing without hitting the network.
4. `make lint typecheck test` all green.

## Skipped explicitly (with rationale)

- **chromadb / multimodal RAG**: heavy dep, no corpus, no embedding model in container. Documented as deferred in status.md.
- **Real arxiv/PubMed calls**: container has no outbound network. Live path implemented, tests use `MockTransport`.
- **GUI dashboard preview**: Phase 6.
