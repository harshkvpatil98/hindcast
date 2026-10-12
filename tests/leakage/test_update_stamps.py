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
from cascade.corpus.stamps import STAMP_TOLERANCE, stamps_after

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
            "SELECT c.chunk_id, c.published_at, d.crawled_at, c.body FROM chunks c "
            "JOIN documents d ON d.document_id = c.document_id "
            "AND d.published_at = c.published_at "
            "WHERE c.body ~* '(updated|modified)[^0-9a-z]{0,12}(on )?[a-z0-9]'"
        )
        rows = cur.fetchall()
    if not rows:
        pytest.skip("no chunk mentions an update stamp; is the corpus built?")
    # A stamp later than the moment the page was *fetched* cannot be true of
    # the fetched text: measured, the four such stamps in the rebuilt corpus
    # are publisher typos ("Published Dec 31, 2025" on a page fetched
    # 2025-01-01). Where the fetch time is known it bounds every real stamp.
    offenders = [
        (chunk_id, published_at, real)
        for chunk_id, published_at, crawled_at, body in rows
        if (
            real := [
                stamp
                for stamp in stamps_after(body, published_at)
                if crawled_at is None or stamp <= crawled_at + STAMP_TOLERANCE
            ]
        )
    ]
    assert (
        offenders == []
    ), f"{len(offenders)} chunk(s) carry update stamps after their date; first: {offenders[:3]}"
