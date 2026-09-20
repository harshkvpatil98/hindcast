"""Chunking ahead of the embedder changes when work happens, never what is stored.

`corpus.chunk_workers` chunks a write batch on a thread pool, one batch ahead
of the embedding call. Every test here is a way that could silently stop being
a pure optimisation: results read in completion order, a lookahead that is not
bounded, a unit marked done before its last batch is written, a worker's
exception that disappears, a pool that outlives the run that started it.

The harness is the database-free one from `test_corpus_pipeline_stops`, with
the things it only counted now recorded in full, on one event log, because the
claims are about *order* -- of chunks within a write, of writes within a unit,
of a unit's mark after its last write.
"""

from __future__ import annotations

import threading
import time
import zlib
from datetime import UTC, datetime
from typing import Any

import pytest
from hypothesis import HealthCheck, given
from hypothesis import settings as hypothesis_settings
from hypothesis import strategies as st

from cascade.config import Settings
from cascade.corpus import pipeline
from cascade.corpus.chunker import chunk_text
from cascade.corpus.schema import Chunk, DatedDocument, RawDocument
from tests.unit.test_corpus_pipeline_stops import WORDS, Harness, with_corpus

NOW = datetime(2026, 9, 19, tzinfo=UTC)
BATCH = 5
WORKER_COUNTS = (1, 2, 8)
# Generous, and never the thing a passing test waits for: every wait below is
# ended by an event, and this only bounds how long a *failing* one hangs.
PATIENCE_S = 20.0
# Captured once. Each harness wraps `pipeline._chunks_for`, and a test builds
# several: wrapping whatever is there would nest them, and an earlier harness
# would go on logging a later one's documents.
REAL_CHUNKS_FOR = pipeline._chunks_for


def prose(seed: int) -> str:
    """Distinct, multi-sentence, and of very different lengths per document.

    Length is what makes documents finish out of order on a pool, and the
    per-document vocabulary is what keeps near-duplicate collapse off them.
    """
    sentences = []
    for s in range(2 + (seed * 11) % 23):
        words = [WORDS[(seed * 7 + s * 3 + i * (seed + 5)) % len(WORDS)] for i in range(6 + s % 9)]
        sentences.append(" ".join(f"{word}{seed}" for word in words).capitalize() + ".")
    return " ".join(sentences) + " " + " ".join(f"closing{seed}x{i}" for i in range(40))


def small_chunks(settings: Settings, *, workers: int, **update: Any) -> Settings:
    """Chunks of ~30 words and batches of five, so a few dozen documents span
    many chunks, many batches and a ragged last batch."""
    return with_corpus(
        settings,
        chunk_workers=workers,
        write_batch_size=BATCH,
        chunk_max_tokens=30,
        chunk_overlap_tokens=6,
        **update,
    )


class WordCounter:
    """The stops harness's tokenizer, with latency that depends on the text.

    A pool only reorders work whose durations differ. The delay is a CRC of
    the text rather than `hash()`, which is salted per process: the same
    documents are slow on every run.
    """

    def __init__(self, *, max_delay_ms: int = 6) -> None:
        self.max_delay_ms = max_delay_ms

    def load(self) -> None:
        return None

    def count_tokens(self, text: str) -> int:
        return len(text.split())

    def count_tokens_batch(self, texts: Any) -> list[int]:
        if self.max_delay_ms:
            key = zlib.crc32(" ".join(texts).encode())
            time.sleep((key % (self.max_delay_ms + 1)) / 1000)
        return [len(text.split()) for text in texts]

    def encode(self, texts: Any) -> list[list[float]]:
        # A function of the text, so a vector filed against the wrong chunk is
        # a visible difference rather than one zero vector among many.
        return [[zlib.crc32(text.encode()) / 2**32] * 3 for text in texts]


def unit_of(document: DatedDocument) -> str:
    return document.source_ref.rsplit("/", 1)[0]


def batch_of(document: DatedDocument) -> tuple[str, int]:
    """Which write batch a document belongs to: documents reach the chunker in
    load order, so its position within its unit decides it."""
    unit, _, position = document.source_ref.rpartition("/")
    return unit, int(position) // BATCH


class Recording(Harness):
    """The stops harness, recording what it used to count.

    One event log, appended to from every thread: `chunk_start` / `chunk_end`
    per document, `write` per batch with everything that would be stored, and
    `mark` per unit.
    """

    def __init__(
        self, monkeypatch: pytest.MonkeyPatch, *, units: list[str], documents_per_unit: int = 23
    ) -> None:
        super().__init__(monkeypatch, units=units)
        self.documents_per_unit = documents_per_unit
        self.events: list[tuple[Any, ...]] = []
        self.on_chunk_start: Any = None
        self.on_chunk_end: Any = None

        def chunks_for(document: DatedDocument, settings: Any, embedder: Any) -> list[Chunk]:
            self.events.append(("chunk_start", document.document_id, batch_of(document)))
            if self.on_chunk_start is not None:
                self.on_chunk_start(document)
            try:
                return REAL_CHUNKS_FOR(document, settings, embedder)
            finally:
                self.events.append(("chunk_end", document.document_id, batch_of(document)))
                if self.on_chunk_end is not None:
                    self.on_chunk_end(document)

        monkeypatch.setattr(pipeline, "_chunks_for", chunks_for)
        monkeypatch.setattr(
            pipeline, "mark_unit", lambda settings, **kw: self.events.append(("mark", kw))
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
                body=prose(ordinal * 100 + n),
                published_at=datetime(2025, 1, ordinal, tzinfo=UTC),
            )
            for n in range(self.documents_per_unit)
        ]

    def _write(self, settings: Any, batch: Any, chunks: Any, vectors: Any) -> tuple[int, int]:
        self.events.append(
            (
                "write",
                batch_of(batch[0]),
                tuple(document.document_id for document in batch),
                tuple(chunk.model_dump_json() for chunk in chunks),
                tuple(tuple(vector) for vector in vectors),
            )
        )
        return len(batch), len(chunks)

    def ingest(self, settings: Settings, embedder: Any) -> pipeline.IngestReport:
        return pipeline.run_ingest(
            settings,
            now=NOW,
            sources=("govpr",),
            embedder=embedder,
            free_disk_gb=lambda: 500.0,
        )

    def stored(self) -> list[tuple[Any, ...]]:
        """Everything that reaches the database, in the order it would."""
        return [event for event in self.events if event[0] in ("write", "mark")]

    def order(self, kind: str) -> list[str]:
        return [event[1] for event in self.events if event[0] == kind]


def no_chunk_threads() -> bool:
    return not [t for t in threading.enumerate() if t.name.startswith("chunk")]


# --------------------------------------------------------------------------
# Equivalence
# --------------------------------------------------------------------------


def run_with(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, workers: int
) -> tuple[Recording, pipeline.IngestReport]:
    harness = Recording(monkeypatch, units=["u1", "u2", "u3"])
    report = harness.ingest(small_chunks(settings, workers=workers), WordCounter())
    return harness, report


def test_any_worker_count_stores_exactly_what_the_serial_loop_stores(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    serial, serial_report = run_with(settings, monkeypatch, 1)
    writes = [event for event in serial.stored() if event[0] == "write"]
    # The fixture has to be worth the comparison: many batches, a ragged last
    # one, several chunks per document, and every document kept.
    assert len(writes) == 15 and {len(event[2]) for event in writes} == {5, 3}
    assert serial_report.documents_written == 69 and serial_report.collapsed == 0
    assert serial_report.chunks_written > 3 * serial_report.documents_written

    for workers in WORKER_COUNTS[1:]:
        parallel, report = run_with(settings, monkeypatch, workers)
        assert parallel.stored() == serial.stored(), f"chunk_workers={workers} stored differently"
        assert report == serial_report
        # The comparison only has teeth if the pool really did finish
        # documents out of order. If it never did, reading futures as they
        # complete would pass too, and this test would be decoration.
        assert parallel.order("chunk_end") != parallel.order("chunk_start")
        assert no_chunk_threads()

    assert serial.order("chunk_end") == serial.order("chunk_start")


def test_the_serial_loop_stores_what_the_chunker_says_document_by_document(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An oracle that does not go through the pipeline at all, so the
    equivalence above cannot be two paths agreeing on the same mistake."""
    for workers in WORKER_COUNTS:
        harness = Recording(monkeypatch, units=["u1"])
        configured = small_chunks(settings, workers=workers)
        harness.ingest(configured, WordCounter(max_delay_ms=2))
        bodies = {
            f"u1/{n}": prose(100 + n) for n in range(harness.documents_per_unit)
        }  # source_ref -> body, as `_load` built them
        position = 0
        for event in harness.stored():
            if event[0] != "write":
                continue
            stored = [Chunk.model_validate_json(raw) for raw in event[3]]
            expected: list[tuple[str, int, str, int]] = []
            for document_id in event[2]:
                plans = chunk_text(
                    bodies[f"u1/{position}"].strip(),
                    count_tokens=lambda text: len(text.split()),
                    max_tokens=30,
                    overlap_tokens=6,
                )
                expected += [
                    (f"{document_id}#{ordinal}", ordinal, plan.body, plan.token_count)
                    for ordinal, plan in enumerate(plans)
                ]
                position += 1
            assert [(c.chunk_id, c.ordinal, c.body, c.token_count) for c in stored] == expected
        assert position == harness.documents_per_unit


def test_a_unit_is_marked_only_after_its_last_batch_is_written(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invariant 8. A unit marked done with a batch still unwritten is skipped
    by the next run, and those documents are never ingested by anything."""
    units = ["u1", "u2", "u3"]
    for workers in WORKER_COUNTS:
        harness, _ = run_with(settings, monkeypatch, workers)
        stored = harness.stored()
        marks = [i for i, event in enumerate(stored) if event[0] == "mark"]
        assert [stored[i][1]["unit_key"] for i in marks] == units
        for nth, i in enumerate(marks):
            unit = stored[i][1]["unit_key"]
            own_writes = [
                j for j, event in enumerate(stored) if event[0] == "write" and event[1][0] == unit
            ]
            assert len(own_writes) == 5 and max(own_writes) < i
            assert [stored[j][1][1] for j in own_writes] == [0, 1, 2, 3, 4]
            assert stored[i][1]["state"] == "done"
            assert stored[i][1]["n_chunks"] == sum(len(stored[j][3]) for j in own_writes)
            # ...and the lookahead did not reach into the next unit: nothing
            # of a later unit was chunked before this one was marked.
            before_mark = harness.events[: harness.events.index(stored[i])]
            started = {event[2][0] for event in before_mark if event[0] == "chunk_start"}
            assert started == set(units[: nth + 1])


# --------------------------------------------------------------------------
# Overlap, and its bound
# --------------------------------------------------------------------------


class GatedEmbedder(WordCounter):
    """An embedder whose first `encode` does not return until it is released."""

    def __init__(self, release: threading.Event, *, wait_s: float) -> None:
        super().__init__(max_delay_ms=0)
        self.release = release
        self.wait_s = wait_s
        self.released_in_time: list[bool] = []

    def encode(self, texts: Any) -> list[list[float]]:
        if not self.released_in_time:
            self.released_in_time.append(self.release.wait(self.wait_s))
        return super().encode(texts)


def signal_when_chunking(batch: tuple[str, int], signal: threading.Event) -> Any:
    """A chunk-start hook, built by a factory so it binds its own event rather
    than whichever one the enclosing loop ended on."""

    def hook(document: DatedDocument) -> None:
        if batch_of(document) == batch:
            signal.set()

    return hook


def test_the_next_batch_is_chunked_while_this_one_is_being_embedded(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    for workers in WORKER_COUNTS[1:]:
        harness = Recording(monkeypatch, units=["u1"])
        next_batch_started = threading.Event()
        harness.on_chunk_start = signal_when_chunking(("u1", 1), next_batch_started)
        embedder = GatedEmbedder(next_batch_started, wait_s=PATIENCE_S)
        harness.ingest(small_chunks(settings, workers=workers), embedder)
        # `encode` of batch 0 returned *because* batch 1 had begun chunking.
        assert embedder.released_in_time == [True], f"no overlap at chunk_workers={workers}"


def test_one_worker_is_the_serial_loop_and_overlaps_nothing(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Recording(monkeypatch, units=["u1"])
    next_batch_started = threading.Event()
    threads_seen: set[str] = set()

    def note(document: DatedDocument) -> None:
        threads_seen.add(threading.current_thread().name)
        if batch_of(document) == ("u1", 1):
            next_batch_started.set()

    harness.on_chunk_start = note
    # Not a race: with no second thread, nothing can set the event while
    # `encode` holds the only one, however long it waits.
    embedder = GatedEmbedder(next_batch_started, wait_s=0.05)
    harness.ingest(small_chunks(settings, workers=1), embedder)
    assert embedder.released_in_time == [False]
    assert threads_seen == {threading.main_thread().name}


class PatientEmbedder(WordCounter):
    """Holds batch 0 in `encode` until the batch behind it is fully chunked,
    then a moment longer -- the window in which an unbounded lookahead runs on
    into batch 2 and a bounded one has nothing left to start."""

    def __init__(self, ahead_is_done: threading.Event, *, wait_s: float) -> None:
        super().__init__(max_delay_ms=0)
        self.ahead_is_done = ahead_is_done
        self.wait_s = wait_s
        self.waited: list[bool] = []

    def encode(self, texts: Any) -> list[list[float]]:
        if not self.waited:
            self.waited.append(self.ahead_is_done.wait(self.wait_s))
            time.sleep(0.05)
        return super().encode(texts)


def unwritten_batches_high_water(events: list[tuple[Any, ...]]) -> int:
    """The most batches that ever had chunks in existence and not yet written."""
    alive: set[tuple[str, int]] = set()
    high = 0
    for event in events:
        if event[0] == "chunk_start":
            alive.add(event[2])
            high = max(high, len(alive))
        elif event[0] == "write":
            alive.discard(event[1])
    return high


def test_the_lookahead_is_one_batch_whatever_the_worker_count(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    for workers, expected in ((1, 1), (2, 2), (8, 2)):
        harness = Recording(monkeypatch, units=["u1"], documents_per_unit=30)
        ahead_is_done = threading.Event()
        finished_ahead: list[str] = []

        def note_end(
            document: DatedDocument,
            finished: list[str] = finished_ahead,
            done: threading.Event = ahead_is_done,
        ) -> None:
            if batch_of(document) == ("u1", 1):
                finished.append(document.document_id)
                if len(finished) == BATCH:
                    done.set()

        harness.on_chunk_end = note_end
        # The serial loop never chunks ahead, so there is nothing to wait for.
        embedder = PatientEmbedder(ahead_is_done, wait_s=PATIENCE_S if workers > 1 else 0.0)
        harness.ingest(small_chunks(settings, workers=workers), embedder)

        assert embedder.waited == [workers > 1]
        assert unwritten_batches_high_water(harness.events) == expected, f"workers={workers}"


# --------------------------------------------------------------------------
# Failure
# --------------------------------------------------------------------------


class Tripwire(WordCounter):
    """Refuses one document, the way a tokenizer fault would: from inside a
    worker thread, part-way through a unit."""

    def __init__(self, marker: str) -> None:
        super().__init__(max_delay_ms=3)
        self.marker = marker

    def count_tokens_batch(self, texts: Any) -> list[int]:
        if any(self.marker in text for text in texts):
            raise ValueError(f"tokenizer refused {self.marker}")
        return super().count_tokens_batch(texts)


def test_a_worker_exception_fails_the_unit_and_stops_everything_after_it(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Document 12 of the first unit: third batch, third document. `prose`
    # numbers the first unit's documents from 100.
    outcomes = []
    for workers in WORKER_COUNTS:
        harness = Recording(monkeypatch, units=["u1", "u2"])
        with pytest.raises(ValueError, match="tokenizer refused closing112x0") as failure:
            harness.ingest(small_chunks(settings, workers=workers), Tripwire("closing112x0"))

        stored = harness.stored()
        # Both batches before the failing one are committed, in order, and
        # nothing else is: not the failing batch, not a later one, no mark of
        # any kind, and nothing of the next unit.
        assert [event[1] for event in stored] == [("u1", 0), ("u1", 1)], f"workers={workers}"
        assert harness.loaded == ["u1"]
        # Asserted with the traceback still held, which is how a real failure
        # travels: up to the CLI, alive while it is reported. A generator that
        # is only finalised when its frame is collected would pass this with
        # the traceback dropped, and leave threads chunking under a live one.
        assert failure.tb is not None
        assert no_chunk_threads(), "the pool outlived the failure"
        outcomes.append(stored)
    assert outcomes[0] == outcomes[1] == outcomes[2]


class FailingEncoder(WordCounter):
    def __init__(self) -> None:
        super().__init__(max_delay_ms=3)
        self.calls = 0

    def encode(self, texts: Any) -> list[list[float]]:
        self.calls += 1
        if self.calls == 2:
            raise RuntimeError("the GPU went away")
        return super().encode(texts)


def test_a_failure_on_the_consuming_side_leaves_no_thread_and_no_queue_behind(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same exit an interrupt takes: out through the loop body, with a
    batch already submitted behind the one that failed."""
    for workers in WORKER_COUNTS:
        harness = Recording(monkeypatch, units=["u1", "u2"], documents_per_unit=40)
        with pytest.raises(RuntimeError, match="the GPU went away") as failure:
            harness.ingest(small_chunks(settings, workers=workers), FailingEncoder())
        assert [event[1] for event in harness.stored()] == [("u1", 0)]
        # Traceback held on purpose -- see the worker-exception test above.
        assert failure.tb is not None
        assert no_chunk_threads(), "chunking threads were left running"
        # Batch 2 was legitimately submitted behind batch 1. Nothing past it
        # may ever have started: that work was never queued.
        started = {event[2][1] for event in harness.events if event[0] == "chunk_start"}
        assert started <= {0, 1, 2}, f"workers={workers} chunked ahead to {sorted(started)}"


# --------------------------------------------------------------------------
# `_chunk_ahead` on its own
# --------------------------------------------------------------------------


def document(n: int, words: int) -> DatedDocument:
    return DatedDocument(
        document_id=f"d{n}",
        source="govpr",
        source_ref=f"p/{n}",
        url=f"https://example.org/{n}",
        title=f"d{n}",
        body=" ".join(f"w{n}x{i}" for i in range(words)),
        published_at=datetime(2025, 1, 1, tzinfo=UTC),
        simhash=n,
    )


def slow_chunk(doc: DatedDocument) -> list[Chunk]:
    """Pure, and slower for some documents than others."""
    time.sleep((zlib.crc32(doc.document_id.encode()) % 4) / 1000)
    words = doc.body.split()
    return [
        Chunk(
            chunk_id=f"{doc.document_id}#{ordinal}",
            document_id=doc.document_id,
            ordinal=ordinal,
            body=" ".join(words[start : start + 7]),
            token_count=len(words[start : start + 7]),
            published_at=doc.published_at,
        )
        for ordinal, start in enumerate(range(0, len(words), 7))
    ]


@hypothesis_settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    lengths=st.lists(st.integers(min_value=0, max_value=40), min_size=0, max_size=24),
    batch_size=st.integers(min_value=1, max_value=7),
    workers=st.integers(min_value=2, max_value=6),
)
def test_chunk_ahead_yields_what_the_serial_branch_yields(
    lengths: list[int], batch_size: int, workers: int
) -> None:
    documents = [document(n, words) for n, words in enumerate(lengths)]
    batches = [documents[i : i + batch_size] for i in range(0, len(documents), batch_size)]
    serial = list(pipeline._chunk_ahead(batches, workers=1, chunk=slow_chunk))
    parallel = list(pipeline._chunk_ahead(batches, workers=workers, chunk=slow_chunk))
    assert parallel == serial
    # Empty documents and empty batches' worth of chunks are yielded, not
    # dropped: skipping a batch with nothing to embed is the consumer's call.
    assert [batch for batch, _ in parallel] == batches


def test_closing_the_generator_early_cancels_what_was_queued(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What `--max-units`, a stop rule or Ctrl-C leaves behind: nothing.

    Batch 1 is held with two documents inside the workers and two queued, and
    the generator is closed. The held documents are released only once the
    queued ones have been cancelled, so whether the queue was cancelled -- or
    merely drained by the pool's exit -- is visible in what ran.
    """
    from concurrent.futures import Future

    started: list[str] = []
    batch_one_is_held = threading.Event()
    queue_was_cancelled = threading.Event()
    cancelled: list[bool] = []
    real_cancel = Future.cancel

    def cancel(future: Future[Any]) -> bool:
        cancelled.append(real_cancel(future))
        if cancelled.count(True) == 2:
            queue_was_cancelled.set()
        return cancelled[-1]

    monkeypatch.setattr(Future, "cancel", cancel)

    def held(doc: DatedDocument) -> list[Chunk]:
        started.append(doc.document_id)
        if doc.document_id in ("d4", "d5"):
            if {"d4", "d5"} <= set(started):
                batch_one_is_held.set()
            queue_was_cancelled.wait(PATIENCE_S)
        return slow_chunk(doc)

    documents = [document(n, 10) for n in range(12)]
    batches = [documents[i : i + 4] for i in range(0, 12, 4)]
    ahead = pipeline._chunk_ahead(batches, workers=2, chunk=held)

    first_batch, first_chunks = next(ahead)
    assert [doc.document_id for doc in first_batch] == ["d0", "d1", "d2", "d3"]
    assert len(first_chunks) == 8
    assert batch_one_is_held.wait(PATIENCE_S)
    ahead.close()

    assert no_chunk_threads()
    # d4 and d5 were running and cannot be recalled; d6 and d7 were queued
    # behind them and must never have run; batch 2 was never submitted at all.
    assert sorted(started) == ["d0", "d1", "d2", "d3", "d4", "d5"]
    assert cancelled.count(True) == 2


def test_chunk_workers_below_one_is_refused_at_load(settings: Settings) -> None:
    from pydantic import ValidationError

    from cascade.config import CorpusConfig

    with pytest.raises(ValidationError, match="chunk_workers"):
        CorpusConfig(**{**settings.corpus.model_dump(), "chunk_workers": 0})
