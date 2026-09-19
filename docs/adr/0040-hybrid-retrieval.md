# ADR-0040 — Hybrid retrieval: keyword and vector pools, fused by rank, with recency and story diversity

- **Status:** accepted, and **`retrieval.mode: hybrid`** — set by the project
  owner on 2026-09-19 on the measured bench below, before anything was
  compiled, so no recorded decision or graph was invalidated
- **Milestone:** M14 (an M3 mechanism)

## Context

Every evidence site — compiler, agent prefixes, single-model baselines —
ranked by one thing: distance from a 384-dimension embedding. Small dense
models blur named entities, so "the FTC's review of the Acme–Globex merger"
and a generic antitrust story sit close together. An article seventeen months
old ranks the same as one from the day before the cutoff. And syndicated
copies of one wire story can fill all six of an agent's slots.

## Decision

**A second SQL function, `chronofence_search_hybrid`** (migration 018), beside
the untouched `chronofence_search`:

- Two candidate pools, **both time-locked in SQL**: the existing HNSW vector
  pool (byte-identical to migration 014's), and a keyword pool over a per-
  partition **expression GIN index** on `to_tsvector('english', body)`.
  `published_at < as_of` appears in each pool and again over their union.
- **No `k` parameter and only literal LIMITs** — ADR-0026's finding that a
  parameterised limit in a non-inlinable SECURITY DEFINER function cost 4x.
- SECURITY DEFINER, pinned `search_path`, `as_of` without a default,
  deterministic `(…, chunk_id)` tie-breaks; `cascade_sim` gets EXECUTE and
  nothing on the tables.
- The keyword pool is **one `plainto_tsquery` per entity term** (≤ 8), ordered
  by how many distinct terms a chunk matches, then by exact distance.
  `websearch_to_tsquery` over a 30-word query ANDs every lexeme and matches
  nothing; OR-ing matches the corpus; `ts_rank` has no IDF and re-parses every
  body it ranks. Terms come from `cascade/retrieval/keywords.py` (pure): proper
  noun runs and the actor's own name, failing closed.
- The index is built by `cascade retrieval index --fts`, not a migration
  (ADR-0012's lifecycle rule), and `retrieval verify` reports its readiness.

**Fusion in pure Python** (`fusion.py`): reciprocal-rank fusion over vector
rank, keyword rank and **recency rank** — recency is a ranking, never a filter,
and `FusionParams` refuses a recency weight large enough for the newest bottom
candidate to outrank the oldest top one. Then **story diversity**: at most two
chunks per story (document id, or SimHash within 8 bits of the story's first
chunk), a same-ordinal chunk from a sibling document deferred as a copy, and
`k` backfilled. SimHash rather than MMR over embeddings: the corpus already
fingerprints documents, and returning `halfvec`s would cost ~300 kB a query.

**One switch.** `Chronofence.retrieve` dispatches on `retrieval.mode`, and every
evidence site goes through it — compiler, agents, baselines, the dossier pool
(ADR-0037) and the injection probe; a static test counts them. A
`TimeLockViolation` tripwire raises (never filters) on any candidate dated at or
after `as_of`.

**Measured, not assumed.** `cascade retrieval bench --relevance` runs both arms
over the study's real queries as `cascade_sim` and reports, with a salt-seeded
paired bootstrap over scenarios: the rate of chunks mentioning a scenario's
named parties (two readings — `party_names` include boilerplate such as "PM ET"
and "Other", screened by their background rate), median age at the cutoff,
distinct documents, mean embedding distance (the cost), overlap, and latency.
It decides nothing by itself; switching the mode re-keys every recorded
decision and graph, so it is the owner's call on the numbers.

## Verified on a scratch database (2026-09-19)

A throwaway pgvector 0.8.0 / PostgreSQL 16 container, never the study database: all 19 migrations applied cleanly; the sealed registry rebuilt from the source cache to the same manifest (`91ccd314…`); 40,000 synthetic chunks across 39 quarterly partitions (registry question text, random vectors); `retrieval index --fts` built 39 GIN indexes in 2.9 s; `retrieval verify` passed with the hybrid function deployed; and **all 23 hybrid integration tests passed**, including the two planner assumptions (the keyword scan uses the expression index; its ordering is not served by HNSW). At 40,000 rows, not 2 million: the planner may choose differently at scale, which is why the tests run again on the real corpus.

## Measured on the rebuilt corpus (2026-09-19, 1,998,127 chunks)

`retrieval verify`: 25/25 partitions with both indexes, hybrid function deployed.
The 23 integration tests pass, including the two planner assumptions, and the
leakage and property suites pass (74 tests): the poison pill planted for every
scenario was never returned through the keyword pool.

`cascade retrieval bench --relevance`, 180 scenarios, paired bootstrap over
scenarios (B = 10,000, salt-seeded); every interval below excludes zero:

| | compiler evidence (k=60) | baseline evidence (k=6) |
|---|---|---|
| chunks naming a registry party | 0.5508 → 0.6452 (+0.0945) | 0.7676 → 0.8257 (+0.0581) |
| same, generic names screened | 0.4947 → 0.5903 (+0.0956) | 0.7197 → 0.7842 (+0.0645) |
| median age at the cutoff (days) | 183.15 → 139.29 | 141.07 → 70.22 |
| distinct documents | 42.94 → 49.44 | 4.55 → 4.86 |
| mean embedding distance (the cost) | 0.7954 → 0.8186 | 0.6962 → 0.7212 |
| latency p50 / p95 (ms) | 101 / 451 → 310 / 1019 | 48 / 241 → 263 / 336 |

The two arms share 43% and 38% of their chunks. 25 of 464 registry names
were screened as generic (e.g. "all", "other", "trump", "united states"), and
7 scenarios had no entity term, so their hybrid query is vector plus recency.
These show the mechanisms working, at a stated semantic cost; they are not
evidence about forecast accuracy. The study makes about 2,500 searches, so the
latency difference is minutes, not a design constraint. The runbook
order is `db migrate` → `retrieval index --fts --dry-run` → `retrieval index
--fts` (est. 20–60 min, 0.9–1.7 GB, unverified) → `retrieval verify` → the 23
integration and 13 leakage/property tests (the poison-pill through the keyword
pool is the one that matters) → `bench --relevance` → the M3 bench again, to
confirm the vector path's p95 did not move under the larger working set.

## Verification offline

215 new tests; 45 of 45 seeded mutants killed, including the time lock removed
from the keyword pool, an `as_of` default in SQL and in Python, recency as a
hard filter, a parameterised LIMIT, `SELECT` on `chunks` granted to
`cascade_sim`, and a call site reverted to `fence.search`.
