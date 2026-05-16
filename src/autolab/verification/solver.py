"""Z3 wrapper for entailment checking.

Each step is verified by asking Z3: `premises and not claim` — if `unsat`, the
claim is entailed by its premises (VALID); if `sat`, there's a model that
satisfies the premises but contradicts the claim (INVALID); if `unknown`
(timeout or undecidable), we conservatively return UNGROUNDED.

Inputs are SMT-LIB 2 strings, parsed via `z3.parse_smt2_string` so the
boundary between LLM-produced text and Z3's AST is explicit.
"""

from __future__ import annotations

from dataclasses import dataclass

import z3

from autolab.state.models import Verdict


@dataclass(frozen=True)
class SmtFormula:
    """An SMT-LIB chunk plus the declarations it depends on.

    `declarations` is the cumulative `(declare-const ...)` /
    `(declare-fun ...)` block that defines every symbol used by the
    `assertion`. Keeping them separate lets the verifier compose premises
    and claims without re-parsing the full prior history.
    """

    declarations: str
    assertion: str


@dataclass(frozen=True)
class SolverOutcome:
    """Result of an entailment check."""

    verdict: Verdict
    details: str = ""
    counter_model: str = ""


@dataclass
class Z3Solver:
    """Stateless Z3-backed entailment checker."""

    timeout_seconds: float = 5.0

    def check_entailment(
        self,
        premises: list[SmtFormula],
        claim: SmtFormula,
    ) -> SolverOutcome:
        """Return whether `claim` is entailed by `premises`.

        Conventions:
          - No premises → UNGROUNDED.
          - premises and not claim is unsat → VALID.
          - premises and not claim is sat   → INVALID.
          - Any solver error / unknown / timeout → UNGROUNDED.
        """
        if not premises:
            return SolverOutcome(verdict="UNGROUNDED", details="no premises to entail from")

        smt = _build_smt2(premises, claim)
        try:
            assertions = z3.parse_smt2_string(smt)
        except z3.Z3Exception as exc:
            return SolverOutcome(
                verdict="UNGROUNDED",
                details=f"SMT parse error: {exc}",
            )

        solver = z3.Solver()
        solver.set("timeout", int(self.timeout_seconds * 1000))
        solver.add(assertions)
        result = solver.check()
        if result == z3.unsat:
            return SolverOutcome(verdict="VALID", details="premises entail claim")
        if result == z3.sat:
            model = solver.model()
            return SolverOutcome(
                verdict="INVALID",
                details="premises do not entail claim",
                counter_model=str(model),
            )
        return SolverOutcome(
            verdict="UNGROUNDED",
            details=f"solver returned {result}",
        )


def _build_smt2(premises: list[SmtFormula], claim: SmtFormula) -> str:
    """Compose a single SMT-LIB 2 document from premises + negated claim.

    Declarations are deduplicated line-by-line so identical declares across
    premises don't conflict. Order is preserved (first occurrence wins).
    """
    seen: set[str] = set()
    decls: list[str] = []
    for chunk in (*premises, claim):
        for line in chunk.declarations.splitlines():
            stripped = line.strip()
            if not stripped or stripped in seen:
                continue
            seen.add(stripped)
            decls.append(stripped)

    parts: list[str] = [*decls]
    for p in premises:
        parts.append(_wrap_assert(p.assertion))
    parts.append(f"(assert (not {claim.assertion}))")
    return "\n".join(parts)


def _wrap_assert(body: str) -> str:
    body = body.strip()
    if body.startswith("(assert"):
        return body
    return f"(assert {body})"


__all__ = ["SmtFormula", "SolverOutcome", "Z3Solver"]
