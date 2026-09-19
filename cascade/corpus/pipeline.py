"""The ingest pipeline: fetch -> normalize -> date-validate -> dedupe ->
chunk -> embed -> index (spec §3.2).

The thin shell around the pure stages. Everything that decides *whether* a
document is usable lives in ``normalize.py``, everything that decides how it is
split lives in ``chunker.py``, and neither can reach a network or a clock.

Resumability is per unit (invariant 8). A unit is an EDGAR quarter, a Federal
Register month, a CC-NEWS WARC file, a GDELT month-query, or one depth of one
scenario's Wikipedia snapshots. Units are marked done only after their rows are
committed, so a crash mid-unit re-does that unit and nothing else.

Rate limits are **per source**, because they are properties of the upstream:
GDELT rejects faster than one request per five seconds, SEC EDGAR allows ten,
and letting one source spend another's budget is how a source starts returning
nothing for reasons that look like an empty archive.
"""

from __future__ import annotations

import shutil
from collections import deque
from collections.abc import Callable, Generator, Iterable, Iterator, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial

from cascade.config import Settings, repo_root
from cascade.corpus.anchored import CrawlFile, parse_listing, plan_anchored
from cascade.corpus.chunker import chunk_text
from cascade.corpus.coverage import demand_profile, order_units
from cascade.corpus.embed import Embedder
from cascade.corpus.fetch import Fetcher
from cascade.corpus.normalize import DedupeIndex, deduplicate, validate
from cascade.corpus.schema import Chunk, DatedDocument, RawDocument
from cascade.corpus.sources import ccnews, edgar, gdelt, govpr, wikipedia
from cascade.corpus.store import (
    completed_units,
    existing_document_ids,
    mark_unit,
    seen_simhashes,
    stored_chunk_count,
    write_batch,
)

__all__ = ["IngestReport", "SourceReport", "run_ingest"]

# GDELT documents a hard floor of one request per five seconds and answers
# faster clients with HTTP 429. This is not tuning; it is the published limit.
GDELT_REQUESTS_PER_SECOND = 0.2


@dataclass
class SourceReport:
    """What one source contributed, and what it discarded."""

    source: str
    units_done: int = 0
    units_skipped: int = 0
    units_failed: int = 0
    documents_seen: int = 0
    documents_written: int = 0
    chunks_written: int = 0
    dropped: dict[str, int] = field(default_factory=dict)
    collapsed: int = 0
    detail: str = ""

    def drop(self, reason: str) -> None:
        self.dropped[reason] = self.dropped.get(reason, 0) + 1


@dataclass
class IngestReport:
    """The whole run, in the shape the acceptance criteria are stated in."""

    sources: list[SourceReport] = field(default_factory=list)
    # Why the run stopped before exhausting its queue, if it did. `disk` is a
    # precondition failure (exit 3); `ceiling` is the build finishing its work.
    stopped: str | None = None
    stop_detail: str = ""

    @property
    def documents_written(self) -> int:
        return sum(report.documents_written for report in self.sources)

    @property
    def chunks_written(self) -> int:
        return sum(report.chunks_written for report in self.sources)

    @property
    def collapsed(self) -> int:
        return sum(report.collapsed for report in self.sources)

    @property
    def documents_seen(self) -> int:
        return sum(report.documents_seen for report in self.sources)

    @property
    def collapse_ratio(self) -> float:
        """Fraction of validated documents absorbed as near-duplicates."""
        total = self.documents_written + self.collapsed
        return self.collapsed / total if total else 0.0


def _wikipedia_requests(settings: Settings) -> dict[str, wikipedia.SnapshotUnit]:
    """Plan ``corpus.wikipedia_depth`` snapshot units per scenario, each anchored at its cutoff.

    This is the source's whole point: the article as it stood at the cutoff.
    Anchoring to the scenario that will be forecast from it is what makes the
    snapshot honest -- a single global "as of" would be too late for early
    scenarios and needlessly early for late ones.

    Preserves additivity (ADR-0023's lesson, applied here): depth is part of
    the unit key, so raising ``wikipedia_depth`` adds keys and never changes
    what an existing key means. A unit keyed by the bare scenario id is the
    previous adapter's and is not planned any more; its `done` row stays valid
    and simply matches nothing, so it is neither re-run nor in the way.

    Reads ``scenarios`` only. Titles come from the question, the registry's
    own ``party_names`` and the cutoff, and no outcome is touched (invariant 2).
    """
    from cascade.ledger.store import load_scenarios

    units: dict[str, wikipedia.SnapshotUnit] = {}
    for scenario in load_scenarios(settings, role="admin"):
        for depth in range(1, settings.corpus.wikipedia_depth + 1):
            unit = wikipedia.plan_unit(
                question=scenario.question,
                party_names=scenario.party_names,
                cutoff=scenario.cutoff_ts,
                depth=depth,
            )
            if unit.titles or unit.hop_sources:
                key = wikipedia.unit_key(scenario.scenario_id, scenario.cutoff_ts, depth)
                units[key] = unit
    return units


def _scenario_cutoffs(settings: Settings) -> list[datetime]:
    """The registry's cutoffs, which are what the ingest is trying to cover.

    Reads ``scenarios`` only -- a cutoff is not an outcome, and
    ``scenario_labels`` is not touched here or anywhere in the corpus
    pipeline (invariant 2). An empty registry is not an error: ordering then
    degrades to breadth-first chronological, which is what it was before
    demand existed.
    """
    from cascade.ledger.store import load_scenarios

    try:
        return [scenario.cutoff_ts for scenario in load_scenarios(settings, role="admin")]
    except Exception:  # noqa: BLE001 -- a corpus build must not require a registry
        return []


def _units_for(settings: Settings, source: str) -> list[str]:
    corpus = settings.corpus
    span = {"start_year": corpus.start_year, "end_year": corpus.end_year}
    if source == "govpr":
        return govpr.unit_keys(**span)
    if source == "edgar":
        return edgar.unit_keys(**span)
    if source == "gdelt":
        return gdelt.unit_keys(**span)
    if source == "ccnews":
        return ccnews.unit_keys(**span, max_files=corpus.ccnews_max_files)
    if source == "wikipedia":
        return sorted(_wikipedia_requests(settings))
    raise ValueError(f"unknown corpus source: {source}")


def _documents_for(
    settings: Settings,
    source: str,
    unit_key: str,
    fetchers: dict[str, Fetcher],
    wiki_units: dict[str, wikipedia.SnapshotUnit],
) -> Iterator[RawDocument]:
    corpus = settings.corpus
    limit = corpus.max_documents_per_unit
    if source == "govpr":
        return govpr.load_page(fetchers["govpr"], unit_key=unit_key, max_documents=limit)
    if source == "edgar":
        return edgar.load_quarter(fetchers["edgar"], unit_key=unit_key, max_documents=limit)
    if source == "gdelt":
        return gdelt.load_window(
            fetchers["gdelt"],
            fetchers["web"],
            unit_key=unit_key,
            max_records=corpus.gdelt_max_records,
        )
    if source == "wikipedia":
        return wikipedia.load_snapshots(fetchers["wikipedia"], wiki_units[unit_key])
    if source == "ccnews":
        return _ccnews_unit(settings, unit_key, fetchers["ccnews"])
    raise ValueError(f"unknown corpus source: {source}")


def _ccnews_unit(settings: Settings, unit_key: str, fetcher: Fetcher) -> Iterator[RawDocument]:
    """Stream one WARC file -- the k-th of its month -- as one unit.

    The month's path list is fetched to resolve k, which costs one small
    gzipped request per unit. That is the price of file-level resumability:
    the alternative is caching the list across units, which would make a
    unit's meaning depend on what ran before it in the same process.

    A month whose list is shorter than k yields nothing and the unit is
    recorded done-and-empty, because a file that does not exist is not an
    outage. A month with no list at all raises, and the unit is recorded
    failed -- `unit_keys` already excludes the months the collection does not
    cover, so that case is a real fetch failure.
    """
    corpus = settings.corpus
    month, index = ccnews.split_unit(unit_key)
    paths = ccnews.month_paths(fetcher, unit_key=month)
    if index >= len(paths):
        return
    yield from ccnews.load_warc(
        fetcher, path=paths[index], max_records=corpus.ccnews_max_records_per_file
    )


def _prefetch(
    unit_keys: Sequence[str],
    *,
    workers: int,
    load: Callable[[str], list[RawDocument]],
) -> Iterator[tuple[str, list[RawDocument] | Exception]]:
    """Fetch ahead by ``workers`` units, yielding results in submission order.

    Fetching and the CPU-bound stages downstream -- chunking, tokenizing,
    embedding -- alternate rather than overlap: measured single-stream, this
    pipeline spent ~75% of wall time waiting on a download with the tokenizer
    idle. Overlapping them is worth ~1.8x, and the earlier version bought it
    by streaming several WARC files *inside* one unit. That is no longer
    available, because a unit is now one file (ADR-0023) -- so the overlap
    moves up a level, to a bounded lookahead across units.

    Order is preserved, which is what keeps this an optimisation rather than a
    behaviour change: units are committed and marked in exactly the sequence
    they would have run in sequentially, so an interruption leaves the same
    state either way. ``load`` is called from worker threads and must not
    share a rate limiter or an HTTP client between calls.

    An exception is yielded rather than raised so one unreachable unit is
    recorded failed and the rest of the queue still runs.
    """
    if workers <= 1:
        for unit_key in unit_keys:
            try:
                yield unit_key, load(unit_key)
            except Exception as exc:  # noqa: BLE001 -- reported per unit, never fatal
                yield unit_key, exc
        return

    with ThreadPoolExecutor(max_workers=workers) as pool:
        pending: deque[tuple[str, Future[list[RawDocument]]]] = deque()
        upcoming = iter(unit_keys)
        try:
            while True:
                while len(pending) < workers:
                    nxt = next(upcoming, None)
                    if nxt is None:
                        break
                    pending.append((nxt, pool.submit(load, nxt)))
                if not pending:
                    return
                unit_key, future = pending.popleft()
                try:
                    yield unit_key, future.result()
                except Exception as exc:  # noqa: BLE001 -- reported per unit
                    yield unit_key, exc
        finally:
            # A consumer that stops early (``--max-units``, a budget abort)
            # must not leave downloads running against the politeness budget.
            for _, future in pending:
                future.cancel()


def _unit_loader(
    settings: Settings,
    source: str,
    fetchers: dict[str, Fetcher],
    wiki_units: dict[str, wikipedia.SnapshotUnit],
) -> tuple[Callable[[str], list[RawDocument]], int]:
    """Return a thread-safe loader for ``source`` and how many may run at once.

    Only CC-NEWS is prefetched. It is the source with the volume, and it is
    the only one whose upstream is a static file store rather than a rate-
    limited API -- Common Crawl publishes no request floor, while GDELT
    publishes a five-second one and EDGAR ten per second. Running those
    concurrently would spend a politeness budget the ingest depends on.
    """
    corpus = settings.corpus
    if source != "ccnews":

        def sequential(unit_key: str) -> list[RawDocument]:
            return list(_documents_for(settings, source, unit_key, fetchers, wiki_units))

        return sequential, 1

    def concurrent(unit_key: str) -> list[RawDocument]:
        # Its own Fetcher per call: sharing one would serialise the downloads
        # behind that fetcher's rate limiter, which is what this avoids, and
        # `httpx.Client` reuse across threads is not what the limiter is for.
        worker = Fetcher(requests_per_second=corpus.requests_per_second, user_agent=corpus.contact)
        try:
            return list(_ccnews_unit(settings, unit_key, worker))
        finally:
            worker.close()

    return concurrent, max(1, corpus.fetch_workers)


def _listing_months(cutoffs: Sequence[datetime], done: Iterable[str]) -> list[str]:
    """Months whose listings the anchored planner needs. Pure.

    Each cutoff's own month and the two before it -- beyond that a file is worth
    under 2% at a 14-day half-life -- plus every month already ingested, so what
    is held counts as evidence.
    """
    months = {unit.split("#")[0] for unit in done}
    for cutoff in cutoffs:
        year, month = cutoff.year, cutoff.month
        for _ in range(3):
            if (year, month) >= (ccnews.FIRST_YEAR, ccnews.FIRST_MONTH):
                months.add(f"{year:04d}/{month:02d}")
            year, month = (year, month - 1) if month > 1 else (year - 1, 12)
    return sorted(months)


def _anchored_units(settings: Settings, fetcher: Fetcher, done: Iterable[str]) -> list[str]:
    """The cutoff-anchored CC-NEWS plan, already in ingest order (ADR-0036).

    A listing that cannot be fetched raises: planning around a month nobody
    could list would silently drop the scenarios that month serves, and the run
    is resumable, so failing loudly costs a retry and nothing else.
    """
    corpus = settings.corpus
    held = sorted(set(done))
    cutoffs = _scenario_cutoffs(settings)
    listings: dict[str, list[CrawlFile]] = {}
    for month in _listing_months(cutoffs, held):
        listings[month] = parse_listing(month, ccnews.month_paths(fetcher, unit_key=month))
    return plan_anchored(
        cutoffs,
        listings,
        done=held,
        max_files=corpus.anchor_plan_files,
        half_life_days=corpus.anchor_half_life_days,
        lookback_days=corpus.coverage_lookback_months * 30.5,
        floor_days=corpus.anchor_floor_days,
        floor_min_rescued=corpus.anchor_floor_min_rescued,
        equity=corpus.anchor_equity,
    )


def _free_disk_gb() -> float:
    """Free space on the volume holding the repository, in GB."""
    return shutil.disk_usage(repo_root()).free / 1_000_000_000


def _chunks_for(document: DatedDocument, settings: Settings, embedder: Embedder) -> list[Chunk]:
    """Split one document, counting tokens the way the model does."""
    corpus = settings.corpus
    plans = chunk_text(
        document.body,
        count_tokens=embedder.count_tokens,
        count_tokens_batch=embedder.count_tokens_batch,
        max_tokens=corpus.chunk_max_tokens,
        overlap_tokens=corpus.chunk_overlap_tokens,
    )
    return [
        Chunk(
            chunk_id=f"{document.document_id}#{ordinal}",
            document_id=document.document_id,
            ordinal=ordinal,
            body=plan.body,
            token_count=plan.token_count,
            published_at=document.published_at,
        )
        for ordinal, plan in enumerate(plans)
    ]


def _chunk_ahead(
    batches: Sequence[Sequence[DatedDocument]],
    *,
    workers: int,
    chunk: Callable[[DatedDocument], list[Chunk]],
) -> Generator[tuple[Sequence[DatedDocument], list[Chunk]], None, None]:
    """Chunk one batch ahead of the consumer, yielding batches in order.

    Chunking and embedding alternated on one thread: sampled on a live ingest,
    it was either inside the tokenizer with the GPU idle or inside the
    embedding call with the CPUs near idle, and both release the GIL. So the
    documents of a batch are chunked on a pool, and the *next* batch is
    submitted just before this one is handed over -- it chunks while the
    consumer embeds and writes.

    Preserves the serial loop's output exactly, for any ``workers``. A
    document's chunks are a function of that document alone, futures are read
    in submission order and never in completion order, and batches are yielded
    in the order given, so the consumer sees the same chunk list, element for
    element, that the ``workers <= 1`` branch builds -- and that branch is the
    loop this replaced, with no thread anywhere in it.

    The lookahead is one batch and is not tunable. The next batch is submitted
    only after the current one has been fully collected, so at most two
    batches' chunks exist unwritten at any moment -- the one being embedded
    and the one behind it -- whatever ``workers`` is. Deeper lookahead could
    not make the consumer faster, only the heap larger, on a machine whose
    memory the embedding model shares with the GPU.

    A worker's exception surfaces from ``result()`` when its document's turn
    comes, which is where the serial loop would have raised it: after every
    earlier batch was written and before anything later is. ``chunk`` runs on
    worker threads and must not mutate shared state.
    """
    if workers <= 1:
        for batch in batches:
            yield batch, [piece for document in batch for piece in chunk(document)]
        return

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="chunk") as pool:
        # Never longer than two: the batch being collected or consumed, and the
        # one submitted behind it.
        outstanding: deque[tuple[Sequence[DatedDocument], list[Future[list[Chunk]]]]] = deque()
        upcoming = iter(batches)

        def submit_next() -> None:
            nxt = next(upcoming, None)
            if nxt is not None:
                outstanding.append((nxt, [pool.submit(chunk, document) for document in nxt]))

        try:
            submit_next()
            while outstanding:
                batch, futures = outstanding[0]
                chunks: list[Chunk] = []
                for future in futures:
                    chunks.extend(future.result())
                outstanding.popleft()
                submit_next()
                yield batch, chunks
        finally:
            # Reached on a worker's exception, on the consumer's, and on an
            # interrupt. Queued documents are cancelled so the pool's exit
            # waits only for the few already running -- the failing batch's
            # own tail included, which is why it is popped only once collected.
            for _, futures in outstanding:
                for future in futures:
                    future.cancel()


def run_ingest(
    settings: Settings,
    *,
    now: datetime,
    sources: tuple[str, ...] | None = None,
    max_units_per_source: int | None = None,
    embedder: Embedder | None = None,
    free_disk_gb: Callable[[], float] | None = None,
) -> IngestReport:
    """Run the pipeline for each enabled source.

    ``now`` is required rather than read from the clock, for the same reason
    ``as_of`` is (invariant 1): a future-date check that consults the wall
    clock gives different answers on different runs.

    Preserves invariant 8 under both stop rules: a unit is only ever skipped
    whole, never abandoned half-written, so a stopped build resumes exactly
    where it left off. ``free_disk_gb`` is injected so the guard is testable
    without filling a disk.

    ``corpus.chunk_workers`` changes when chunking happens and never what is
    stored: embedding, writing and marking stay on this thread, in batch
    order, and the lookahead never crosses a unit boundary -- so both stop
    rules still fire between units, with nothing of the next unit started.
    """
    corpus = settings.corpus
    measure_disk = free_disk_gb if free_disk_gb is not None else _free_disk_gb
    selected = sources if sources is not None else corpus.enabled_sources
    report = IngestReport()

    model = embedder or Embedder(
        model_name=settings.models.embedding, batch_size=corpus.embed_batch_size
    )
    model.load()
    # Bound once, outside every loop: the workers call it, and a closure over
    # a loop variable is how a pool ends up chunking for the wrong iteration.
    chunk = partial(_chunks_for, settings=settings, embedder=model)

    index = DedupeIndex()
    index.seed(seen_simhashes(settings))
    known_ids = existing_document_ids(settings)
    stored_at_start = stored_chunk_count(settings)

    fetchers = {
        "govpr": Fetcher(requests_per_second=corpus.requests_per_second, user_agent=corpus.contact),
        "edgar": Fetcher(requests_per_second=corpus.requests_per_second, user_agent=corpus.contact),
        "wikipedia": Fetcher(
            # Its own, slower budget: Wikimedia asks automated readers to go
            # serially and gently, and the whole pass is a few thousand requests.
            requests_per_second=corpus.wikipedia_requests_per_second,
            user_agent=corpus.contact,
            # MediaWiki reports `maxlag` and rate limits under HTTP 200. Read
            # as answers they say "no such article", and the unit is marked
            # done -- for good -- with nothing in it.
            classify_body=wikipedia.classify_body,
        ),
        "ccnews": Fetcher(
            requests_per_second=corpus.requests_per_second, user_agent=corpus.contact
        ),
        "gdelt": Fetcher(
            requests_per_second=GDELT_REQUESTS_PER_SECOND,
            user_agent=corpus.contact,
            # GDELT serves refusals under 200 as readily as under 429. Without
            # this the notice reaches the JSON parser and the unit dies with a
            # parse error instead of backing off and retrying.
            classify_body=gdelt.classify_body,
        ),
        "web": Fetcher(
            requests_per_second=corpus.requests_per_second,
            user_agent=corpus.contact,
            max_retries=1,
        ),
    }
    wiki_units = _wikipedia_requests(settings) if "wikipedia" in selected else {}
    # Computed once for the whole run: it is a function of the registry, which
    # is sealed, so recomputing it per source could only introduce drift.
    demand = demand_profile(
        _scenario_cutoffs(settings), lookback_months=corpus.coverage_lookback_months
    )

    try:
        for source in selected:
            source_report = SourceReport(source=source)
            report.sources.append(source_report)

            try:
                units = _units_for(settings, source)
            except Exception as exc:  # noqa: BLE001 -- reported per source, never fatal
                source_report.detail = f"{type(exc).__name__}: {exc}"[:200]
                continue

            done = completed_units(settings, source)
            if source == "ccnews":
                # A month-level `done` row predates file-level units and covers
                # the files that month's ingest actually read (ADR-0023).
                done = {
                    expanded
                    for unit in sorted(done)
                    for expanded in ccnews.expand_legacy_unit(unit)
                }
            pending = [unit for unit in units if unit not in done]
            source_report.units_skipped = len(units) - len(pending)
            if source == "ccnews" and corpus.ccnews_planning == "anchored":
                # The plan is already ordered, and already excludes what is
                # held; the swept unit list above is kept only for the skipped
                # count. A listing failure is reported like any source failure.
                try:
                    pending = _anchored_units(settings, fetchers["ccnews"], done)
                except Exception as exc:  # noqa: BLE001 -- reported per source, never fatal
                    source_report.detail = f"{type(exc).__name__}: {exc}"[:200]
                    continue
            else:
                pending = [plan.unit_key for plan in order_units(pending, demand=demand)]
            if max_units_per_source is not None:
                pending = pending[:max_units_per_source]

            load, workers = _unit_loader(settings, source, fetchers, wiki_units)
            for unit_key, fetched in _prefetch(pending, workers=workers, load=load):
                # Checked before a unit is *processed*, so a stop never leaves
                # one half-written. The unit in hand was already fetched by the
                # lookahead and is dropped unmarked; the next run fetches it
                # again, which is the price of never skipping it.
                stored = stored_at_start + report.chunks_written
                if stored >= corpus.max_chunks:
                    report.stopped = "ceiling"
                    report.stop_detail = (
                        f"{stored:,} chunks stored, at or above corpus.max_chunks "
                        f"({corpus.max_chunks:,})"
                    )
                    break
                if corpus.min_free_disk_gb is not None:
                    free = measure_disk()
                    if free < corpus.min_free_disk_gb:
                        report.stopped = "disk"
                        report.stop_detail = (
                            f"{free:.1f} GB free, below corpus.min_free_disk_gb "
                            f"({corpus.min_free_disk_gb:g})"
                        )
                        break
                if isinstance(fetched, Exception):
                    source_report.units_failed += 1
                    mark_unit(
                        settings,
                        source=source,
                        unit_key=unit_key,
                        state="failed",
                        detail=f"{type(fetched).__name__}: {fetched}",
                    )
                    continue
                raw_documents = fetched

                source_report.documents_seen += len(raw_documents)
                validated: list[DatedDocument] = []
                for raw in raw_documents:
                    outcome = validate(raw, now=now)
                    if isinstance(outcome, str):
                        source_report.drop(outcome)
                        continue
                    if outcome.document_id in known_ids:
                        source_report.drop("duplicate")
                        continue
                    known_ids.add(outcome.document_id)
                    validated.append(outcome)

                collapsed = deduplicate(validated, index=index)
                source_report.collapsed += collapsed.collapsed

                documents = collapsed.kept
                if not documents:
                    mark_unit(
                        settings, source=source, unit_key=unit_key, state="done", detail="empty"
                    )
                    source_report.units_done += 1
                    continue

                # Flush in bounded batches rather than accumulating the whole
                # unit. A CC-NEWS month can hold ~100k chunks, and holding
                # their text and vectors before a single write would put
                # hundreds of megabytes on the heap and lose all of it to one
                # interruption. Batching bounds memory and commits progress as
                # it goes; the unit is still only marked done at the end, so a
                # crash mid-unit re-does the unit and never skips it.
                batch_size = max(1, settings.corpus.write_batch_size)
                batches = [
                    documents[start : start + batch_size]
                    for start in range(0, len(documents), batch_size)
                ]
                unit_chunks = 0
                # `closing`, because an exception out of this loop keeps the
                # generator alive for as long as the traceback does, and its
                # `finally` is what stops the pool. Closed here, no chunking
                # thread outlives the unit that started it.
                with closing(
                    _chunk_ahead(batches, workers=corpus.chunk_workers, chunk=chunk)
                ) as chunked:
                    for batch, chunks in chunked:
                        if not chunks:
                            continue
                        vectors = model.encode([piece.body for piece in chunks])
                        written_documents, written_chunks = write_batch(
                            settings, batch, chunks, vectors
                        )
                        source_report.documents_written += written_documents
                        source_report.chunks_written += written_chunks
                        unit_chunks += written_chunks

                source_report.units_done += 1
                mark_unit(
                    settings,
                    source=source,
                    unit_key=unit_key,
                    state="done",
                    n_documents=len(documents),
                    # This unit's own chunks. It used to record the source's
                    # running total, so every row after the first overstated.
                    n_chunks=unit_chunks,
                )
            if report.stopped is not None:
                break
    finally:
        # Sorted (invariant 7). Close order does not matter today, but the
        # rule is that no mapping is ever walked in insertion order -- and the
        # static check does not carve out exceptions for cleanup paths.
        for _, fetcher in sorted(fetchers.items()):
            fetcher.close()

    return report
