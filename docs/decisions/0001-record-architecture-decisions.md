# ADR-0001: Record Architecture Decisions

- **Status:** Accepted
- **Date:** 2026-05-16

## Context

AutoLab's architecture choices are non-trivial (a non-bypassable symbolic verifier, multi-agent orchestration with HITL, structured-only LLM outputs). Without a written record, the *why* behind each choice is lost the first time a future contributor wonders whether to swap a library or change an interface.

## Decision

We use [Architecture Decision Records](https://github.com/joelparkerhenderson/architecture-decision-record) in the Nygard style. Every non-obvious technical choice gets a numbered ADR in `docs/decisions/NNNN-title.md` with sections: **Status, Context, Decision, Consequences**.

ADRs are append-only. Reversing a decision means a new ADR superseding the old one (mark the old one `Status: Superseded by NNNN`).

## Consequences

- Onboarding has a single place to find rationale.
- PR reviews can require an ADR for material changes (verifier swap, orchestrator change, storage swap, etc.).
- A small process tax on contributors — accepted, because the alternative is rediscovering tradeoffs every six months.
