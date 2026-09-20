# ADR-0052 — Batching is a cost requirement, so it binds only providers that charge

- **Status:** accepted
- **Milestone:** M16
- **Amends ADR-0020, which made the batch discount functional**

## Context

`cascade simulate all` is refused under the `claude_code` provider:

```
provider 'claude_code' is not ready: 4 uncached request(s) need the Message
Batches API, which the Claude Code CLI does not provide; the batched phase's
ceiling assumes the batch rate (ADR-0020).
```

That refusal is correct as written. ADR-0020 made the 50% batch discount a
functional requirement rather than an optimisation: §12.1 prices the simulate
phase at $126 with the discount and $252 without, against a configured ceiling
of $240. A provider with no Message Batches API would run the phase at roughly
twice the rate its ceiling was set against, so it is refused at the batch door
before any spend rather than falling back silently to one call at a time.

**But the argument is about money, and for one provider there is none.**
`claude_code` runs the Claude Code CLI headless under a flat-rate subscription
(ADR-0031). Its `ProviderSpec.billing` is `subscription`, and
`Settings.pricing_table` returns a zero table for it *in code* — not from
configuration — so every call it serves books `Decimal(0)`. There is no
discount for an unbatched run to lose, and a phase that costs nothing cannot
breach a dollar ceiling by taking longer.

The consequence of not drawing this line is on the record: the study can
*compile* on a subscription but not *simulate*, which is how 154 graphs came to
be compiled on a provider that could never run a single step of the study they
were compiled for.

## Decision

**The fan-out may resolve a wave one call at a time where, and only where, the
provider charges nothing per call. Everywhere else the refusal stands
unchanged.**

The fallback lives in `LLMAgents.prepare()` — the `BatchingPolicy` seam the
runner probes with `getattr` — and **not** in `LLMClient.complete_batch()`.

```python
@property
def submits_batches(self) -> bool:
    provider = getattr(self.client, "provider", None)
    return charges_per_call(provider) if isinstance(provider, str) else True
```

`charges_per_call` is a pure function over the static `PROVIDERS` registry.
`complete_batch` still refuses every provider without a batch endpoint; only
its *message* now distinguishes the two remedies, because a caller told to
"record this phase through `anthropic`" when the real answer is "prepare
serially" has been sent the wrong way.

## Rationale

**The batch door has one contract, and it is shared.** `complete_batch` means
exactly one thing to every caller: these requests go out as one submission at
the batch rate. `eval/baselines.py` calls it too — the self-consistency
baseline is 36,000 calls and the largest single block of the `baseline`
ceiling. A door that sometimes batched and sometimes looped would change that
phase's cost model without a word, and the one function in the system that
knows the discount is functional would also be the function that says it is
optional.

**The refusal and the exemption ask different questions, and Bedrock is where
they come apart.** `supports_batches` is a fact about an endpoint;
`billing` is a fact about an invoice. Bedrock has `supports_batches=False` and
`billing="per_token"` — no batch endpoint *and* per-call pricing — so it must
still be refused. Keeping the two questions in two modules keeps a later edit
from merging them into one `if`. A mutant that keyed the exemption off
`supports_batches` fails three tests.

**The decider already owns wave shape.** `_prepare` finds `prepare` by
`getattr`, and `ToolUsingAgents` deliberately has none, because a tool loop is
multi-turn and cannot ride ADR-0020's 24-batch shape (ADR-0049). "How a wave
can be resolved" is already a property of the decider rather than of the
client. A subscription provider is another instance of the same category.

**Preparation is the contract; the batch is one way to honour it.** This is
the part that is easy to get wrong, and the wrong version looks right. The
tempting fallback is for `prepare` to return 0 and let `decide` reach the model
turn by turn — the runner already handles a decider with no `prepare` at all.
It would produce the same actions and a **different event log**:
`DecisionEvent.canonical` hashes `cache_hit`, so a wave that skipped
preparation writes `cache_hit=False` into every event, and those runs fail M8's
byte-identical criterion against their own recordings. So the serial path fills
the cache through the same door, in the same sorted order — and `decide` still
serves every turn from disk, exactly as the batched path leaves it.

**Duplicates still cost one call.** The batch path deduplicates identical
requests within a wave explicitly; the serial path deduplicates by arriving
second at a content-addressed cache. Same saving, slower route.

**Live mode is refused on both paths, for one reason.** Live bypasses the
cache, so a prepared turn would be answered in `prepare` and asked again at
`decide`. Under a subscription that is two calls against a usage limit rather
than two charges — still twice the work, and two different answers to one turn.

**Measured, not asserted.** The exemption is a claim about dollars, so it is
tested in dollars rather than against the flag that encodes it. On the same
`Usage`:

| provider | batched | unbatched |
|---|---|---|
| `claude_code` | `Decimal(0)` | `Decimal(0)` |
| `anthropic` | *c* | exactly *2c* |

The subscription row is the exemption and the paid row is the control; without
the second, the first would also pass for a meter that had stopped pricing.

**The guard cannot be reached by configuration.** `charges_per_call` reads
`ProviderSpec.billing`, a frozen module constant. It deliberately does *not*
read the price table: `providers.bedrock.pricing` is a YAML field, and an
operator who zeroed it — by accident, or to quiet a readiness error — would
otherwise also switch off the ceiling that guards the study's cost claim. An
unrecognised provider name is charged, and a client that does not say which
provider it is is charged, because failing toward "this is free" is how a phase
silently costs double.

## Consequences

- **The report counts submissions, not preparations.** `FanoutReport.batches`
  and `batched_turns` are what the study's cost claim is read off, so turns
  resolved serially are counted in their own column, `unbatched_turns`. A run
  that resolved its turns one at a time and reported them as batch submissions
  would put a count in the phase report that no provider ever served.
  `cascade simulate all` does not yet print the new column.
- **A subscription phase books nothing, and must go on booking nothing.**
  `trace cost` keeps *nothing to reconcile* apart from *reconciled* (M8), and
  an unbatched subscription run stays on the first side of that line. Booking a
  notional list price for calls a subscription served would invent a spend no
  provider record could match.
- **Invariant 8 is untouched.** The plan is a set difference against the runs
  table and never consults the decider; preparation shape is downstream of it.
- **Wall-clock, not money, is now the cost of the exempt path.** One submission
  per step becomes one request per uncached decision. That is the trade a
  subscription offers and there is no ceiling on it, but it is real and it is
  why this is an exemption rather than a default.
- **`eval baselines` is not covered.** It calls `complete_batch` directly and
  is still refused under `claude_code`, now with a message naming the right
  remedy rather than the wrong one. Giving that phase a serial shape is its
  own change.

### A defect this uncovered, in code this record does not own

`DecisionEvent.canonical` hashes `cache_hit`, which is a property of *when* a
call happened rather than of the run. Measured on a model-backed run of the
single-run driver, 171 events:

| | `cache_hit=True` | hash |
|---|---|---|
| recorded (record mode) | 0 of 171 | `26085526…` |
| replayed (replay mode) | 171 of 171 | `3dd92581…` |

So a run recorded through `cascade simulate run` would never replay to its own
stored hash, and M8 criterion 1 would fail for it. It has never fired because
every stored run is heuristic, where `cache_hit` is false on both sides.

The wavefront is immune, and now visibly so: `prepare` fills the cache before
any decision, so a study run stores `cache_hit=True` and replays to it. That is
a second reason ADR-0020's preparation step is load-bearing, and it is why the
serial path here fills the cache rather than declining to. The fix belongs in
`cascade/trace/events.py` — excluding `cache_hit` from the canonical projection
as `latency_ms` already is — and is left to whoever owns that file.

## Verified by

`tests/unit/test_unbatched_wavefront.py` — 21 tests. The unbatched wavefront
reproduces the batched wavefront's event-log hash byte for byte, and a
preparation that merely declines to batch does not; all three drivers agree on
replay without constructing an SDK client; the meter prices the exemption and
its control; Bedrock, an unknown name and a nameless client are all charged; a
zeroed price table does not exempt Bedrock; a synthetic provider's exemption
follows its billing rather than its name; live mode is refused; duplicates cost
one call; the report's two columns each stay empty on the path they do not
describe; and an interrupted unbatched phase resumes by set difference.

Checked by mutation, not by inspection: eight behaviours were broken on purpose
— the exemption granted to everyone, keyed off `supports_batches`, granted to
an unknown name or a nameless client, read from `settings` instead of the
client, the serial path reduced to a no-op, the live-mode guard removed, and
serial turns counted as submissions — and **8 of 8 were killed**. The suite was
95/95 before and after, and the three source files were byte-identical
afterwards.

## What would change this

If a subscription ever acquired a per-call price, or a provider appeared whose
billing is neither per-token nor flat-rate, `charges_per_call` is the one place
that answers the question and the ceiling argument would have to be re-derived
for it. And if `complete_batch` ever grew a second contract — a caller that
wants "resolve these, however" rather than "submit these as one batch" — that
would be a different record, not an extension of this one.
