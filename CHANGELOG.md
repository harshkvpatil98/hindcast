# Changelog

One entry per milestone, recording what shipped and what the acceptance numbers
**actually were**. The full record, including every defect found and fixed
along the way, is the build log in [`CLAUDE.md`](CLAUDE.md).

Versions track milestones rather than releases; nothing here is published to a
package index.

---

## M14 — Forecast quality: a benchmark, a held-out split, better evidence

In progress. The corpus, retrieval and evaluation mechanisms are done and
measured; the study numbers still need a batch-capable model provider.

- **The market's own price at each cutoff is now the benchmark**
  ([ADR-0039](docs/adr/0039-market-price-benchmark.md)). 143 usable prices of
  180: stale, missing and non-market scenarios are excluded and counted, never
  imputed, and `cascade_sim` cannot read the table.
- **A dev/test split declared before any forecast existed**
  ([ADR-0038](docs/adr/0038-dev-test-split-and-declared-analyses.md)): 40 dev /
  125 test, pinned and re-checked on every evaluation path, with the headline
  computed on test alone. Two analyses were declared with it: accuracy by
  evidence quality, and 12 evidence chunks per agent instead of 6, in its own
  Holm family. Three ways a tuned number could have reached the headline were
  closed, including a Holm family open to any stored configuration.
- **Two time-lock leaks found, both with honest dates and later text.** The
  Wikipedia adapter rendered old revisions through today's templates — a 2025
  article carried the 2026 final standings
  ([ADR-0041](docs/adr/0041-wikipedia-as-of-wikitext.md)) — and CC-NEWS pages
  were dated by what they state while the stored text is the later fetch, which
  for 5.5% of documents was more than 180 days later
  ([ADR-0044](docs/adr/0044-date-text-by-when-it-was-knowable.md)). The stored
  corpus was repaired in place: 200,122 documents re-dated, 340 Wikipedia
  documents purged, and a new scan for update stamps later than a document's
  date went from 381 flagged chunks to 4 — all four publisher typos.
- **Hybrid retrieval** — a time-locked keyword pool fused with vector distance
  and recency, and near-duplicate suppression
  ([ADR-0040](docs/adr/0040-hybrid-retrieval.md)). Switched on after measuring
  it: chunks naming a scenario's own parties rose from 0.55 to 0.65 for the
  compiler and 0.77 to 0.83 for the baselines, and median evidence age at the
  cutoff fell from 183 to 139 days and 141 to 70 days.
- **Deeper, time-locked Wikipedia evidence**: titles chosen from the question
  rather than from party names used as page titles, revisions read as they
  stood before the cutoff, redirects and page moves followed as they read then.
  Scenarios with at least one on-topic article rose from 127 to 147.
- **A cited situation report per scenario**
  ([ADR-0037](docs/adr/0037-scenario-dossier.md)), built from ~100 pre-cutoff
  chunks, every claim machine-checked against the excerpts it cites so the
  writer's memory of the outcome cannot reach the agents as evidence. Off until
  it can be judged on the dev partition.
- **15 sealed scenarios are exchange placeholder legs** and are excluded from
  scoring and counted ([ADR-0043](docs/adr/0043-registry-placeholder-legs.md));
  the sealed set itself is unchanged.
- **An adversarial-document probe** for prompt injection through the corpus
  (threat T3), and a blend whose weight is fitted on dev and scored on test.
- **Platform follow-ups**: an opt-in egress tier for the ingest, model access
  and a durable LLM cache for the study task, GuardDuty findings routed to the
  alerts topic, and both caches in the recovery plan
  ([ADR-0042](docs/adr/0042-ingest-egress-model-access-and-durable-caches.md)).

## M10 — Model providers on AWS

Four providers behind the one call site, and the first model-produced number
since M3. Built and tested; the live AWS criteria are blocked.

- **Four providers, one door.** `llm.provider` selects the Anthropic API,
  Claude Platform on AWS, Amazon Bedrock, or the Claude Code CLI. The three
  SDK clients ship in the one `anthropic` package, so invariant 5 needs no
  exemption ([ADR-0028](docs/adr/0028-model-providers-behind-one-door.md)).
- **Bedrock cannot carry the simulate phase.** It has no Message Batches API,
  and the phase fits its $240 ceiling only at the batch rate. A batch with
  anything to submit is refused before any spend (exit 3).
- **Routing explicit, identity ambient.** Both AWS clients read
  `AWS_REGION` and base-URL variables from the shell before the region; the
  seam passes every routing value from config, verified against decoys.
- **Priced by the logical model, per provider**, with empty AWS price tables
  that refuse to record rather than guess a rate.
- **The API providers share one cache namespace — conditionally.**
  `cascade eval equivalence` bootstraps cross-provider against within-provider
  disagreement; divergence makes the provider join the key
  ([ADR-0029](docs/adr/0029-api-providers-share-one-cache-namespace.md)).
  Not yet run: no two API providers were available.
- **Bedrock Knowledge Bases rejected** — they cannot enforce the time lock the
  leakage suite verifies. **Guardrails deferred** — no path through the one
  door ([ADR-0030](docs/adr/0030-knowledge-bases-rejected-guardrails-deferred.md)).
- **`claude_code`: a local provider on a Claude subscription**, keyed apart
  because the CLI cannot set `temperature` or `max_tokens`. Measured before it
  was trusted: 448 tokens of harness context per call, thinking on by default
  (235 → 0 output tokens once disabled), and this repository's ~27k-token
  `CLAUDE.md` one working directory away from every call — now refused
  ([ADR-0031](docs/adr/0031-claude-code-cli-provider.md)).
- **Parametric memorization, measured for the first time** (through
  `claude_code`, not the pinned configuration): 180/180 parsed, median
  confidence 0.70, direction correct on 97/180 — not distinguishable from
  chance — and a probe Brier of 0.2616 against climatology's 0.2500.
- **The README stated three cost targets as the study's price.** `$290`,
  `$0.0035/run` and `$0.008/run` are §1 targets; they are now described as the
  cost model they are. M9 recorded that a grep for contract literals over the
  README returned nothing; it did not.
- **The environment was rebuilt.** The database volume was lost; the registry
  was re-fetched and re-sealed at `91ccd314…`, a new frozen split. The corpus
  was not rebuilt, so compile remains blocked.

## M9 — Presentation

The repository made readable and runnable from a clean clone. 2 of 3 criteria
met; the optional dashboard deferred.

- **`README.md`** applies the measured-values contract to the front page, with
  six stated limitations including the two criteria still missed. (M10 found
  that it also stated three §1 cost targets as the study's price, and corrected
  it.)
- **`make demo`** — registry → four ablation cells → 25-run replay →
  provenance chain → report artifact, entirely on the stand-in decider, with no
  credential and no spend. `make verify` and `make install-full` are new.
- **CI**: a gates job and a separate invariants job, a pull-request template
  carrying the gate checklist, two issue templates; `CONTRIBUTING.md`,
  `LICENSE` (MIT) and an index of the 27 ADRs.
- **The CI workflow was run in a CI-equivalent environment first, and it
  failed**: without the `embed` extra, mypy could not resolve the lazily
  imported `torch` and `sentence_transformers`. Fixed with the same override the
  other optional imports use; re-measured at 1,193 passed, 1 skipped.
- **The quickstart did not work**: `make install` omitted the `kernel` extra
  the demo needs. `cascade trace explain` now defaults to the first stored run,
  so the documented demo line needs no `psql`.
- **Deferred:** the read-only dashboard — optional in the spec, and there were
  no study numbers to put in it.

## M8 — Strata: determinism and provenance

Replay verification, provenance chains, and the cost-ledger gate.

- **25/25 runs replay byte-identically** across fresh interpreters under a
  differing `PYTHONHASHSEED`, in 24.3 s. A mismatch reports the first divergent
  step with both state hashes rather than only "the hashes differ".
- **Provenance chains complete in 0.27 s.** The specification's recursive CTE
  recurses over all six antecedents and expands 6^depth — measured on a real
  run it did not return. Its own example output is a single path
  ([ADR-0027](docs/adr/0027-provenance-chain-is-a-path.md)).
- **The cost ledger gate refuses to pass vacuously.** Two defects made it
  report "0.0000% — reconciles" over 452,328 calls costing nothing:
  `runs.llm_calls` counted decisions rather than model calls, and zero agreed
  with zero. Both fixed; the gate now has three verdicts.
- **Retrieval regression closed on recall.** recall@20 **0.8950 → 0.9675**
  (criterion met); p95 **176.00 → 90.92 ms** (criterion still missed, still
  reported). Two independent causes: a parameterised `LIMIT` in a
  non-inlinable function cost 4×, and IVFFlat does not scale past ~2M chunks
  ([ADR-0026](docs/adr/0026-hnsw-and-bounded-candidate-pool.md)). HNSW also
  retires the index-drift class that had interrupted M4 through M7 in turn.

## M7 — Assay: the evaluation harness

Scoring, the ablation grid, statistics, and the report artifact.

- Brier, Brier skill score, Murphy decomposition with its binning residual,
  clipped log loss, mid-rank AUC, Wilson intervals, ten-bin calibration,
  isotonic recalibration — all written from their definitions, because the
  declared dependency set has no scipy or scikit-learn.
- Paired bootstrap over **scenarios** (B = 10,000) with Holm–Bonferroni across
  the family, seeded from the study salt so intervals reproduce.
- **Two of four ablation factors were configuration fields nothing read.** Six
  of the twelve cells would have run as duplicates and the headline deltas
  would have come back as nulls with confidence intervals attached
  ([ADR-0025](docs/adr/0025-ablation-factors-a-and-c.md)).
- Climatology scored over all 180 scenarios: Brier **0.250000**, AUC
  **0.5000**, ECE **0.0000** — the exact closed form for a constant base-rate
  forecast, which is what makes it a validation of the metric stack.
- Report artifact writes all ten Appendix D files plus four SVG figures. A
  quantity that could not be produced is `null` and named, never substituted.

## M6 — Chorus: ensemble at scale

The fan-out, batch submission, and forecast aggregation.

- Wavefront batching: one batch per step across the whole wave, **24
  submissions** for the study instead of 864,000
  ([ADR-0020](docs/adr/0020-wavefront-batching.md)).
- Hartigan's dip test implemented from its definition and validated against
  closed-form cases rather than transcribed
  ([ADR-0021](docs/adr/0021-dip-test-from-definition.md)).
- Resumable by set difference: kill the pool mid-phase and it resumes with
  exactly the runs that did not finish, adding zero duplicate events.

## M5 — Loom + Aperture

The simulation kernel and the information-asymmetry layer.

- Four arbiter property tests pass under Hypothesis: bounded, conserving,
  permutation-invariant, monotone.
- One scenario runs 24 steps end to end; `WorldState` round-trips exactly,
  byte-identically across processes.
- The whole step's randomness is drawn at once, so an ablation runs the
  identical stream through a different policy
  ([ADR-0015](docs/adr/0015-run-rng-draw-plan.md)).

## M4 — Lathe: the decomposition compiler

Draft → critique → repair → validate, into a typed `CausalGraph`.

- All six validator rules, with `OutcomeRule` monotone by construction
  ([ADR-0014](docs/adr/0014-outcome-rule-and-utility-term.md)).
- Acceptance numbers need 540 Sonnet calls and remain **unmeasured**; the
  pipeline is verified end to end against a live database with only the model
  stubbed.

## M3 — Chronofence: time-locked retrieval

- p95 **13.43 ms**, recall@20 **0.9372** at 409,899 chunks. *(Both superseded
  at M8 — see above.)*
- **0 of 500** planted post-cutoff documents retrieved across all 180 cutoffs,
  with a positive control asserting the poison *is* reachable without the time
  filter.
- Date-monotonicity property test over the full retrieval trace.

## M2 — Evidence corpus

- **1,950,912 chunks** across **492,270 documents**; zero NULL, future or naive
  dates over the full table; 100% embedding coverage.
- Ingest order turned out to be a correctness concern: chronological units left
  a 1.76M-chunk corpus with 99.6% of its evidence before 2018 against 169 of
  180 cutoffs after 2024 ([ADR-0023](docs/adr/0023-corpus-unit-granularity-and-demand-ordering.md)).

## M1 — Scenario registry

- Exactly **180** scenarios, YES rate **0.5000**, no domain above **25.0%**.
- `manifest.sha256` sealed and asserted by every later phase; a mutated label
  is caught in both directions.
- `cascade_sim` gets `InsufficientPrivilege` on `scenario_labels` — the
  separation is a grant, not a convention.

## M0 — Foundations

- Repo scaffold, typed settings, the single LLM call site with
  record/replay/live, exact-`Decimal` cost metering with per-phase ceilings,
  Langfuse tracing that degrades to a no-op.
- Record → replay round trip makes **zero** network calls, asserted against a
  transport that raises on any request.
