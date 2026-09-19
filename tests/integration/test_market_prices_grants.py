"""The market price is a benchmark, enforced by Postgres (migration 017).

Whether an agent should ever see the market's probability is a decision nobody
has made. Until it is, ``cascade_sim`` must be *unable* to read it -- the same
mechanism as invariant 2, and asserted the same way: not that the code avoids
the table, but that the database refuses the read.

The static half -- that the migration grants ``cascade_sim`` nothing -- is in
``tests/unit/test_eval_market.py`` and runs without a database.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from cascade.config import Settings

pytestmark = pytest.mark.integration


@pytest.fixture
def live_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Real settings against the running stack, with the project's own .env."""
    env_file = Path(__file__).resolve().parents[2] / ".env"
    if not env_file.is_file():
        pytest.skip("no .env; run `make env` first")
    monkeypatch.setenv("CASCADE_ENV_FILE", str(env_file))
    settings = Settings()
    try:
        import psycopg

        with psycopg.connect(settings.database_url("admin"), connect_timeout=5) as conn:
            row = conn.execute("SELECT to_regclass('public.market_prices')").fetchone()
    except Exception as exc:  # noqa: BLE001 -- a skip needs the reason, not a traceback
        pytest.skip(f"postgres not reachable: {type(exc).__name__}")
    if row is None or row[0] is None:
        pytest.skip("migration 017 not applied; run `make migrate`")
    return settings


def connect(settings: Settings, role: str) -> Any:
    import psycopg

    return psycopg.connect(settings.database_url(role), connect_timeout=10)  # type: ignore[arg-type]


def a_scenario(settings: Settings) -> tuple[str, datetime]:
    with connect(settings, "admin") as conn:
        row = conn.execute(
            "SELECT scenario_id, cutoff_ts FROM scenarios ORDER BY scenario_id LIMIT 1"
        ).fetchone()
    if row is None:
        pytest.skip("no registry loaded; run `cascade ledger build --write`")
    return str(row[0]), row[1]


def test_sim_role_cannot_read_market_prices(live_settings: Settings) -> None:
    from psycopg import errors

    with connect(live_settings, "sim") as conn, pytest.raises(errors.InsufficientPrivilege):
        conn.execute("SELECT count(*) FROM market_prices")


def test_eval_role_reads_and_cannot_write(live_settings: Settings) -> None:
    """The phase that is scored against the benchmark cannot adjust it."""
    from psycopg import errors

    scenario_id, cutoff = a_scenario(live_settings)
    with connect(live_settings, "eval") as conn:
        conn.execute("SELECT count(*) FROM market_prices").fetchone()
        with pytest.raises(errors.InsufficientPrivilege):
            conn.execute(
                "INSERT INTO market_prices (scenario_id, source, source_ref, cutoff_ts, "
                "unobtainable_reason, fetched_at) VALUES (%s, 'x', 'x', %s, 'not_a_market', now())",
                (scenario_id, cutoff),
            )


def test_the_table_refuses_an_observation_at_the_cutoff(live_settings: Settings) -> None:
    """The time lock is a constraint: `observed_at < cutoff_ts`, strictly."""
    from psycopg import errors

    scenario_id, cutoff = a_scenario(live_settings)
    with connect(live_settings, "admin") as conn:
        with pytest.raises(errors.CheckViolation):
            conn.execute(
                "INSERT INTO market_prices (scenario_id, source, source_ref, cutoff_ts, "
                "probability, observed_at, fetched_at) VALUES (%s, 'x', 'x', %s, 0.5, %s, now()) "
                "ON CONFLICT (scenario_id) DO UPDATE SET observed_at = EXCLUDED.observed_at, "
                "probability = EXCLUDED.probability, unobtainable_reason = NULL",
                (scenario_id, cutoff, cutoff),
            )
        conn.rollback()


def test_the_table_refuses_a_row_that_is_neither_priced_nor_explained(
    live_settings: Settings,
) -> None:
    from psycopg import errors

    scenario_id, cutoff = a_scenario(live_settings)
    with connect(live_settings, "admin") as conn:
        with pytest.raises(errors.CheckViolation):
            conn.execute(
                "INSERT INTO market_prices (scenario_id, source, source_ref, cutoff_ts, fetched_at) "
                "VALUES (%s, 'x', 'x', %s, now()) "
                "ON CONFLICT (scenario_id) DO UPDATE SET probability = NULL, observed_at = NULL, "
                "unobtainable_reason = NULL",
                (scenario_id, cutoff),
            )
        conn.rollback()


def test_a_price_round_trips_and_is_never_visible_through_forecasts(
    live_settings: Settings,
) -> None:
    """Written as admin, read as eval, rolled back -- and no row appears in
    `forecasts`, which `cascade_sim` can read."""
    from cascade.eval.market import MARKET_CONFIG_ID

    scenario_id, cutoff = a_scenario(live_settings)
    observed = cutoff - timedelta(seconds=30)
    with connect(live_settings, "admin") as conn:
        conn.execute(
            "INSERT INTO market_prices (scenario_id, source, source_ref, cutoff_ts, probability, "
            "observed_at, fetched_at) VALUES (%s, 'polymarket', 'polymarket:x:1', %s, 0.25, %s, %s) "
            "ON CONFLICT (scenario_id) DO NOTHING",
            (scenario_id, cutoff, observed, datetime.now(UTC)),
        )
        row = conn.execute(
            "SELECT observed_at < cutoff_ts FROM market_prices WHERE scenario_id = %s",
            (scenario_id,),
        ).fetchone()
        copies = conn.execute(
            "SELECT count(*) FROM forecasts WHERE config_id = %s", (MARKET_CONFIG_ID,)
        ).fetchone()
        conn.rollback()
    assert row is not None and row[0] is True
    assert copies is not None and int(copies[0]) == 0
