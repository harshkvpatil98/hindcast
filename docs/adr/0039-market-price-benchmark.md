# ADR-0039 — The market's own price at the cutoff is a benchmark, and only a benchmark

- **Status:** accepted (2026-09-19)
- **Milestone:** M14 (an M7 mechanism)

## Context

171 of the 180 sealed scenarios are prediction-market questions. The registry
read each market's price exactly once — to learn which way it resolved — and
discarded everything else. The study's baselines were climatology, a single
model asked directly, and the same model sampled 200 times. None of them is
the benchmark a forecasting reader asks for first: *what did the market say
on the day you claim to be forecasting from?*

A Brier score has no natural scale. "0.19" is good on coin-flips and poor on
near-certainties; "better than the market's price at the same instant, on the
same questions" means something on its own. It is also the comparison most
likely to be lost, and a study that omits it invites the assumption that it
was.

## Decision

**1. The benchmark is the last observation strictly before `cutoff_ts`.**
Strictly: an observation stamped at the cutoff is not used, in the selector,
in the row's CHECK constraint (`observed_at < cutoff_ts`) and in the request
windows, which end at the cutoff. The same rule Chronofence enforces on
evidence, so the system and its benchmark stand at the same instant.

**2. A price is never imputed.** A scenario with no market, no history before
the cutoff, or only a stale price is **excluded from the market baseline and
counted**, by reason. Any comparison against the market is paired on the
intersection and prints the paired count. Scoring a missing price as 0.5 would
hand the market a well-calibrated forecast it never made.

**3. A stale price is stored and reported, never scored.** The bound is 24
hours (`market_baseline.max_staleness_hours`), chosen from how the sources
sample and recorded with its reasons in `configs/base.yaml`: Polymarket's
series is per-minute, so an older last price means the market was not quoting
across the cutoff; and an old price gives the system an information head start
over its benchmark — a bias in the system's favour, the direction that must
never be silent. It is not to be revisited against a Brier.

**4. The price never enters `forecasts`, and `cascade_sim` cannot read it.**
`market_prices` (migration 017) grants SELECT to `cascade_eval` and nothing to
`cascade_sim`. The price is pre-cutoff information, not an outcome, so
invariant 2 does not strictly require this. It is done anyway: whether agents
should see the market is a separate experiment nobody has decided to run, and
until it is, a market price reaching an agent would turn "beats the market"
into "was told the market". `forecasts` is readable by `cascade_sim`, so the
baseline is scored from its own table by routing inside the store, and
`eval score`, `eval status`, `report` and the Holm family reach it unchanged.

**5. Only fields fixed at write time are read.** Polymarket's market document
carries `outcomePrices` — the resolution — beside the token ids; it is never
read here. Manifold bets carry `isFilled`, `amount` and `fills`, which keep
changing after the cutoff; only `createdTime`, `probAfter` and `isRedemption`
are used.

## Measured (2026-09-19, the sealed set `91ccd314…`)

358 requests at ≤ 2/s; replayed offline from the source cache, the identical
180 rows with 0 requests.

| | scenarios |
|---|---|
| usable price | **143** — all Polymarket; observed 0.04–79.7 s before the cutoff (p50 29.6 s) |
| stale, excluded | 6 — every Manifold market; last bet 32.7–542.6 h before the cutoff |
| not a market | 9 — the curated questions |
| no history in the 14 days before the cutoff | 21 |
| market created after its own cutoff | 1 |
| fetch failed | 0 |

19 usable prices are below 0.01, so the existing log-loss clip matters;
probabilities are stored unclipped and clipped once, where every other
forecast is.

**The 21 + 1 were the finding.** A market with no price history before its
cutoff is a market nobody was trading, and that turned out to describe
placeholder legs the volume screen had never applied to — see ADR-0043.

## Consequences

- `cascade eval market-prices` fetches and stores; `cascade eval baselines
  --baseline market` scores. `make migrate` must run first: `available_configs`
  reads `market_prices` and says so, naming the migration, if it is absent.
- The market baseline is flagged `in_spec=False`: it is a sixth baseline the
  specification did not ask for, and the report says which are which.
- BSS against climatology for this baseline is computed over the priced subset
  while the stored climatology describes all 180 — the same mismatch a capped
  ablation cell already has. A paired climatology would fix both; not done.
- Migration 017 applied cleanly to a scratch PostgreSQL 16 holding the rebuilt sealed
  registry, and its **5 integration tests passed** there (sim denied, eval read-only,
  the CHECK constraints). Not yet applied to the study database, which was carrying
  the corpus ingest.
- 112 unit tests; 24 of 24 seeded mutants killed, including `<=` at the cutoff
  in three places, a 0.5 imputed for a missing price, the NO token priced, and
  SELECT granted to `cascade_sim`.
