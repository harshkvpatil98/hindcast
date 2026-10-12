"""Re-dating stored CC-NEWS documents against a live database (ADR-0044).

What cannot be seen offline: that moving ``published_at`` -- the partition key
of both corpus tables -- carries a document and its chunks across partitions
together, keeps the stated date, is idempotent, and never moves a date earlier.

Rows are written under a test prefix and removed afterwards.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from cascade.config import Settings
from cascade.corpus.redate import _apply_file

pytestmark = pytest.mark.integration

PREFIX = "ccnews:https://itest-redate.invalid/"


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
            cur.execute(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = 'documents' AND column_name = 'crawled_at'"
            )
            if cur.fetchone() is None:
                pytest.skip("documents.crawled_at is absent; run `cascade db migrate`")
    except pytest.skip.Exception:
        raise
    except Exception as exc:  # noqa: BLE001 -- a skip needs its reason
        pytest.skip(f"postgres not reachable: {type(exc).__name__}")
    return settings


def _run(settings: Settings, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    import psycopg

    with (
        psycopg.connect(settings.database_url("admin"), connect_timeout=10) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(sql, params)
        rows = list(cur.fetchall()) if cur.description else []
        conn.commit()
    return rows


@pytest.fixture
def seeded(live_settings: Settings) -> Iterator[Settings]:
    def cleanup() -> None:
        _run(live_settings, "DELETE FROM chunks WHERE document_id LIKE %s", (PREFIX + "%",))
        _run(live_settings, "DELETE FROM documents WHERE document_id LIKE %s", (PREFIX + "%",))

    cleanup()
    rows = (
        # A 2025 article re-crawled in 2026: must move, across partitions.
        ("old", datetime(2025, 8, 23, 18, 31, tzinfo=UTC)),
        # Fetched an hour after it was published.
        ("fresh", datetime(2026, 3, 16, 10, 0, tzinfo=UTC)),
        # States a later instant than its fetch: must not move earlier.
        ("ahead", datetime(2026, 3, 16, 13, 0, tzinfo=UTC)),
    )
    for name, published in rows:
        _run(
            live_settings,
            "INSERT INTO documents (document_id, source, source_ref, published_at, simhash, "
            "n_chunks) VALUES (%s, 'ccnews', %s, %s, 0, 2)",
            (PREFIX + name, name, published),
        )
        for ordinal in (0, 1):
            _run(
                live_settings,
                "INSERT INTO chunks (chunk_id, document_id, ordinal, body, token_count, "
                "published_at) VALUES (%s, %s, %s, 'text', 1, %s)",
                (f"{PREFIX}{name}:{ordinal}", PREFIX + name, ordinal, published),
            )
    yield live_settings
    cleanup()


FETCHED = datetime(2026, 3, 16, 11, 31, 17, tzinfo=UTC)


def test_documents_and_their_chunks_move_to_the_fetch_together(seeded: Settings) -> None:
    times = {PREFIX + name: FETCHED for name in ("old", "fresh", "ahead")}
    matched, gaps = _apply_file(seeded, times)
    assert matched == 3 and len(gaps) == 2

    documents = dict(
        (row[0].removeprefix(PREFIX), row[1:])
        for row in _run(
            seeded,
            "SELECT document_id, published_at, stated_published_at, crawled_at "
            "FROM documents WHERE document_id LIKE %s",
            (PREFIX + "%",),
        )
    )
    assert documents["old"] == (FETCHED, datetime(2025, 8, 23, 18, 31, tzinfo=UTC), FETCHED)
    assert documents["fresh"][0] == FETCHED
    assert documents["ahead"][0] == datetime(2026, 3, 16, 13, 0, tzinfo=UTC)  # never earlier
    chunk_dates = _run(
        seeded,
        "SELECT d.document_id, count(*) FROM chunks c JOIN documents d "
        "ON d.document_id = c.document_id AND d.published_at = c.published_at "
        "WHERE d.document_id LIKE %s GROUP BY 1",
        (PREFIX + "%",),
    )
    assert sorted(count for _, count in chunk_dates) == [
        2,
        2,
        2,
    ], "every chunk moved with its document"
    partition = _run(
        seeded,
        "SELECT tableoid::regclass::text FROM chunks WHERE chunk_id = %s",
        (PREFIX + "old:0",),
    )
    assert partition == [("chunks_2026q1",)]


def test_a_second_pass_changes_nothing_and_a_later_fetch_only_moves_forward(
    seeded: Settings,
) -> None:
    times = {PREFIX + "old": FETCHED}
    _apply_file(seeded, times)
    matched, gaps = _apply_file(seeded, times)
    assert (matched, gaps) == (1, [])
    earlier = {PREFIX + "old": datetime(2025, 12, 1, tzinfo=UTC)}
    _apply_file(seeded, earlier)
    [(published, stated)] = _run(
        seeded,
        "SELECT published_at, stated_published_at FROM documents WHERE document_id = %s",
        (PREFIX + "old",),
    )
    assert published == FETCHED
    assert stated == datetime(2025, 8, 23, 18, 31, tzinfo=UTC)
