-- 017: the market's own probability at each scenario's cutoff (M14).
--
-- 171 of the 180 sealed scenarios are prediction-market questions, and until
-- now the registry read a market's price only to learn which way it resolved.
-- This table holds what the market said *at the cutoff* -- the benchmark a
-- forecasting reader asks for before any other.
--
-- One row per scenario that was asked about, priced or not. A row is in
-- exactly one of two states, and the CHECKs below admit no third:
--
--   * a probability together with the timestamp it was observed at, or
--   * a reason there is none (no market, no history before the cutoff, ...).
--
-- There is no default probability and no imputed one: an unobtainable price is
-- a counted exclusion, never a 0.5.
--
-- The time lock is a constraint, not a convention. `cutoff_ts` is copied onto
-- the row -- it is the cutoff the observation was *selected against* -- and
-- `observed_at < cutoff_ts` is strict, matching Chronofence, where
-- `published_at >= as_of` is a violation. The read path joins `scenarios` and
-- drops any row whose copied cutoff no longer equals the registry's, so a
-- price selected against a cutoff that later moved cannot be scored.
--
-- Staleness (`cutoff_ts - observed_at`) is derivable and deliberately not
-- stored as a verdict: the bound is configuration
-- (`market_baseline.max_staleness_hours`), and a row that recorded "usable"
-- would go on saying so after the bound changed.
--
-- Forward-only. Never edit a migration that has been applied; add a new one.

BEGIN;

CREATE TABLE IF NOT EXISTS market_prices (
    scenario_id         text        PRIMARY KEY REFERENCES scenarios (scenario_id),
    source              text        NOT NULL,
    -- The registry's own reference, verbatim: `polymarket:{slug}:{market_id}`
    -- or `manifold:{contract_id}`. Enough to ask the source again.
    source_ref          text        NOT NULL,
    cutoff_ts           timestamptz NOT NULL,
    -- The YES probability as the source quoted it. Unclipped: log loss is
    -- clipped once, for every configuration alike, where it is computed.
    probability         double precision
                        CHECK (probability IS NULL OR (probability >= 0 AND probability <= 1)),
    observed_at         timestamptz,
    unobtainable_reason text,
    detail              text        NOT NULL DEFAULT '',
    -- When the source was asked, not when the row was written: a replayed
    -- recording keeps the time of the recording.
    fetched_at          timestamptz NOT NULL,
    CONSTRAINT market_prices_priced_or_explained
        CHECK ((probability IS NULL) = (observed_at IS NULL)
               AND (probability IS NULL) <> (unobtainable_reason IS NULL)),
    CONSTRAINT market_prices_strictly_before_cutoff
        CHECK (observed_at IS NULL OR observed_at < cutoff_ts)
);

COMMENT ON TABLE market_prices IS
    'Market probability strictly before each scenario cutoff. A benchmark: '
    'readable by cascade_eval, never by cascade_sim.';

-- --------------------------------------------------------------------------
-- Grants
--
-- The price is pre-cutoff information, not an outcome -- but whether an agent
-- should ever see it is a separate decision nobody has made. Until it is, the
-- price is a benchmark only, and that is enforced the way invariant 2 is: by
-- the *absence* of a grant (ADR-0005). `cascade_sim` is deliberately not named
-- below, PUBLIC holds nothing (migration 001), so the simulation role gets a
-- permission error. A REVOKE here would be a no-op and is not written.
--
-- The admin role owns the table and is the only writer; it needs no grant.
-- For the same reason the baseline is scored straight from this table and
-- never copied into `forecasts`, which `cascade_sim` can read.
-- --------------------------------------------------------------------------

GRANT SELECT ON market_prices TO cascade_eval;

COMMIT;
