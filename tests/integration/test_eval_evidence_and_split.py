"""The two M14 reads that need the live database. NOT run offline.

Written without being executed -- Postgres was off-limits to the session that
wrote them -- so treat a first failure here as a defect in the test as readily
as in the code. They check the two facts a unit test cannot: that the evidence
count the tiers are built from is the half-open window it claims to be, and
that the split pinned in `configs/base.yaml` is the split of the registry the
database actually holds.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from cascade.config import Settings
from cascade.eval.evidence import EVIDENCE_WINDOW_DAYS, tier_of
from cascade.eval.split import assert_declared, declare_split
from cascade.eval.store import assert_frozen_split, evidence_counts

pytestmark = pytest.mark.integration


def _connect(settings: Settings, role: str) -> Any:
    import psycopg

    return psycopg.connect(settings.database_url(role), connect_timeout=10)


def test_every_sealed_scenario_gets_a_measured_count(live_settings: Settings) -> None:
    counts = evidence_counts(live_settings, window_days=EVIDENCE_WINDOW_DAYS)
    with _connect(live_settings, "eval") as conn, conn.cursor() as cur:
        cur.execute("SELECT scenario_id FROM scenarios ORDER BY scenario_id")
        registry = [str(row[0]) for row in cur.fetchall()]
    if not registry:
        pytest.skip("scenario registry is empty; run `cascade ledger build`")
    assert sorted(counts) == registry
    assert all(count >= 0 for count in counts.values())
    assert {tier_of(count).name for count in counts.values()} <= {
        "none",
        "thin",
        "moderate",
        "rich",
    }


def test_the_window_is_half_open_and_ends_strictly_before_the_cutoff(
    live_settings: Settings,
) -> None:
    """``cutoff - 30d <= published_at < cutoff``, checked one scenario at a
    time against a direct count with the bounds written out."""
    counts = evidence_counts(live_settings, window_days=EVIDENCE_WINDOW_DAYS)
    with _connect(live_settings, "eval") as conn, conn.cursor() as cur:
        cur.execute("SELECT scenario_id, cutoff_ts FROM scenarios ORDER BY scenario_id LIMIT 5")
        for scenario_id, cutoff in cur.fetchall():
            cur.execute(
                "SELECT count(*) FROM chunks WHERE published_at >= %s AND published_at < %s",
                (cutoff - timedelta(days=EVIDENCE_WINDOW_DAYS), cutoff),
            )
            assert counts[str(scenario_id)] == int(cur.fetchone()[0]), scenario_id


def test_the_pinned_split_is_the_split_of_the_stored_registry(live_settings: Settings) -> None:
    """The pin was computed from a label-free export of the registry. This is
    the check that the export and the database agree."""
    from cascade.ledger.store import load_scenarios

    frozen = assert_frozen_split(live_settings)
    registry = load_scenarios(live_settings, role="eval")
    declared = declare_split(
        [(item.scenario_id, item.domain) for item in registry],
        salt=frozen.study_salt,
        dev_size=live_settings.eval.dev_scenarios,
    )
    assert_declared(declared, pinned_sha256=live_settings.eval.split_sha256)
    assert declared.n == frozen.n_scenarios
