"""Golden traces for live formalization, grouped by category.

The live verifier sees only natural-language claims, so the SG1 soundness
traces (whose fixture claims are placeholders such as "premise") get claims
that state their SMT in words. Claim types, citations and expected outcomes
come from the golden trace unchanged. `VOCAB_CONFLICTS` is left out: it
simulates a formalizer that re-declares symbols, which cannot be provoked
from the claim text.
"""

from __future__ import annotations

from dataclasses import dataclass

from autolab.state.models import ReasoningStep, ReasoningTrace
from tests.fixtures.golden_traces import ALL, INVALID, VALID, GoldenTrace


@dataclass(frozen=True)
class LiveCase:
    """One golden trace under a category, optionally with natural-language claims."""

    category: str
    golden: GoldenTrace
    claims: tuple[str, ...] = ()

    @property
    def id(self) -> str:
        return self.golden.id

    def trace(self) -> ReasoningTrace:
        """The trace to verify, with `claims` replacing the golden claims if given."""
        claims = self.claims or tuple(s.claim for s in self.golden.steps)
        if len(claims) != len(self.golden.steps):
            raise ValueError(f"{self.id}: {len(claims)} claims for {len(self.golden.steps)} steps")
        steps = tuple(
            ReasoningStep(
                index=i,
                claim=claim,
                claim_type=gs.claim_type,
                referenced_step_indices=gs.referenced_step_indices,
            )
            for i, (gs, claim) in enumerate(zip(self.golden.steps, claims, strict=True))
        )
        return ReasoningTrace(agent="live", steps=steps)


_BY_ID = {g.id: g for g in ALL}


def _case(category: str, golden_id: str, *claims: str) -> LiveCase:
    return LiveCase(category, _BY_ID[golden_id], claims)


CASES: tuple[LiveCase, ...] = (
    *(LiveCase("valid", g) for g in VALID),
    *(LiveCase("invalid", g) for g in INVALID),
    _case("explosion", "e01-explosion-bool", "P holds", "P does not hold", "Therefore Q holds"),
    _case(
        "explosion",
        "e02-explosion-arith",
        "The integer x is greater than 5",
        "x is less than 3",
        "Therefore x equals 100",
    ),
    _case(
        "dilution",
        "d02-dilution-arith",
        "The integer x is positive",
        "The integer y is positive",
        "The integer z is positive",
        "The integer w is positive",
        "Therefore x + y is greater than z + w",
    ),
    _case(
        "dilution",
        "d04-dilution-hidden-converse",
        "A implies B",
        "C implies D",
        "B holds",
        "D holds",
        "Therefore A or C holds",
    ),
    _case(
        "poisoned",
        "p02-poisoned-modus-ponens",
        "P holds",
        "Q implies R",
        "Therefore Q holds",
        "Therefore R holds",
    ),
    _case(
        "poisoned",
        "p03-poisoned-arith",
        "The integer x is positive",
        "Therefore x is greater than 10",
        "Therefore x is greater than 5",
    ),
    _case(
        "wrong_citations",
        "w02-cites-wrong-bound",
        "The integer x is at least 5",
        "The integer y is negative",
        "Therefore x is greater than 3",
    ),
    _case(
        "wrong_citations",
        "w04-correct-citations",
        "P holds",
        "P implies Q",
        "R holds",
        "Therefore Q holds",
    ),
    _case(
        "quantifiers",
        "q01-universal-instantiation",
        "For every integer n, H(n) holds",
        "Therefore H(a) holds for the integer a",
    ),
    _case(
        "quantifiers",
        "q04-forall-exists-swap",
        "For every integer x there is an integer y such that R(x, y)",
        "Therefore there is an integer y such that R(x, y) holds for every integer x",
    ),
    _case(
        "arithmetic",
        "a03-linear-system",
        "For integers x and y, x + y = 10",
        "x - y = 2",
        "Therefore x = 6",
        "Therefore y = 4",
    ),
    _case(
        "arithmetic",
        "a05-off-by-one",
        "The integer x is at least 3",
        "Therefore x is greater than 3",
    ),
    _case(
        "chains",
        "m01-hypothetical-syllogism",
        "A holds",
        "A implies B",
        "B implies C",
        "Therefore B holds",
        "Therefore C holds",
        "Therefore B and C both hold",
    ),
    LiveCase("chains", _BY_ID["m04-assay-chain-uncited"]),
)

CATEGORIES: tuple[str, ...] = tuple(dict.fromkeys(c.category for c in CASES))

ONE_PER_CATEGORY: tuple[LiveCase, ...] = tuple(
    next(c for c in CASES if c.category == cat) for cat in CATEGORIES
)

__all__ = ["CASES", "CATEGORIES", "ONE_PER_CATEGORY", "LiveCase"]
