# ADR-0002: Move heavy dependencies to optional extras

- **Status:** Accepted
- **Date:** 2026-05-16

## Context

`CLAUDE.md` §3 mandates `streamlit` (dashboard) and `chromadb` (RAG) but both are heavy:
`chromadb` transitively pulls `onnxruntime` (~90 MB wheel) and `streamlit` pulls an Arrow/Tornado stack. Installed in the default set, they would dominate `uv sync` time and CI wall-clock — for code that Phase 0-4 doesn't touch.

## Decision

`streamlit` lives in `[project.optional-dependencies].dashboard`; `chromadb` lives in `[project.optional-dependencies].rag`. CI installs `--all-extras` so quality gates still cover them, but local Phase 0-4 work can `uv sync` without them.

Both remain locked dependencies (not optional in the §3 contract sense) — this is purely a packaging organization choice.

## Consequences

- Phase 0-4 contributors get a faster local install.
- CI still covers the optional extras (no drift risk).
- Phase 5 (RAG) and Phase 6 (dashboard) plans must explicitly require `--all-extras` for their integration tests; documented in those phase plans.
- If we ever ship AutoLab as a library, downstream users can opt out of the heavy deps.
