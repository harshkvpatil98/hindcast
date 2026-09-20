# ADR-0049 — Agent tool access is a measured factor, and `as_of` is unrepresentable in a tool payload

- **Status:** accepted
- **Milestone:** M15
- **Constrained by ADR-0020 (the batch wavefront) and ADR-0019 (evidence in the
  cached prefix); applies ADR-0047's interface argument to a payload the model
  writes**

## Context

An agent in this system is a stateless policy function. `LLMAgents.decide` is
one `client.complete()` returning one of seven enum actions; the agent cannot
look anything up, cannot ask a question and cannot compute. That is not an
oversight — it is what makes the ADR-0020 wavefront possible, because a
decision that depends on nothing but its own request can be submitted in a
batch with 36,000 others. It is also the ceiling on how much reasoning a turn
can express: the agent's entire view of the world is fixed before it is asked
to act, and if the evidence it was given at run preparation does not bear on
what it now sees, there is nothing it can do about that.

Whether that ceiling costs the study anything is an empirical question nobody
here has an answer to. This record is about building the other arm so the
question can be asked, and about the two properties that arm must not break.

The first is the time lock. ADR-0019 retrieves evidence once per (scenario,
actor) at run preparation, where `as_of` is read from the sealed registry by
code the model never touches. A tool arm changes that: the model now writes
part of a retrieval request. Every previous seam in this system either had
`as_of` passed by the study (Chronofence, the compiler) or could not be handed
it at all (the reranker, ADR-0047). This is the first seam where the model
supplies the payload, and "the model must not set the cutoff" is therefore, for
the first time, a property about untrusted input rather than about our own
call sites.

The second is the batch shape. §12.1 prices the simulate phase at $126 with the
50% batch rate and $252 without, against a configured ceiling of $240. ADR-0020
records that this makes batching a functional requirement rather than an
optimisation, and that a batch per step across the whole wave is the only
submission shape that completes in a useful time.

## Decision

**Three things, and the third is the one that matters for the study's claims.**

**1. Two tools, `lookup_evidence` and `recall`, on a surface that cannot
express a cutoff.** `LookupEvidenceArgs` has one field, `query: str`, and
`extra="forbid"`. `as_of` is bound to a `ToolBelt` at construction, keyword-only
and undefaulted (invariant 1), from the run's own scenario cutoff; the belt
holds it privately, passes only `args.query` into the injected retrieval port,
and never renders it into a result or an error. `recall` searches the one
memory string the kernel rendered for this actor's turn — §6.3's `(run_id,
actor_id)` namespace — and there is no constructor, method or argument in
`cascade/sim/tools.py` that takes two.

**2. A separate decider, `ToolUsingAgents`, that does not claim to be
batchable.** It runs a loop bounded by `kernel.tools.max_turns`, with the last
permitted turn pinned to the action tool. It has no `prepare` method, so the
runner's `_prepare` falls back to deciding turn by turn. `LLMAgents` is
untouched: two system blocks, one tool, one call, byte-identical requests and
therefore byte-identical cache keys.

**3. Tools are an ablation factor, not an upgrade.** The tool arm is a cell of
the grid measured against the arm without it. It does not replace the headline
configuration and cannot, for the arithmetic in *Consequences* below.

## Rationale

**Unrepresentable, not forbidden.** ADR-0047 made the point for the reranker:
a stage that is never handed `as_of` cannot default it, and that is a stronger
guarantee than a rule about not passing it. The same shape is available here
and is worth more, because the payload is written by the thing the rule would
be constraining. Concretely, three independent barriers, each checkable:

- *Type.* `LookupEvidenceArgs` has no time field, so `args.as_of` is a mypy
  error. An executor that wanted to honour a model-supplied cutoff cannot be
  written without first editing the argument model, which is a diff a reviewer
  reads.
- *Validation.* `extra="forbid"` turns `{"query": ..., "as_of": ...}` into a
  `ValidationError`, not a dropped key. This is the barrier that matters most
  and it is the one easiest to get wrong: a model with `extra="ignore"` would
  answer the query and silently discard the date — correct behaviour that is
  indistinguishable, from both sides, from having honoured it. A refusal is
  observable; a silent drop is not.
- *Binding.* The belt is constructed per (scenario, actor) with the cutoff the
  registry holds, and `ToolUsingAgents._belt` checks that the belt it found
  names the actor it was asked for rather than trusting the dictionary key —
  because a mis-keyed belt retrieves under another scenario's lock and returns
  something that looks entirely normal.

A fourth, deliberately redundant: `ToolBelt.admissible` drops anything at or
after the bound cutoff *after* retrieval returns it. That is a backstop and not
the mechanism — it only ever sees what was already returned, so it cannot
substitute for Chronofence. It exists because the retrieval port is *injected*,
which means a future adapter or a test stub could hand this module post-cutoff
rows, and the failure direction of a boundary that trusts its input is a leak
that reads as evidence. The leakage suite asserts the count is zero against the
real corpus *and* non-zero against a stub built to leak, so the backstop is
known to be capable of firing.

**Retrieved documents are quoted, not pasted, and a tool result is the case
that most needs it.** `cascade eval injection` measured a chunk that addressed
the model in the operator's voice being obeyed on 20 of 30 scenarios; the
vector was impersonating the frame rather than issuing instructions. A tool
result arrives mid-conversation in a block the provider itself labels as
harness output — it *is* the frame the attack was imitating. So a tool result
goes through `cascade.quoting.quote_documents`, the same renderer, the same
markers, the same stated count that the cached prefix uses. There is one way
this system quotes a document and the newest arrival does not get its own.

**Evidence retrieval goes through `Chronofence.retrieve`, not `search`.**
ADR-0047 made `retrieve` the one place the retrieval mode and the rerank stage
are decided. An arm that called `search` would read the corpus differently from
the arm it is being compared against, and the measured difference would be
retrieval rather than tools — the confound §10.2 already guards against for the
baselines. The adapter that binds the two lives in `cascade/sim/tools.py` and
calls exactly one method, so that property is structural rather than a note.

**A tool arm cannot carry the headline, and the reason is the batch shape.**
ADR-0020's third submission shape — one batch per step across the whole wave —
works because nothing in a step's first four stages depends on any decision
taken in that step, so every turn in the wave can be assembled before any is
resolved. A tool loop breaks that at the level of a single decision: turn 2's
request contains turn 1's tool result, so the wave cannot be assembled at all.
The arm therefore runs unbatched, at list price, at up to `max_turns` calls per
decision. Taking §12.1's own figures — $252 for 36,000 unbatched runs — a
non-headline cell at Q1's budget cap (90 scenarios × 30 replicates = 2,700 runs)
is on the order of $19 per turn-per-decision, which is affordable, and the same
arm over the full grid is not. That is arithmetic from the cost model, not a
measurement: §12.4 forbids launching a phase without `simulate estimate`, and
this arm is exactly the case that rule was written for.

**So tool access enters as a factor, which is also the only honest way to enter
it.** ADR-0025 is the precedent and the warning. Two ablation factors were
configured, documented and inert, and the grid would have executed six
duplicate cells whose headline deltas came back as nulls with confidence
intervals attached — indistinguishable from an honest finding. Adding tools to
`LLMAgents` instead would be the same mistake with the sign flipped: every
number in the study would move, no cell would isolate why, and the report would
attribute to decomposition whatever the tools had done. The same kernel runs
both arms — same 24 steps, same arbiter, same seeded draw plan, same
admissibility — so the only thing that varies is whether the agent could look
anything up.

**Determinism survives because a tool result is a pure function.** M8's
criterion is a byte-identical event-log hash across processes. Each turn goes
through `LLMClient` (invariant 5), so each is content-addressed, cached,
metered and replayable; and the loop's *n*-th request is a pure function of the
first *n−1* responses and the tool results they produced, which are themselves
a function of (query, cutoff, corpus). `search_memory` scores by integer term
overlap and breaks ties on position in the record, so it cannot reorder between
processes. Tool-use blocks are answered in the order the provider returned them
rather than sorted, because that order is part of the recorded response body.

**The turn count is carried, not inferred.** M8 found `runs.llm_calls` counting
decisions rather than model calls, and the cost gate compared zero against zero
and passed. `int(from_model)` was a correct call count only while every
decision that reached the model cost exactly one; here it is not, so
`Decision.model_turns` carries the truth and `ToolUsingAgents.turns_per_decision`
reports the multiplier the arm actually measured.

## Consequences

- **`cascade/sim/kernel.py` still counts `ctx.llm_calls += int(decision.from_model)`.**
  For this arm that under-counts by `model_turns − 1` per decision, which is
  precisely the class of defect M8's criterion 3 exists to catch. The kernel is
  not edited here — it is the most delicate module in the system and this
  change belongs to whoever owns it — so **`runs.llm_calls` is wrong for the
  tool arm until `stage_decide` reads `decision.model_turns`.** Nothing has run
  yet, so nothing has been mis-recorded; this must land before the arm does.
- **Measured static cost of the arm's prefix:** `tool_rules` is 317 estimated
  tokens and the two tool schemas 453, so the tool arm carries **770 tokens**
  more static prefix than the plain arm's 3,068 (`RULES` 1,544 + action schema
  1,524). Both sit below the 4,096-token cache floor without the persona;
  ADR-0019's evidence block is what carries either over it, and
  `prepare_tool_actor` measures the tool arm's own prefix rather than reusing
  the plain arm's number.
- **The arm's per-decision cost is bounded and configured, not emergent.** The
  last permitted turn is pinned to the action tool, so `max_turns` is a hard
  ceiling on calls. Exhausting it is a recorded WAIT carrying
  `tool_budget_exhausted`, on ADR-0018's precedent: one unusable turn costs one
  actor one turn, and the run has 23 more steps of behaviour in it.
- **A cell's `allow` list changes the prompt.** `tool_rules` describes only the
  tools the cell enabled, because an agent told about a tool the executor
  refuses would generate refusals that the arm gets charged for.
- **Two integration points remain for whoever wires the CLI**: building the
  belts (one per (scenario, actor), `as_of` from the sealed registry, search
  from `chronofence_evidence`) and selecting the decider for a cell. Both are
  listed in the M15 hand-off; nothing in `cascade/cli.py` or `cascade/config.py`
  was changed here.
- **No study number exists.** 47 offline tests hold the mechanism and a leakage
  probe holds the time lock against the live corpus. Whether tools move the
  Brier is not decided here, and if the measured delta straddles zero that is
  what the report will say.

## What would change this

If `recall` ever reads a store rather than the turn's own rendered record —
a cross-run memory, a shared scratchpad, anything keyed by more than
`(run_id, actor_id)` — the namespace argument above no longer holds and the
information-asymmetry factor needs its own re-derivation before that lands.

If a tool is ever added that *writes* — to the world, to another actor, to the
event log — this record does not cover it. Everything here is a read, which is
why the arbiter remains the only thing that changes the world and invariant 3
is untouched.

And if the batch API ever supports a multi-turn exchange as one submission,
the arithmetic that confines this arm to ablation scale changes, and the
question of whether the headline configuration should have tools becomes a
budget question rather than a structural one.
