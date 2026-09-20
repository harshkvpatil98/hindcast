"""Re-dating stored CC-NEWS documents by fetch time (ADR-0044): the pure parts."""

from __future__ import annotations

from datetime import UTC, datetime

from cascade.corpus.redate import fetch_times, gap_summary


def at(day: int, hour: int = 0) -> datetime:
    return datetime(2026, 3, day, hour, tzinfo=UTC)


def test_a_url_fetched_twice_keeps_the_later_fetch() -> None:
    times = fetch_times([("https://a", at(1)), ("https://a", at(3)), ("https://a", at(2))])
    assert times == {"ccnews:https://a": at(3)}


def test_a_record_without_a_readable_date_contributes_nothing() -> None:
    """Never a guessed fetch time: the document stays unmatched, and an
    unmatched document is deleted rather than dated by assumption."""
    assert fetch_times([("https://a", None)]) == {}


def test_ids_match_how_the_ingest_keys_documents() -> None:
    """The ingest stores `ccnews:<target uri>`; a mismatch here would leave
    every document unmatched and delete the corpus as orphans."""
    from cascade.corpus.normalize import validate
    from cascade.corpus.schema import RawDocument

    document = validate(
        RawDocument(
            source="ccnews",
            source_ref="https://example.com/x",
            body="A long enough body about a port strike and its settlement. " * 6,
            published_at=at(1),
        ),
        now=at(20),
    )
    assert not isinstance(document, str)
    assert set(fetch_times([("https://example.com/x", at(2))])) == {document.document_id}


def test_the_gap_summary_counts_by_threshold() -> None:
    summary = gap_summary((0.05, 0.5, 2.0, 10.0, 40.0, 200.0))
    assert summary["moved"] == 6
    assert (summary["over_1_day"], summary["over_7_days"]) == (4, 3)
    assert (summary["over_30_days"], summary["over_180_days"]) == (2, 1)
    assert summary["median_days"] == 10.0
