# ADR-0038 — A dev/test split declared before any forecast, and the analyses declared with it

- **Status:** accepted (2026-09-19); re-pinned the same day by ADR-0043
- **Milestone:** M14 (an M7 mechanism)

## Context

The owner wants the system improved, not merely measured: how many evidence
chunks an agent sees, whether retrieval is hybrid, whether a situation report
helps, how to blend two forecasters. Every one of those is a choice made by
looking at accuracy. Made on the 180 scenarios that also produce the headline,
each one inflates it, and by an amount nothing in the report can show. Until
this ADR the project had no development set, so the only safe amount of
tuning was none.

A split is only credible if it was declared before anyone could have chosen
it after looking. That moment is now: no forecast exists.

## Decision

**1. The split.** `cascade/eval/split.py` (pure). Each scenario gets a fixed
score `blake2b("cascade.eval.split/dev-test/v1|<id>", key=study salt)`. Within
each domain the lowest-scoring scenarios are dev, with quotas apportioned by
largest remainder in integer arithmetic (every domain keeps at least one test
scenario). `declare_study_split` first removes the scenarios declared
unscoreable (ADR-0043). **40 dev / 125 test** of the 165 scored scenarios.

- *Outcome-independent by construction*: ids, domains, question wording, a
  salt and a size are the only inputs, and a test asserts the signature.
- *Stable*: a per-id score, not a rank over the set — adding a scenario moves
  at most a domain's boundary (measured on the real registry: 1–4 scenarios),
  where a positional "first 40" would move about 60.
- *Its own purpose string*: without it, all 40 dev scenarios landed inside the
  ablation's 90-scenario subsample, which hashes the bare id.
- *Why 40*: M7's only measured paired interval, at n = 40, had half-width
  0.0154; scaled by √n, holding 40 out widens the headline interval about 13%
  while dev resolves differences the M7 harness could. 30 and 60 are tabulated
  in `configs/base.yaml`. Caveat, stated there too: the M7 figure came from
  stand-in deciders.

**2. The pin.** `eval.split_sha256` holds the fingerprint of the exact
membership, excluded ids included. Every `cascade eval` path and `cascade
report` recomputes the split from the whole sealed registry through the
label-free loader and **exits 3** on a mismatch, an unpinned split, or a
registry whose size is not the seal's.

**3. Headline is test.** `cascade report` computes the headline on test alone
and prints the all-scenario and dev figures beside it, labelled as not the
headline. `headline.md` states the split, its sizes, its fingerprint, that it
was declared before any forecast, and that tuning is legitimate only on dev.
`eval score`, `eval significance` and `eval prompt-audit` default to dev.

**4. A guard with teeth.** `require_dev_only` refuses any scenario set touching
test, an excluded scenario, or an undeclared id; `cascade eval tune-guard`
exposes it; `SplitError` is exit 3. Configurations not declared in code
(C01–C12, the baselines, S01, `base`) are *tuning variants* and are refused off
dev.

**5. Two analyses declared now.**
- *Accuracy by evidence quality* — scenarios tiered by corpus chunks in the
  final 30 days before the cutoff, at fixed thresholds (0 / 1–999 /
  1,000–9,999 / 10,000+), not quantiles, so the tiers cannot be drawn after
  seeing where the errors fall. One pre-declared Spearman test.
- *S01, 12 evidence chunks per agent instead of 6* — the owner's question. A
  supplementary cell (`configs/supplementary/`), run only by `eval grid
  --supplementary`, **Holm-adjusted in its own family**: the twelve Appendix C
  cells' adjusted p-values are asserted identical with and without it.

**6. The blend** (`cascade/eval/blend.py`). The Brier-optimal linear weight
between the simulation and a single model is closed-form; `fit_weight` takes
the dev pairs and `apply_weight` takes no outcomes at all, by signature.

## Leaks found in existing code and closed

- **The Holm family was open.** Any configuration with stored forecasts joined
  it — a forgotten tuning variant would have raised the multiplier on all
  twelve ablation results. It is now the declared cells and baselines.
- **`eval prompt-audit` recorded C01's Brier on all 180** as the "after"
  figure, so running the audit was itself a look at test. Now dev.
- **`report` scored every stored configuration** on every scenario and
  published it.

## Not closed

A headline configuration can still be re-run under an edited `base.yaml` and
re-scored on test; nothing counts test looks. `prompt_revisions` has no
partition column (a migration). Both are named in the report's limitations.

## Verification

187 new tests (unit + property); 24 of 24 seeded mutants killed (positional
split, headline on all, S01 in the twelve-cell family, guard as a no-op, tiers
by quantile, recalibration across the boundary, …). The integration test that
checks the pin against the live registry is written and runs with the next
database pass.
