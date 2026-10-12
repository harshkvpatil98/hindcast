"""Re-date stored CC-NEWS documents by when their text was fetched (ADR-0044).

Every CC-NEWS document ingested before ADR-0044 is dated by the publication
date its page states. The text stored is the text Common Crawl fetched, which
for a re-crawled page can be months newer than that date. This module re-reads
the WARC *headers* of each finished unit -- no chunking, no embedding -- and
moves each stored document to ``max(stated, fetched)``, keeping the stated
date in ``stated_published_at`` and the fetch in ``crawled_at``.

Order-independent and resumable: each file's update takes ``GREATEST`` of what
is stored and what the file says, so a URL seen in several files ends at its
latest fetch whichever file is read first, a file read twice changes nothing,
and a finished file is recorded so a restart skips it.

A stored document that no finished unit contains came from a unit that was
interrupted and never marked done; its fetch time cannot be known, so it is
deleted, and the unit re-fetches it under the new rule when the ingest
resumes. Pure planning is separate from the shell so the rules are testable.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from cascade.config import Settings

__all__ = [
    "REDATE_SOURCE",
    "FileFetchTimes",
    "RedateReport",
    "fetch_times",
    "gap_summary",
    "run_redate",
]

# The corpus_ingest_state source under which a re-dated file is recorded.
REDATE_SOURCE = "ccnews-redate"


@dataclass(frozen=True)
class FileFetchTimes:
    """Every response record's fetch time in one WARC file, by document id."""

    unit_key: str
    times: dict[str, datetime]


def fetch_times(records: Iterable[tuple[str, datetime | None]]) -> dict[str, datetime]:
    """``document_id -> latest fetch`` over ``(target_uri, warc_date)`` pairs.

    Pure. Preserves the safe direction: a URL fetched twice in one file keeps
    the later fetch, and a record with no readable date contributes nothing --
    it is never given a guessed one.
    """
    out: dict[str, datetime] = {}
    for uri, fetched in records:
        if fetched is None:
            continue
        key = f"ccnews:{uri}"
        if key not in out or fetched > out[key]:
            out[key] = fetched
    return out


@dataclass(frozen=True)
class RedateReport:
    """What a re-dating pass did."""

    files: int
    files_skipped: int
    documents_matched: int
    documents_moved: int
    gap_days: tuple[float, ...]
    """(fetched - stated) in days for every moved document, sorted."""
    orphans_deleted: int
    chunks_deleted: int


def gap_summary(gaps: tuple[float, ...]) -> dict[str, float | int]:
    """Counts over the moved documents' gaps, for the printed report. Pure."""
    return {
        "moved": len(gaps),
        "over_1_day": sum(1 for gap in gaps if gap > 1),
        "over_7_days": sum(1 for gap in gaps if gap > 7),
        "over_30_days": sum(1 for gap in gaps if gap > 30),
        "over_180_days": sum(1 for gap in gaps if gap > 180),
        "median_days": gaps[len(gaps) // 2] if gaps else 0.0,
    }


def _connect(settings: Settings) -> Any:
    import psycopg

    return psycopg.connect(settings.database_url("admin"), connect_timeout=30)


def _stream_file(fetcher: Any, path: str) -> Iterator[tuple[str, datetime | None]]:
    from cascade.corpus.sources import ccnews

    client = fetcher._http()
    fetcher.requests += 1
    with client.stream("GET", f"{ccnews.DATA_URL}/{path}") as response:
        if response.status_code != 200:
            raise RuntimeError(f"{path}: HTTP {response.status_code}")
        for header, _ in ccnews._iter_warc_records(
            response.iter_bytes(chunk_size=1 << 20), max_records=10**9
        ):
            kind = ccnews._WARC_TYPE.search(header)
            if kind is None or kind.group(1).lower() != b"response":
                continue
            target = ccnews._WARC_TARGET.search(header)
            if target is None:
                continue
            yield target.group(1).decode("utf-8", "replace"), ccnews.warc_date(header)


def _apply_file(settings: Settings, times: dict[str, datetime]) -> tuple[int, list[float]]:
    """Move one file's documents and their chunks. One transaction per file."""
    with _connect(settings) as conn, conn.cursor() as cur:
        cur.execute(
            "CREATE TEMP TABLE fetched (document_id text PRIMARY KEY, crawled_at timestamptz) "
            "ON COMMIT DROP"
        )
        with cur.copy("COPY fetched (document_id, crawled_at) FROM STDIN") as copy:
            for document_id in sorted(times):
                copy.write_row((document_id, times[document_id]))
        # The documents this file holds, with their dates before and after.
        cur.execute("""
            CREATE TEMP TABLE moving ON COMMIT DROP AS
            SELECT d.document_id, d.published_at AS old_at,
                   GREATEST(d.published_at, f.crawled_at) AS new_at,
                   GREATEST(COALESCE(d.crawled_at, f.crawled_at), f.crawled_at) AS crawled_at,
                   COALESCE(d.stated_published_at, d.published_at) AS stated_at
            FROM documents d JOIN fetched f USING (document_id)
            WHERE d.source = 'ccnews'
            """)
        cur.execute("SELECT count(*) FROM moving")
        matched = int(cur.fetchone()[0])
        cur.execute(
            "SELECT EXTRACT(EPOCH FROM (new_at - stated_at)) / 86400.0 FROM moving "
            "WHERE new_at > old_at"
        )
        gaps = [float(row[0]) for row in cur.fetchall()]
        # Chunks first, keyed on the old date: afterwards the document's date
        # has moved and the pair would no longer identify them.
        cur.execute("""
            UPDATE chunks c SET published_at = m.new_at
            FROM moving m
            WHERE c.document_id = m.document_id AND c.published_at = m.old_at
              AND m.new_at > m.old_at
            """)
        cur.execute("""
            UPDATE documents d
            SET published_at = m.new_at, crawled_at = m.crawled_at,
                stated_published_at = m.stated_at
            FROM moving m
            WHERE d.document_id = m.document_id AND d.published_at = m.old_at
            """)
        conn.commit()
    return matched, gaps


def _record_file(settings: Settings, unit_key: str, matched: int) -> None:
    from cascade.corpus.store import mark_unit

    mark_unit(
        settings,
        source=REDATE_SOURCE,
        unit_key=unit_key,
        state="done",
        n_documents=matched,
        detail="re-dated by fetch time (ADR-0044)",
    )


def _delete_orphans(settings: Settings) -> tuple[int, int]:
    """Remove CC-NEWS documents no finished file vouches for, and their chunks."""
    with _connect(settings) as conn, conn.cursor() as cur:
        cur.execute(
            "CREATE TEMP TABLE orphans ON COMMIT DROP AS "
            "SELECT document_id, published_at FROM documents "
            "WHERE source = 'ccnews' AND crawled_at IS NULL"
        )
        cur.execute(
            "DELETE FROM chunks c USING orphans o "
            "WHERE c.document_id = o.document_id AND c.published_at = o.published_at"
        )
        chunks = cur.rowcount
        cur.execute(
            "DELETE FROM documents d USING orphans o "
            "WHERE d.document_id = o.document_id AND d.published_at = o.published_at"
        )
        documents = cur.rowcount
        conn.commit()
    return documents, chunks


def run_redate(
    settings: Settings, *, limit: int | None, delete_orphans: bool, progress: Any = None
) -> RedateReport:
    """Re-date every finished CC-NEWS unit's documents; optionally drop orphans.

    Orphans are deleted only once every finished unit has been re-dated, and
    only when asked: before that, "no file vouches for it" may just mean "its
    file has not been read yet".
    """
    from cascade.corpus.fetch import Fetcher
    from cascade.corpus.sources import ccnews
    from cascade.corpus.store import completed_units

    fetcher = Fetcher(
        requests_per_second=settings.corpus.requests_per_second,
        user_agent=settings.corpus.contact,
    )
    units = sorted(
        key
        for unit in completed_units(settings, "ccnews")
        for key in ccnews.expand_legacy_unit(unit)
    )
    done = completed_units(settings, REDATE_SOURCE)
    pending = [key for key in units if key not in done]
    if limit is not None:
        pending = pending[:limit]

    listings: dict[str, list[str]] = {}
    matched_total = moved_total = 0
    gaps: list[float] = []
    for index, unit_key in enumerate(pending, start=1):
        month, file_index = ccnews.split_unit(unit_key)
        if month not in listings:
            listings[month] = ccnews.month_paths(fetcher, unit_key=month)
        path = listings[month][file_index]
        times = fetch_times(_stream_file(fetcher, path))
        matched, file_gaps = _apply_file(settings, times)
        _record_file(settings, unit_key, matched)
        matched_total += matched
        moved_total += len(file_gaps)
        gaps.extend(file_gaps)
        if progress is not None:
            progress(index, len(pending), unit_key, matched, len(file_gaps))

    orphans = chunks = 0
    finished = not [key for key in units if key not in completed_units(settings, REDATE_SOURCE)]
    if delete_orphans and finished:
        orphans, chunks = _delete_orphans(settings)
    return RedateReport(
        files=len(pending),
        files_skipped=len(units) - len(pending) if limit is None else len(done),
        documents_matched=matched_total,
        documents_moved=moved_total,
        gap_days=tuple(sorted(gaps)),
        orphans_deleted=orphans,
        chunks_deleted=chunks,
    )
