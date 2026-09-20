"""The build's two stop rules, and what it records per unit (M11).

`run_ingest` had no unit test: every store call reaches Postgres. This harness
replaces the store, the unit list, the loader and the embedder, and runs the
real loop -- validate, dedupe, chunk, write, mark -- with no database.

Found at M10 by running it: `target_chunks` was a floor nothing enforced as a
stop, so an unbounded build would have walked ~1,460 CC-NEWS files (~140 GB)
into 27 GB of free disk.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from cascade.config import Settings
from cascade.corpus import pipeline
from cascade.corpus.schema import RawDocument

NOW = datetime(2026, 9, 19, tzinfo=UTC)
WORDS = [
    "harbour",
    "tariff",
    "ledger",
    "quorum",
    "turbine",
    "orchard",
    "ballot",
    "freight",
    "lattice",
    "monsoon",
    "pension",
    "granite",
    "subsidy",
    "ferry",
    "arbitration",
    "saffron",
    "cobalt",
    "viaduct",
    "census",
    "lichen",
]


def body(seed: int) -> str:
    """Distinct prose per document, so near-duplicate collapse leaves them alone."""
    return " ".join(WORDS[(seed * 7 + i * (seed + 3)) % len(WORDS)] + str(seed) for i in range(90))


class FakeEmbedder:
    def load(self) -> None:
        return None

    def count_tokens(self, text: str) -> int:
        return len(text.split())

    def count_tokens_batch(self, texts: Any) -> list[int]:
        return [len(text.split()) for text in texts]

    def encode(self, texts: Any) -> list[list[float]]:
        return [[0.0] * 384 for _ in texts]


class Harness:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, units: list[str], stored: int = 0):
        self.marked: list[dict[str, Any]] = []
        self.written_chunks = 0
        self.loaded: list[str] = []
        monkeypatch.setattr(pipeline, "seen_simhashes", lambda settings: [])
        monkeypatch.setattr(pipeline, "existing_document_ids", lambda settings: set())
        monkeypatch.setattr(pipeline, "completed_units", lambda settings, source: set())
        monkeypatch.setattr(pipeline, "stored_chunk_count", lambda settings: stored)
        monkeypatch.setattr(pipeline, "_scenario_cutoffs", lambda settings: [])
        monkeypatch.setattr(pipeline, "_units_for", lambda settings, source: list(units))
        monkeypatch.setattr(pipeline, "mark_unit", lambda settings, **kw: self.marked.append(kw))
        monkeypatch.setattr(pipeline, "write_batch", self._write)
        monkeypatch.setattr(
            pipeline, "_unit_loader", lambda settings, source, fetchers, wiki: (self._load, 1)
        )

    def _load(self, unit_key: str) -> list[RawDocument]:
        self.loaded.append(unit_key)
        ordinal = len(self.loaded)
        return [
            RawDocument(
                source="govpr",
                source_ref=f"{unit_key}/{n}",
                url=f"https://example.org/{unit_key}/{n}",
                title=f"doc {ordinal}-{n}",
                body=body(ordinal * 10 + n),
                published_at=datetime(2025, 1, ordinal, tzinfo=UTC),
            )
            for n in range(2)
        ]

    def _write(self, settings: Any, batch: Any, chunks: Any, vectors: Any) -> tuple[int, int]:
        self.written_chunks += len(chunks)
        return len(batch), len(chunks)

    def run(self, settings: Settings, **kwargs: Any) -> pipeline.IngestReport:
        return pipeline.run_ingest(
            settings,
            now=NOW,
            sources=("govpr",),
            embedder=FakeEmbedder(),  # type: ignore[arg-type]
            free_disk_gb=kwargs.pop("free_disk_gb", lambda: 500.0),
            **kwargs,
        )


def with_corpus(settings: Settings, **update: Any) -> Settings:
    return settings.model_copy(update={"corpus": settings.corpus.model_copy(update=update)})


def test_an_unstopped_build_processes_every_unit(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(monkeypatch, units=["u1", "u2", "u3"])
    report = harness.run(settings)
    assert report.stopped is None
    assert [m["unit_key"] for m in harness.marked] == ["u1", "u2", "u3"]
    assert report.chunks_written == harness.written_chunks > 0


def test_each_unit_records_its_own_chunks_not_the_running_total(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(monkeypatch, units=["u1", "u2", "u3"])
    report = harness.run(settings)
    per_unit = [m["n_chunks"] for m in harness.marked]
    assert sum(per_unit) == report.chunks_written
    assert per_unit[2] < report.chunks_written  # the old code recorded the total here


def test_the_ceiling_counts_what_was_already_stored_and_stops_cleanly(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe = Harness(monkeypatch, units=["u1"])
    one_unit = probe.run(settings).chunks_written

    harness = Harness(monkeypatch, units=["u1", "u2", "u3", "u4"], stored=1_999_990)
    ceiling = with_corpus(settings, max_chunks=1_999_990 + one_unit)
    report = harness.run(ceiling)

    assert report.stopped == "ceiling"
    assert [m["unit_key"] for m in harness.marked] == ["u1"]
    # Nothing half-done: every marked unit is `done`, and the unit in hand when
    # the stop fired was left unmarked, so the next run takes it whole.
    assert all(m["state"] == "done" for m in harness.marked)


def test_low_disk_stops_before_the_next_unit_is_processed(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(monkeypatch, units=["u1", "u2", "u3"])
    readings = iter([50.0, 7.9])
    report = harness.run(settings, free_disk_gb=lambda: next(readings))
    assert report.stopped == "disk"
    assert "7.9 GB free" in report.stop_detail
    assert [m["unit_key"] for m in harness.marked] == ["u1"]


def test_the_disk_guard_can_be_disabled_for_a_remote_database(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(monkeypatch, units=["u1", "u2"])

    def never_called() -> float:
        raise AssertionError("the disk must not be measured when the guard is off")

    report = harness.run(with_corpus(settings, min_free_disk_gb=None), free_disk_gb=never_called)
    assert report.stopped is None and len(harness.marked) == 2


def test_a_ceiling_below_the_target_is_refused_at_load(settings: Settings) -> None:
    from pydantic import ValidationError

    from cascade.config import CorpusConfig

    with pytest.raises(ValidationError, match="could ever pass"):
        CorpusConfig(**{**settings.corpus.model_dump(), "max_chunks": 1_000})


# ---------------------------------------------------------------------------
# Anchored planning in the loop (ADR-0036)
# ---------------------------------------------------------------------------


def test_ccnews_is_ingested_in_the_anchored_plans_order_not_the_sweeps(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = ["2026/03#339", "2026/04#194", "2024/07#171"]
    harness = Harness(monkeypatch, units=["2026/03#0", "2026/03#1"])
    monkeypatch.setattr(pipeline, "_anchored_units", lambda settings, fetcher, done: list(plan))
    report = pipeline.run_ingest(
        with_corpus(settings, ccnews_planning="anchored"),
        now=NOW,
        sources=("ccnews",),
        embedder=FakeEmbedder(),  # type: ignore[arg-type]
        free_disk_gb=lambda: 500.0,
    )
    assert report.stopped is None
    assert harness.loaded == plan, "files are fetched in plan order, by their listing index"


def test_sweep_planning_is_still_available_and_never_asks_for_listings(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(monkeypatch, units=["2026/03#0", "2026/03#1"])

    def must_not_be_called(*args: object) -> list[str]:
        raise AssertionError("the sweep needs no listings")

    monkeypatch.setattr(pipeline, "_anchored_units", must_not_be_called)
    pipeline.run_ingest(
        with_corpus(settings, ccnews_planning="sweep"),
        now=NOW,
        sources=("ccnews",),
        embedder=FakeEmbedder(),  # type: ignore[arg-type]
        free_disk_gb=lambda: 500.0,
    )
    assert sorted(harness.loaded) == ["2026/03#0", "2026/03#1"]


def test_a_listing_that_cannot_be_fetched_fails_the_source_loudly(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(monkeypatch, units=["2026/03#0"])

    def unreachable(*args: object) -> list[str]:
        raise ConnectionError("data.commoncrawl.org unreachable")

    monkeypatch.setattr(pipeline, "_anchored_units", unreachable)
    report = pipeline.run_ingest(
        with_corpus(settings, ccnews_planning="anchored"),
        now=NOW,
        sources=("ccnews",),
        embedder=FakeEmbedder(),  # type: ignore[arg-type]
        free_disk_gb=lambda: 500.0,
    )
    assert "ConnectionError" in report.sources[0].detail
    assert harness.loaded == [], "nothing is ingested around a month nobody could list"


def test_the_listing_months_are_each_cutoffs_last_three_plus_what_is_held() -> None:
    from datetime import UTC, datetime

    months = pipeline._listing_months(
        [datetime(2026, 1, 15, tzinfo=UTC), datetime(2016, 9, 2, tzinfo=UTC)],
        ["2025/08#0"],
    )
    # 2016/07 predates the collection (it starts 2016/08) and is not requested.
    assert months == ["2016/08", "2016/09", "2025/08", "2025/11", "2025/12", "2026/01"]
