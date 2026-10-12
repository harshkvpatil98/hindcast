# ADR-0032 — Live mode: forward questions, forecast from a frozen intake snapshot

- **Status:** proposed and parked — requested by the project owner on 2026-09-19 and, the same day, deferred by the owner until the backtest and platform work is finished. Nothing in this record is built or scheduled; it is kept so the numbering has no hole and the reasoning is not lost.
- **Milestone:** M13
- **Extends the specification's scope** — flagged here rather than adopted silently (where this project and the spec diverge, say so). The spec remains authoritative for the backtest.

## Context

The specification describes a backtest: every question has already resolved,
every cutoff is in the past, and the time-locked corpus — Chronofence, the
poison-pill suite, invariant 1 — exists so that hindsight is impossible. A
product answers *forward* questions ("will X happen by March?"). There the
cutoff is now, hindsight is impossible by construction, and a pre-built corpus
is the wrong instrument: stale by the time a question arrives, hours of ingest
to extend (measured this week at ~36 chunks/s on the development machine), and
gigabytes to hold.

The change is contained, and that was checked in the code rather than assumed.
Evidence enters the simulation as **data**. The compiler takes `retrieve` as an
injected callable (`cascade/decompose/compiler.py`). Agent evidence is assembled
in the CLI shell (`cascade/cli.py`, three sites): for each actor, one
Chronofence search at the cutoff, handed to `brief_from(evidence=...)`. The
kernel, the arbiter and Aperture never retrieve. Live mode therefore changes
where evidence comes from, not how the simulation runs.

## Decision

**Two modes, one engine.** Backtest mode is unchanged and remains the
validation instrument. In live mode a question is accepted with its resolution
criterion, evidence is gathered at intake and frozen into an immutable,
content-addressed snapshot **before any simulation step**, and everything
downstream reads only the snapshot.

1. **Intake is two rounds, both before the freeze.** Question-level evidence
   feeds the compiler, replacing its `k_compiler` Chronofence call; after
   compilation, one search per actor replaces the per-actor Chronofence query.
   Then the snapshot is frozen: its id is a SHA-256 over the canonical, sorted
   evidence set, and `as_of` is the freeze timestamp, passed explicitly —
   invariant 1 still holds, because `as_of` is set, never defaulted.

2. **Agents never search.** No server tool appears in any agent request.
   Searching inside the simulation would give each replicate different
   evidence, so the ensemble's spread would measure search variance instead of
   the question's uncertainty; it would break replay, since search results move;
   and it would multiply searches by the decision count, undoing ADR-0019's
   once-per-actor evidence in the cached prefix.

3. **Snapshots never enter the backtest corpus.** They live in their own
   append-only tables, with the grant pattern of migration 009, embedded with
   the pinned model. Measured: the sealed registry's cutoffs run to 2026-07-22,
   and 131 of 180 fall within the last 18 months. A live fetch today of an
   article published before one of those cutoffs would land inside that
   scenario's evidence window, and the backtest would drift with no code change
   to explain it.

4. **Evidence is fetched according to the provider.** Anthropic's server-side
   web search is available through `anthropic` and `aws` and not through
   Bedrock (per the SDK's platform-availability table). Intake uses it through
   the one door — so the search call is cached, metered and replayable like any
   other — and a Bedrock deployment fetches through a news or search API via
   the corpus's `Fetcher`, with its politeness budgets and no model involved.
   `claude_code` is excluded: it is local-only (ADR-0031).

5. **Every live forecast is registered prospectively.** At freeze time the
   registry records the question, the resolution criterion, the snapshot id,
   the configuration, the forecast and its dispersion — append-only.
   Resolutions arrive later in a separate table readable only by
   `cascade_eval`: the live counterpart of `scenario_labels`, and of invariant
   2. Resolved forecasts are scored by the existing M7 metric stack.

6. **The live ensemble size is measured, against a criterion set in advance.**
   Interactive latency rules out the Batches API, whose turnaround is in hours,
   so live runs are unbatched and cost roughly twice the batched rate per run
   (ADR-0020). The live replicate count is the smallest *n* on §9.3's
   convergence curve at which the half-width of the 95% bootstrap interval of
   the ensemble mean is **≤ 0.05**. That threshold is fixed when this ADR is
   accepted, **before** the curve is measured, so the replicate count cannot be
   chosen to flatter a latency or cost number.

7. **Unvalidated output says so.** A live forecast is shown beside the
   backtest's measured calibration. While the backtest numbers do not exist, it
   says that **above** the forecast — the same rule the report applies to runs
   made with the stand-in decider.

## Rationale

**The backtest does not automatically validate the product.** It measures the
pipeline on time-locked, crawl-derived evidence; live mode feeds it
search-ranked, fresher evidence — a different input distribution. Backtest
skill is evidence, not proof, of live skill. Decision 5 is the answer: the
product's own accuracy is measured prospectively on questions it answered
before they resolved, and a backtest Brier is never quoted as the live
product's accuracy.

**Freezing before simulating preserves what this project cares about most.**
Replay, provenance and the append-only log carry over unchanged, because the
simulation cannot tell a frozen snapshot from a time-locked retrieval: both are
a fixed evidence set at an explicit `as_of`.

## Cost of being wrong

If snapshots leaked into the backtest corpus, the backtest would change
silently — decision 3 and a grant make that a permission error, not a
discipline. If agents searched, the ensemble would measure the wrong thing —
decision 2 is enforced by a test, not by review. If the replicate count were
tuned after the fact, the live forecast's stability claim would be circular —
decision 6 fixes the criterion first.

## Acceptance (M13 — measured, none yet)

1. One live question end to end — intake, freeze, compile, simulate, forecast
   — and a re-run from the frozen snapshot in a fresh process reproduces the
   event-log hash byte for byte.
2. No agent request carries a server tool: a static check and a behavioural
   check over a full run.
3. Snapshots and the registry are append-only (`UPDATE`/`DELETE` are
   permission errors), and the live simulation role cannot read resolutions.
4. The live replicate count is read off the measured convergence curve under
   decision 6's threshold, and published with the curve.
5. Per-question cost and latency (p50/p95) are measured and inside a new
   `live` budget ceiling.
6. `cascade live score` reports the registered count, the resolved count and
   the metrics, and exits 3 below a minimum resolved count rather than printing
   a Brier over a handful of questions. Prospective skill accrues over months;
   it is not an M13 number.

## Verified by

Nothing yet — this record is proposed. The two facts it rests on were measured
on 2026-09-19: evidence reaches the simulation only as data (the code sites
above), and the sealed registry's cutoff distribution (decision 3).
