"""Dossier persistence and its grants (migration 019, ADR-0037).

Runs against the live database. What is exercised is what the unit tests
cannot reach: the upsert, the hash round-trip through ``jsonb``, the refusal
statistics, and the grant -- ``cascade_sim`` reads a dossier because it builds
the agents' prompts, and must not be able to write one, because a dossier is
evidence and the simulation does not author its own evidence.

Rows written here are removed afterwards.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from cascade.config import Settings
from cascade.decompose.dossier import (
    Claim,
    Dossier,
    DroppedClaim,
    WriteOutcome,
    dossier_hash,
)
from cascade.decompose.dossier_store import (
    dossier_stats,
    load_dossiers,
    situations_for,
    verify_dossier_hashes,
    write_dossier,
    written_scenarios,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def live_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    env_file = Path(__file__).resolve().parents[2] / ".env"
    if not env_file.is_file():
        pytest.skip("no .env; run `make env` first")
    monkeypatch.setenv("CASCADE_ENV_FILE", str(env_file))
    settings = Settings()
    try:
        import psycopg

        with (
            psycopg.connect(settings.database_url("admin"), connect_timeout=5) as conn,
            conn.cursor() as cur,
        ):
            cur.execute("SELECT to_regclass('public.scenario_dossiers')")
            row = cur.fetchone()
            if row is None or row[0] is None:
                pytest.skip("scenario_dossiers is absent; run `cascade db migrate`")
    except pytest.skip.Exception:
        raise
    except Exception as exc:  # noqa: BLE001 -- a skip needs its reason
        pytest.skip(f"postgres not reachable: {type(exc).__name__}")
    return settings


@pytest.fixture
def borrowed(live_settings: Settings) -> Any:
    """A real scenario id and cutoff, with any dossier against it cleaned up."""
    import psycopg

    def connect() -> Any:
        return psycopg.connect(live_settings.database_url("admin"), connect_timeout=10)

    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT scenario_id, cutoff_ts FROM scenarios ORDER BY scenario_id LIMIT 1")
        row = cur.fetchone()
    if row is None:
        pytest.skip("scenario registry is empty; run `cascade ledger build`")

    def cleanup() -> None:
        with connect() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM scenario_dossiers WHERE scenario_id = %s", (row[0],))
            conn.commit()

    cleanup()
    yield str(row[0]), row[1]
    cleanup()


def outcome_for(scenario_id: str, cutoff: datetime) -> WriteOutcome:
    published = (cutoff - timedelta(days=3)).astimezone(UTC)
    dossier = Dossier(
        scenario_id=scenario_id,
        as_of=cutoff,
        timeline=(
            Claim(text="The regulator opened a review.", cites=(1,), on=f"{published:%Y-%m-%d}"),
        ),
        positions=(),
        status=(Claim(text="A hearing is scheduled for next month.", cites=(1,)),),
        unknowns=(),
        evidence=((1, "chunk-1", published.isoformat().replace("+00:00", "Z")),),
    )
    return WriteOutcome(
        scenario_id=scenario_id,
        dossier=dossier,
        dossier_sha256=dossier_hash(dossier),
        dropped=(
            DroppedClaim(section="status", text="The deal was blocked.", reason="unsupported:x"),
        ),
        n_excerpts=1,
        llm_calls=1,
    )


def test_a_dossier_round_trips_and_its_hash_survives_jsonb(
    live_settings: Settings, borrowed: tuple[str, datetime]
) -> None:
    scenario_id, cutoff = borrowed
    original = outcome_for(scenario_id, cutoff)
    write_dossier(live_settings, original)
    write_dossier(live_settings, original)  # idempotent on scenario_id

    assert scenario_id in written_scenarios(live_settings)
    loaded, recorded = load_dossiers(live_settings, role="sim")[scenario_id]
    assert recorded == original.dossier_sha256
    assert dossier_hash(loaded) == recorded
    assert verify_dossier_hashes(live_settings) == ()

    stats = dossier_stats(live_settings)
    assert stats.written >= 1 and stats.dropped >= 1
    assert "unsupported" in {reason for reason, _ in stats.dropped_by_reason}


def test_an_edited_row_is_detected(live_settings: Settings, borrowed: tuple[str, datetime]) -> None:
    import psycopg

    scenario_id, cutoff = borrowed
    write_dossier(live_settings, outcome_for(scenario_id, cutoff))
    with (
        psycopg.connect(live_settings.database_url("admin"), connect_timeout=10) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(
            "UPDATE scenario_dossiers SET dossier = jsonb_set(dossier, '{status}', '[]'::jsonb) "
            "WHERE scenario_id = %s",
            (scenario_id,),
        )
        conn.commit()
    assert [item.scenario_id for item in verify_dossier_hashes(live_settings)] == [scenario_id]


def test_the_simulation_reads_dossiers_and_cannot_write_them(
    live_settings: Settings, borrowed: tuple[str, datetime]
) -> None:
    import psycopg

    scenario_id, cutoff = borrowed
    write_dossier(live_settings, outcome_for(scenario_id, cutoff))
    with (
        psycopg.connect(live_settings.database_url("sim"), connect_timeout=10) as conn,
        conn.cursor() as cur,
    ):
        cur.execute("SELECT count(*) FROM scenario_dossiers WHERE scenario_id = %s", (scenario_id,))
        assert cur.fetchone() == (1,)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            cur.execute("DELETE FROM scenario_dossiers WHERE scenario_id = %s", (scenario_id,))


def test_enabled_situations_come_from_the_table_as_sim(
    live_settings: Settings, borrowed: tuple[str, datetime]
) -> None:
    scenario_id, cutoff = borrowed
    write_dossier(live_settings, outcome_for(scenario_id, cutoff))
    enabled = live_settings.model_copy(
        update={
            "dossier": live_settings.dossier.model_copy(update={"enabled": True}),
            "llm": live_settings.llm.model_copy(update={"prompt_rev": "r3"}),
        }
    )
    text, recorded = situations_for(enabled, (scenario_id,), role="sim")[scenario_id]
    assert "A hearing is scheduled" in text
    assert recorded == outcome_for(scenario_id, cutoff).dossier_sha256
