# Architecture decision records

Forty-seven records here, plus 0032 (live mode, proposed), which lives on the `m13/live-mode` branch. Each states the decision, the evidence behind it, and
what it costs. They exist because this project's specification is detailed
enough to be wrong in specific, checkable ways — and when it was, the record
says so with the measurement that showed it.

Fifteen correct a defect in the specification. Three correct a defect found in
this build. The rest record a choice the specification left open.

| # | Decision | Milestone |
|---|---|---|
| [0001](0001-prompt-cache-minimum-prefix.md) | The static prompt prefix must clear the model's 4,096-token cache floor; the spec's 1,900-token prefix silently does not cache | M0 |
| [0002](0002-chronofence-security-definer.md) | `chronofence_search` needs `SECURITY DEFINER` and a pinned `search_path`; as specified the app role gets a permission error on its own function | M3 |
| [0003](0003-worldstate-relation-keys.md) | `WorldState.relations` uses a canonical string key — a tuple key cannot round-trip through JSON | M5 |
| [0004](0004-quarterly-chunk-partitions.md) | `chunks` partitions are quarterly, not monthly — ~108 monthly partitions is ~100 index scans against a 15 ms budget | M3 |
| [0005](0005-role-grants-deny-by-default.md) | Role grants deny by default; the spec's `REVOKE … FROM cascade_sim` is a no-op | M0 |
| [0006](0006-langfuse-v2.md) | Langfuse pinned to v2 — v3 needs ClickHouse, Redis and MinIO | M0 |
| [0007](0007-cache-key-excludes-cache-control.md) | The cache key excludes `cache_control` markers, so prompt-cache tuning is a pure cost change | M0 |
| [0008](0008-scenario-constraint-precedence.md) | Constraint precedence when the pool cannot satisfy every inclusion rule: eligibility > base rate > domain cap > N | M1 |
| [0009](0009-party-screen-and-metaculus.md) | `named_parties` counts recognised institutional actors, not proper nouns | M1 |
| [0010](0010-corpus-dates-and-window.md) | Corpus dates come from the document, never the crawl; Wikipedia is anchored per scenario | M2 |
| [0011](0011-chunk-cap-verified.md) | The 512-token cap is verified per chunk, not estimated | M2 |
| [0012](0012-ivfflat-index-lifecycle.md) | Index lifecycle is an operational command, not a migration | M3 |
| [0013](0013-ivfflat-probes-under-partition-fanout.md) | `probes` is 40, not 10: partitioning applies probes per scan *(superseded by 0026)* | M3 |
| [0014](0014-outcome-rule-and-utility-term.md) | `OutcomeRule` is monotone by construction, so §5.3's rule holds for any rule that parses | M4 |
| [0015](0015-run-rng-draw-plan.md) | The run RNG draws a whole step at once, so an ablation runs the identical stream through a different policy | M5 |
| [0016](0016-per-target-action-visibility.md) | Action visibility is per target; one enum per policy cannot express the spec's own derivation rule | M5 |
| [0017](0017-worldstate-scheduler-and-pool-ledgers.md) | `WorldState` carries the alliance pool ledger and the scheduler's per-actor history | M5 |
| [0018](0018-action-admissibility.md) | An action the actor cannot take is coerced to WAIT and the reason recorded on the event | M5 |
| [0019](0019-evidence-in-the-cached-prefix.md) | Evidence is retrieved once per (scenario, actor), not per step — 4.2M searches become 2,520 | M5 |
| [0020](0020-wavefront-batching.md) | The fan-out advances runs in lockstep so one batch serves a whole step — 24 submissions instead of 864,000 | M6 |
| [0021](0021-dip-test-from-definition.md) | The dip test is implemented from its definition and validated against closed forms, not transcribed | M6 |
| [0022](0022-aggregation-runs-label-blind.md) | Aggregation runs as `cascade_sim`, so a forecast cannot be conditioned on the outcome it will be scored against | M6 |
| [0023](0023-corpus-unit-granularity-and-demand-ordering.md) | **Found here.** Ingest order is a correctness concern: chronological units left a 1.76M-chunk corpus covering the wrong decade | M2 |
| [0024](0024-report-figures-are-svg.md) | Report figures are SVG from the pinned stack; the declared dependency set has no plotting library | M7 |
| [0025](0025-ablation-factors-a-and-c.md) | **Found here.** Two of four ablation factors were configuration fields nothing read | M7 |
| [0026](0026-hnsw-and-bounded-candidate-pool.md) | **Found here.** HNSW replaces IVFFlat, and a parameterised `LIMIT` in a non-inlinable function cost 4× | M8 |
| [0027](0027-provenance-chain-is-a-path.md) | The provenance walk is a path, not an ancestor set: the spec's CTE expands 6^depth and does not return | M8 |
| [0028](0028-model-providers-behind-one-door.md) | Four model providers behind the one call site; Bedrock has no Message Batches, so it cannot carry a batched phase | M10 |
| [0029](0029-api-providers-share-one-cache-namespace.md) | The API providers share one cache namespace — conditional on a measured equivalence probe | M10 |
| [0030](0030-knowledge-bases-rejected-guardrails-deferred.md) | Bedrock Knowledge Bases rejected: a managed KB cannot enforce the time lock. Guardrails deferred: no path through the one door | M10 |
| [0031](0031-claude-code-cli-provider.md) | A Claude Code CLI provider under a subscription, keyed apart because it cannot honour `temperature` or `max_tokens` | M10 |
| 0032 | *Live mode (proposed) — on branch `m13/live-mode`* | M13 |
| [0033](0033-infrastructure-as-code-terraform.md) | Terraform, pinned and run in Docker; gated offline with mock-provider tests, TFLint and triaged Checkov — no AWS account needed | M11 |
| [0034](0034-aurora-data-plane.md) | Aurora 16.11 with pgvector pinned at 0.8.0, an isolated VPC, the bench as a Fargate task inside it, fixed capacity, and a copy-on-write clone for the partitioning experiment | M11 |
| [0035](0035-platform-design.md) | One dedicated account; a budget derived from the study configuration; SCP guardrails; an event lake that makes append-only two independent controls; recovery tiers set by cost-to-lose | M12 |
| [0036](0036-cutoff-anchored-file-selection.md) | **Found here.** CC-NEWS files are chosen by their distance from each scenario's cutoff: the month queue ranked old background above the final weeks, and could only ever reach a month's first two days. *Amends 0023* | M12 |
| [0037](0037-scenario-dossier.md) | A cited situation report per scenario, built from ~100 pre-cutoff chunks; every claim machine-checked against the excerpts it cites so the writer's memory of the outcome cannot reach the agents as evidence. Off until the development partition decides | M14 |
| [0038](0038-dev-test-split-and-declared-analyses.md) | A dev/test split (40 / 125) declared before any forecast, pinned and checked on every eval path; the headline is test; evidence-tier analysis and 12-vs-6 chunks (S01) declared in advance, S01 in its own Holm family | M14 |
| [0039](0039-market-price-benchmark.md) | The market's own price strictly before the cutoff is a benchmark and only a benchmark: never imputed, stale prices excluded and counted, unreadable by `cascade_sim` | M14 |
| [0040](0040-hybrid-retrieval.md) | Hybrid retrieval: time-locked vector and keyword pools, rank fusion with recency, story diversity by SimHash; one switch for every evidence site; off until `bench --relevance` is run | M14 |
| [0041](0041-wikipedia-as-of-wikitext.md) | **Found here.** Old revisions were rendered through today's templates, so pre-cutoff-dated Wikipedia text carried post-cutoff facts. Now raw wikitext strictly before the cutoff, titles chosen from the question, redirects and moves as they read then, depth in the unit key | M14 |
| [0042](0042-ingest-egress-model-access-and-durable-caches.md) | An opt-in egress tier (NAT + DNS allow-list, Network Firewall rejected on cost-of-mistake) for the one ingest state that fetches; a study task with per-provider IAM and the LLM cache on EFS, copied add-only into the recovery bucket; a read-only OIDC plan workflow; GuardDuty findings routed above a severity nobody defaulted | M12 |
| [0043](0043-registry-placeholder-legs.md) | **Found here.** 15 of 180 sealed scenarios are exchange placeholder legs ("Candidate B"): the volume screen read a leg's zero as missing. Owner's decision: keep the sealed set, exclude the stand-ins from scoring before the split is drawn, inside its pin; the registry fixes wait on branch `m14-registry-v2` | M14 |
| [0044](0044-date-text-by-when-it-was-knowable.md) | **Found here.** CC-NEWS pages were dated by the date they state, but the stored text is the fetched text: 3.7% of a 2026 file were re-crawls of articles over 180 days old. Dated now by max(stated, fetched), provenance kept, and the stored corpus repaired in place by `cascade corpus redate`. *Corrects 0010* | M14 |
| [0045](0045-quoted-evidence.md) | **Measured, then closed.** A document impersonating a system notice steered the forecast on 20 of 30 scenarios; quoting documents in a frame the renderer strips from their own text takes it to 0 of 30. Prompt revision r4 | M14 |
| [0046](0046-publishing-and-recovery-drills.md) | The report artifact carries the labels — `baselines.csv` and `ablation_grid.csv` are one row per scenario *with how it resolved* — so it lands in a private, Object-Locked bucket the simulation is denied, is fetched rather than served, and the recovery path is drilled rather than asserted | M14 |
| [0047](0047-reranking-is-a-permutation.md) | A managed reranker is admissible where a managed knowledge base was not: a knowledge base *replaces* the `published_at < as_of` filter, a reranker *permutes a set the database already filtered*. Enforced by the interface — it is handed bodies and returns numbers, so naming a post-cutoff chunk is unrepresentable. *Amends 0030* | M15 |
| [0048](0048-a-second-record-of-spend.md) | **Found here.** The two records §12.4 reconciles are both written by this process from the same `Usage` object, so they corroborate rather than verify. Reconciliation now compares N sources, lists every one it asked whether or not it answered, and reports `independently_verified` apart from `reconciled`. Bedrock invocation logging does not cover the `bedrock-mantle` endpoint ADR-0028 routes to, so M8 criterion 3 stays blocked — now naming what would unblock it | M15 |
