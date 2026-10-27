# ADR-0008: An independent estimate guards a design's cost and duration

- **Status:** Accepted (default pending the owner's answer on the SG25 decision card)
- **Date:** 2026-10-20

## Context

The cost (> $10) and duration (> 1 h) pause (principle 7b) reads only a design's
`estimated_cost_usd` and `estimated_duration_minutes`. Since SG17 the guard against a
design that understates them has been `protocol_figures`, a word-list scan of the
protocol and parameter text. Three audit rounds (AUDIT-12 #1, AUDIT-13 #3, AUDIT-17 #1–#3)
found ordinary phrasings it misses ("3 nights", "a week", `incubation_hours = 48`, success
criteria), and AUDIT-19 #1 found ordinary protocols it rejects (`TOP10`, "8-week-old",
`Day 1:`). A list of spellings cannot be complete, and reading figures out of text with
patterns is what CLAUDE.md §4 and §9 rule out.

## Decision

- After verification, the Design agent makes a separate structured call,
  `design.estimate`, through `LLMClient.call_structured` with the `DesignEstimate`
  schema. Its prompt (`build_estimate_prompt`) holds the protocol, parameters and
  success criteria, never the design's own figures, so the estimate is independent.
- The larger of each pair (design, estimate) drives the agent's `hitl_trigger`.
- If either pair is more than `ESTIMATE_DISAGREEMENT_RATIO` (3x) apart, compared at
  floors of $1 and 10 min, the design is withheld as a `verification` request whose
  `payload["feedback"]` names the gap.
- `protocol_figures` stays as a narrow check for figures stated outright (`$500`,
  `240 minutes`); a match is withheld the same way, without the estimate call. It no
  longer has to be complete, so it was narrowed to drop AUDIT-19 #1's false positives,
  and it normalizes text (NFKC, no format characters, Latin look-alikes) for AUDIT-17 #4.
- The monitor node retries a `verification` request that carries `feedback` on a PASS
  or RETRY verdict, with that reason (after the verdict summary) as the agent's
  feedback. Exhausted retries escalate as before.

## Consequences

- One more LLM call per design attempt. Mocked runs register it with
  `tests.fixtures.agent_helpers.register_estimate`; a missing one is a `KeyError`.
- The guard is now as good as the estimator. An estimator that also understates is not
  caught, and the AUDIT-17 repros show the guard acting on an honest estimate, not on
  the text itself.
- The estimate is keyed by the design's text, so two designs with the same text get the
  same estimate. A retry that only changes the figures closes a gap.
- An approved `verification` escalation re-runs the cost trigger on the design's own
  figures (`checkpoints.make_hitl_node`), not on the estimate. When the estimate alone
  would fire the trigger, the request's reason says so, so the reviewer sees it.
- The estimate is not stored in state or traces; it appears only in a request's reason.
