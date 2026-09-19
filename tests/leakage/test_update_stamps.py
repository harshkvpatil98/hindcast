"""No stored text carries an update stamp later than its document's date.

The poison-pill and date-monotonicity probes prove the time lock respects the
dates. This probe asks whether the dates are true of the text: a page whose own
"Updated:" stamp postdates the date it is filed under is later text wearing an
earlier date (ADR-0041, ADR-0044). Run over every chunk, as `cascade_eval`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cascade.config import Settings
from cascade.corpus.stamps import stamps_after

pytestmark = pytest.mark.leakage


@pytest.fixture
def live_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    env_file = Path(__file__).resolve().parents[2] / ".env"
    if not env_file.is_file():
        pytest.skip("no .env; run `make env` first")
    monkeypatch.setenv("CASCADE_ENV_FILE", str(env_file))
    return Settings()


def test_no_chunk_carries_an_update_stamp_after_its_date(live_settings: Settings) -> None:
    import psycopg

    try:
        conn = psycopg.connect(live_settings.database_url("admin"), connect_timeout=5)
    except Exception as exc:  # noqa: BLE001 -- a skip needs its reason
        pytest.skip(f"postgres not reachable: {type(exc).__name__}")
    with conn, conn.cursor() as cur:
        # The database narrows to chunks that mention a stamp at all; the
        # detector, which is unit-tested, decides.
        cur.execute(
            "SELECT chunk_id, published_at, body FROM chunks "
            "WHERE body ~* '(updated|modified)[^0-9a-z]{0,12}(on )?[a-z0-9]'"
        )
        rows = cur.fetchall()
    if not rows:
        pytest.skip("no chunk mentions an update stamp; is the corpus built?")
    offenders = [
        (chunk_id, published_at, stamps)
        for chunk_id, published_at, body in rows
        if (stamps := stamps_after(body, published_at))
    ]
    assert (
        offenders == []
    ), f"{len(offenders)} chunk(s) carry update stamps after their date; first: {offenders[:3]}"
