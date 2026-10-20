# AUDIT-21: Encoding soundness, round 6 (false negatives)

Auditor: an independent agent that wrote none of the code and none of the earlier audits.
Base: `origin/claude/sg27-real-timeouts` at `492980e` (SG24–SG27 merged).
Scope: `src/autolab/verification/*`, `agents/*`, `orchestrator/*`.

Every finding has a repro in `tests/audit/test_audit_21.py`. Each test asserts the correct
behaviour and is marked `xfail(strict=True, reason="AUDIT-21 #n")`. Under `--runxfail`
each one fails on its stated assertion, 10 of 10 runs. That is 5 failing cases, plus one
plain control test that passes. All repros run on `FakeLLMClient`. None calls a real LLM.

"Admitted" means `VerificationMonitor().decide(verdict)` returns PASS, or, for the Design
agent, that a design is merged with no cost/duration (`policy`) pause where principle 7b
requires one. The repros use the default graph's cost trigger (`cost_trigger(10, 60)`).
Two limits are accepted and are not reported: premises are unverified assumptions, and
faithfulness is off for clients that are not live.

Summary: 0 critical, 0 high, 1 medium, 2 low.

Neither the verifier nor the Design agent's own path admits a wrong trace or an
understated design. SG24's tokenizer and bounded probe, SG26's pool and SG27's
incremental loading all held (see "Probed and held up"). The medium finding is in the
human-approval path. When the Design agent withholds a design over its figures, `approve`
re-checks the cost trigger on the design's own figures. It does not use the larger
figure that SG25 says drives the trigger.

---

## Medium

### #1 Approving a Design figure request re-checks the cost trigger on the understated figures

**Impact.** SG25 withholds a design as a `verification` request with
`payload["feedback"]` in two cases:
- its text states a figure (`_stated_figures`);
- its fields are more than 3x from the independent estimate (`_disagreement`).

The monitor retries the request twice, then escalates it to a human. On `approve`,
`_resolve` runs the Design policy, `merged_policy(cost, "current_experiment")`, as
AUDIT-5 #3 requires. That policy reads `estimated_cost_usd` and
`estimated_duration_minutes` from the withheld design. In both cases those are the
figures the request was raised against. The larger figure never reaches the trigger:
- **Disagreement.** The estimate (240 min) was computed but is not kept.
- **Stated figure.** `_stated_figures` returns before `design.estimate` is called, so no
  estimate exists.

The repro uses the graph's own $1 / 5 min design (`tg.DESIGN`). Its trace verifies, and
the human approves:
- **`stated-48-hours`.** The protocol says "Incubate the plate for 48 hours". The human is
  asked about the wording ("protocol states cost or duration figures ['48 hours'];
  these belong only in estimated_cost_usd / estimated_duration_minutes").
- **`estimate-240-min`.** The independent estimate is 240 min.

In both cases the run goes on to Execute and Analysis and completes. No cost/duration
pause is raised. Principle 7b requires a pause before any run of more than 1 hour. Here
the only pause was about the wording, or about the size of the gap, never about the
duration. AUDIT-5 #3 established that approving one kind of request does not approve the
other.

Medium, as AUDIT-5 #3 was: a human is in the loop, and in the disagreement case the
reason quotes the estimate's figure.

**Repro.** `test_approving_a_figure_request_still_pauses_on_the_larger_figure` (2 cases).
Preconditions: the design pause is a `verification` request, and `retry_count == 2`.
Assertion: after `approve`, the graph pauses again with a `policy` request. It fails
here, because the run completes.

**Root cause.**
- `src/autolab/orchestrator/graph.py:118`: `merged_policy(cost, "current_experiment")`.
- `src/autolab/orchestrator/checkpoints.py:152-153`: the policy re-check on `approve`.
- `src/autolab/agents/design.py:91-102`: the figure request is built before, or instead
  of, `_apply_policy`. It carries neither the estimate nor `_at_least(design, estimate)`.

**Suggested fix.** Use either of:
- in `__call__`, also run `hitl_trigger(_at_least(design, estimate))` on a disagreement,
  and append its reason, as `_apply_policy` already does for a blocked trace;
- store the estimate in `hitl_payload`, and make the Design entry in `policies` check
  `_at_least(design, estimate)`.

For a stated-figure request, call the estimator before withholding, so that some larger
figure exists. Neither fix parses a figure from text.

---

## Low

### #2 A policy pause raised on the design's own figures does not name the estimate's larger figure

**Impact.** When the design's own fields already fire the cost trigger, `_apply_policy`
returns the base request unchanged (`design.py:123-124`). Its reason names only the field
that fired. The repro design is $15 / 30 min, and its estimate is within 3x:
- **`duration-80-min`.** The estimate says 80 min. The reason is
  `estimated cost $15.00 > $10.00`, so the human approves a $15 run and is never told it
  takes more than an hour.
- **`cost-44-usd`.** The estimate says $44, and the human is shown $15.

`approve` on a `policy` request merges with no re-check. A pause happens, so principle 7b
is met in form. But the human decides on a figure that SG25's own rule ("the larger of
each pair drives the trigger") has already replaced.

**Repro.** `test_policy_pause_names_the_estimate_that_raised_a_figure` (2 cases).
Assertion: the reason contains `independent estimate`. It fails here.

**Root cause.** `src/autolab/agents/design.py:123-124`.

**Suggested fix.** When `raised`, always run the trigger on `_at_least(design, estimate)`.
Put that reason in the request, or append it to the existing one.

### #3 An inconclusive premise check leaves the pool, so a premise that contradicts it is assumed

**Impact.** A `premise` is assumed only if it is consistent with the facts and every
prior VALID step (AUDIT-1 #4). A premise whose own check is inconclusive gets
UNGROUNDED/`timeout`. That happens on a timeout, on a check that hits the `_SETUP_SHARE`
guard, or on a check that `SolverExecutor.shutdown()` interrupts (`_INTERRUPTED`). Such a
verdict blocks nothing when it is not the final step, and the premise does not enter
`formalized`. A later premise that contradicts it is therefore checked without it, and is
assumed.

The repro trace:
- step 0: "The well is not ready", formalized with a Fermat n=3 conjunct, which runs to
  its timeout;
- step 1: "The well is ready";
- step 2: `ready => plated`;
- step 3: the conclusion `plated`, citing steps 1 and 2.

It scores 0.875 and passes. The same trace with step 0 decided in time (`(not ready)`)
makes step 1 INVALID/`premises_inconsistent`, and the trace is blocked. That is the
control test. The checks have wall-clock timeouts, so under enough load an
easy-but-not-trivial premise times out too. Whether such a trace passes therefore
depends on scheduling.

Low: the conclusion rests only on the assumed premise and the facts. A trace that just
leaves out step 0 is admitted by design (premises are assumptions). What is lost is the
AUDIT-1 #4 guarantee that the admitted premises are jointly consistent. The Research
contradiction trigger fails closed on this (`checkpoints.py`, "could not rule out a
contradiction"), so principle 7c is unaffected.

**Repro.** `test_inconclusive_premise_does_not_let_a_contradicting_premise_pass`. Control:
`test_contradicting_premises_are_blocked_when_decided_in_time`, which passes.

**Root cause.**
- `src/autolab/verification/vericot.py:192-200`: the premise check, which pools only VALID
  prior steps.
- `src/autolab/verification/vericot.py:205-206`: only VALID steps enter `formalized`.
- `src/autolab/agents/verification_monitor.py:57-67`: `blocking_reason` ignores a
  non-final UNGROUNDED step.

**Suggested fix.** Use either of:
- once a premise's check is inconclusive, mark every later premise inconclusive too
  (UNGROUNDED/`timeout`), so the trace cannot pass while its premise set is undecided;
- make an inconclusive premise check block, as a non-VALID final step does.

---

## Probed and held up

**SG24, the tokenizer.** `_TERM_TOKEN` was compared with Z3 4.16's lexer:
- `p:named`, `!p`, `2x` (read as `2` then `x`), `2.5e3` (`2.5` then `e3`), `x,y` (one
  symbol in both, since `,` is a symbol character in Z3), `#o17`, `0x1F`, a string with
  `\"`, non-ASCII letters, and ` ` before `!`.
- Every case either splits the same way or is a Z3 lexer error, which gives
  `formalization_failed`.
- Every `:` outside a string starts a token that begins with `:`, so `:named`,
  `:pattern` and `:weight` are always refused.
- `!` is a symbol character in both lexers, so a hidden `!` is never its own Z3 token.

**SG24, the applied probe.**
- For k ≤ 3 numeric arguments, the 2k + 2 variants are every Int/Real combination
  (8 = 2³ at k = 3).
- For k = 4, a scan was run over all 72,636 identifier strings in `libz3` (113 s). It
  looked for a name that resolves at one of the 6 skipped mixes and at none of the 10
  probed variants, and found none. k ≥ 5 was not scanned.
- Non-numeric argument sorts are kept as written, and user sorts are passed through
  `sort_decls`.
- A sort declared after its use in the same block fails the parse check, so it is
  refused.

**SG25, without a human.**
- A design whose text implies a larger figure is withheld whenever an honest estimator
  reads it. The checks:
  - the 3x ratio and the $1 / 10 min floors;
  - `raised`, which feeds `_at_least` to the trigger;
  - the blocked-trace path, which appends the policy reason.
- A figure request is retried with its reason (PASS + feedback) and then escalated.
- A non-finite cost or duration is refused, by model validation or by `design_facts`. An
  error request merges nothing.
- Nothing outside the trigger reads `estimated_*`.
- The estimate is itself an LLM output. A prompt injection in the protocol is outside
  `FakeLLMClient`'s reach, and is the same kind of limit as faithfulness.

**SG26.**
- Each job has its own `Future` and `_Interrupt`. `_worker.interrupt` is set and cleared
  around each call, and a cancel raises out of `verify`.
- `shutdown` gives running and queued checks `_INTERRUPTED` (UNGROUNDED), never VALID.
- On a conclusion, the tautology check and the circular-premise check fail closed on
  UNGROUNDED. The only verdict that changes with scheduling is a premise's (#3).

**SG27.**
- A prefix that is unsat implies a full pool that is unsat, so `_load` can only give
  `premises_inconsistent` early.
- `unknown`, the deadline and an interrupt all give UNGROUNDED.
- Re-setting the timeout only shortens it. A check that overruns is caught by
  `time.monotonic() >= deadline`.
- `claim!`'s defining assertion is in the loaded vector, and it is satisfiable on its
  own.
- The `true` shortcut requires `consistency == sat` and gives the verdict the full check
  would have given. The tautology check passes `true` as a premise, not as a claim, and
  a literal `true` conclusion is still refused as a tautology.
- Incremental mode may make an honest nonlinear pool UNGROUNDED, which is a false
  positive, not a false negative.

**Verifier and monitor.** The payload binding, the pass-2 carry-over, the score cap and
`blocking_reason` are unchanged since AUDIT-17. Its probes still apply.

## Re-check of earlier soundness fixes

At `492980e`, `uv run pytest tests/audit/test_audit_{1,5,9,12,13,17}.py -q` gives 104
passed and 4 xfailed. The 4 are strict xfails that were already open, AUDIT-9 #4 among
them, and none is a soundness fix that has regressed. AUDIT-17 #1–#4 now pass as plain
tests through SG25's estimate.
