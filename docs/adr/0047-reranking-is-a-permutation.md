# ADR-0047 — Reranking is a permutation, so a managed reranker is admissible

- **Status:** accepted
- **Milestone:** M15
- **Amends ADR-0030, which rejected a managed service inside retrieval**

## Context

Retrieval quality sets a ceiling on everything downstream: the compiler sees
`k_compiler` chunks, each agent sees `k_agent` chunks once per run (ADR-0019),
and no later stage can recover a document that was never retrieved. At M8 the
recall criterion was met (recall@20 0.9675) and at M14 retrieval became hybrid
— vector and keyword pools fused by reciprocal rank, then diversified by
SimHash story (ADR-0040).

RRF is a rank heuristic. It knows nothing about the query beyond the two
orderings it is handed, and it cannot tell that the third result answers the
question while the first merely shares its vocabulary. A cross-encoder
reranker is the standard remedy, and Bedrock offers one as a managed API.

ADR-0030 rejected Bedrock Knowledge Bases for this system. The question this
record answers is whether that rejection generalises to every managed service
inside the retrieval path. It does not, and the reason is worth stating
precisely, because "we rejected the managed thing" and "we adopted the managed
thing" look inconsistent until the line between them is drawn.

## Decision

**A managed reranker is admissible at the ranking stage. No managed service is
admissible at the filtering stage.**

Concretely: `chronofence_search_hybrid` returns a candidate pool already
constrained by `published_at < as_of`. A reranker receives that pool and
returns a permutation of it, truncated to `k`. It is never given the query
before the pool exists, never given `as_of`, and never able to name a document
the pool does not contain.

The default reranker is local and deterministic. Bedrock Rerank is opt-in via
`retrieval.rerank.provider`, and `make demo` runs with no AWS account.

## Rationale

**The time lock is a set-membership property, and a permutation cannot
violate it.** A knowledge base *replaces* the filter: the `as_of` predicate
becomes an optional metadata filter on a request, and a caller who omits it
gets post-cutoff evidence with no error. A reranker *reorders a set that was
already filtered by the database*. The distinction is not stylistic — it is
the difference between a property enforced by a `SECURITY DEFINER` function
and a property promised by a vendor.

This is falsifiable rather than asserted, and three tests hold it:

1. The reranked output is asserted to be a **subset** of the pre-rerank pool.
   A reranker that invented, merged or paraphrased a chunk would fail here.
2. The M3 poison-pill probe runs **through** the rerank path, not around it.
   500 post-resolution documents, queried at every cutoff with their own text;
   the criterion is unchanged at 0 retrieved.
3. `as_of` is not a parameter of the rerank seam at all. It cannot be passed,
   so it cannot be defaulted (invariant 1).

**Determinism is the real constraint, not leakage.** M8's criterion is a
byte-identical event-log hash across processes, and a remote reranker is a
network call whose output can change when the provider updates a model. So the
rerank result is treated exactly like a model call: content-addressed and
cached by (query, ordered pool of chunk ids, rerank model id, top-k), served
from disk on replay, and a cache miss in replay mode exits 4 like any other.
The rerank model id joins the cache domain, so changing rerankers invalidates
recordings instead of silently reordering evidence underneath a stored graph.

**A local default keeps the credential-free path.** The repository's strongest
property is that `make demo` runs with no key, no account and no spend. A
retrieval stage that requires AWS to produce any result would end that. The
local reranker is a deterministic scoring function over the same interface;
the Bedrock one is a provider behind it, chosen by config.

**Why this is where a managed service genuinely earns its place.** Ranking
quality is a model problem with no invariant attached, it is the one part of
retrieval this project has no measured opinion about, and the candidate pool
is small (≤ 200) so the call is cheap. Compare the filter, which is the study's
entire validity claim and is already faster than the budget it was given.

## Consequences

- Retrieval gains a stage, and therefore a latency cost. p95 is already missed
  (90.92 ms against 15 ms) and this will not improve it. The number is
  reported, as before, and nothing is tuned to hide it.
- Retrieval gains a second thing to record and replay. The LLM cache already
  does this; the rerank cache reuses it rather than adding a second mechanism.
- Whether reranking helps is **not decided here**. It lands as an ablation
  factor measured against the current RRF ordering on the dev split, and if it
  does not improve recall or the downstream Brier it is reported as not
  helping and left off.

## What would change this

If a reranker is ever given the query *and* the corpus rather than the query
and a pool — that is, if it becomes a retriever — this record no longer covers
it and ADR-0030 applies instead.
