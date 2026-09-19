"""Date validation and near-duplicate collapse (spec §3.2).

Two M2 acceptance criteria are rooted here: zero NULL/future/naive dates in
the corpus, and earliest-date retention on dedupe. Both are leakage rules, so
they are tested on the pure functions where every branch is reachable, and
re-asserted over the full table in the integration suite.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from cascade.corpus.normalize import (
    MAX_HAMMING,
    MIN_BODY_CHARS,
    DedupeIndex,
    deduplicate,
    sanitize,
    validate,
)
from cascade.corpus.schema import WINDOW_END, WINDOW_START, RawDocument
from cascade.corpus.simhash import hamming, simhash, to_signed

NOW = datetime(2026, 9, 1, tzinfo=UTC)
BODY = "The regulator opened a review of the proposed transaction. " * 8


def raw(**overrides: object) -> RawDocument:
    base: dict[str, object] = {
        "source": "ccnews",
        "source_ref": "ref-1",
        "body": BODY,
        "published_at": datetime(2024, 1, 1, tzinfo=UTC),
    }
    base.update(overrides)
    return RawDocument(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# date-validate: the corpus half of the time lock
# ---------------------------------------------------------------------------


def test_a_well_formed_document_is_accepted() -> None:
    result = validate(raw(), now=NOW)
    assert not isinstance(result, str)
    assert result.published_at.tzinfo is not None
    assert result.document_id == "ccnews:ref-1"


def test_a_missing_date_is_dropped() -> None:
    """A document with no date cannot be placed relative to a cutoff."""
    assert validate(raw(published_at=None), now=NOW) == "missing_date"


def test_a_naive_date_is_dropped_not_assumed_utc() -> None:
    """Assuming an offset invents an instant.

    An hour on the wrong side of a cutoff is a leak, and there is no way to
    tell afterwards which documents were guessed at.
    """
    assert validate(raw(published_at=datetime(2024, 1, 1)), now=NOW) == "naive_date"


def test_a_future_date_is_dropped() -> None:
    assert validate(raw(published_at=NOW + timedelta(days=30)), now=NOW) == "future_date"


def test_small_clock_skew_is_tolerated() -> None:
    """A publisher a few minutes ahead of our clock is not a leakage risk."""
    result = validate(raw(published_at=NOW + timedelta(minutes=30)), now=NOW)
    assert not isinstance(result, str)


def test_a_date_outside_the_corpus_window_is_dropped() -> None:
    """CC-NEWS re-crawls archives, so a 2016 crawl can carry a 2013 article.

    There is no partition for it (deliberately -- a DEFAULT partition could
    never be pruned), so it is out of scope rather than an error.
    """
    before = WINDOW_START - timedelta(days=1)
    assert validate(raw(published_at=before), now=NOW) == "out_of_window"


def test_the_window_matches_the_partitions_the_ddl_creates() -> None:
    """If these drift, a valid document lands in a table with no partition."""
    ddl = (
        __import__("pathlib").Path(__file__).resolve().parents[2] / "migrations" / "003_corpus.sql"
    ).read_text(encoding="utf-8")
    assert f"date '{WINDOW_START.date()}'" in ddl
    assert f"date '{WINDOW_END.date()}'" in ddl


@pytest.mark.parametrize("body", ["", "   ", "\n\n"])
def test_an_empty_body_is_dropped(body: str) -> None:
    assert validate(raw(body=body), now=NOW) == "empty_body"


def test_a_stub_body_is_dropped() -> None:
    """Below the floor a "document" is a headline or a nav fragment."""
    assert validate(raw(body="x" * (MIN_BODY_CHARS - 1)), now=NOW) == "too_short"


def test_a_naive_now_is_refused() -> None:
    """A naive clock cannot order events, so it must not be usable here."""
    with pytest.raises(ValueError, match="timezone-aware"):
        validate(raw(), now=datetime(2026, 1, 1))


def test_validation_does_not_read_the_clock() -> None:
    """``now`` is a parameter, so the same input always gives the same answer.

    A future-date check that consulted the wall clock would make a corpus
    built at 09:00 differ from the same corpus rebuilt at 17:00.
    """
    document = raw(published_at=datetime(2025, 6, 1, tzinfo=UTC))
    early = validate(document, now=datetime(2025, 5, 1, tzinfo=UTC))
    late = validate(document, now=datetime(2026, 1, 1, tzinfo=UTC))
    assert early == "future_date"
    assert not isinstance(late, str)


def test_control_characters_are_stripped() -> None:
    """NUL aborts a COPY of the whole batch, not just the offending row."""
    assert sanitize("a\x00b\x07c") == "abc"
    assert sanitize("keep\ttabs\nand\nnewlines") == "keep\ttabs\nand\nnewlines"
    result = validate(raw(body=BODY + "\x00"), now=NOW)
    assert not isinstance(result, str)
    assert "\x00" not in result.body


# ---------------------------------------------------------------------------
# dedupe: earliest-date retention
# ---------------------------------------------------------------------------


def dated(ref: str, body: str, day: int):  # type: ignore[no-untyped-def]
    result = validate(
        raw(source_ref=ref, body=body, published_at=datetime(2024, 1, day, tzinfo=UTC)), now=NOW
    )
    assert not isinstance(result, str), result
    return result


def test_near_duplicates_collapse_to_one() -> None:
    """Wire copy is syndicated within minutes; each copy is not new evidence."""
    result = deduplicate([dated("a", BODY, 5), dated("b", BODY + " ", 6)])
    assert len(result.kept) == 1
    assert result.collapsed == 1


def test_unrelated_documents_do_not_collapse() -> None:
    other = "Transfer season opened with three clubs bidding for the striker. " * 8
    result = deduplicate([dated("a", BODY, 5), dated("b", other, 6)])
    assert len(result.kept) == 2
    assert result.collapsed == 0


def test_dedupe_keeps_the_earliest_date_whatever_the_arrival_order() -> None:
    """M2 acceptance: earliest-date retention, on a hand-built fixture.

    Keeping a later copy would date the evidence after it actually became
    public -- a cutoff error in the unsafe direction.
    """
    later_first = deduplicate([dated("a", BODY, 9), dated("b", BODY + " ", 3)])
    earlier_first = deduplicate([dated("b", BODY + " ", 3), dated("a", BODY, 9)])
    assert later_first.kept[0].published_at.day == 3
    assert earlier_first.kept[0].published_at.day == 3


def test_collapse_ratio_is_reported() -> None:
    documents = [dated(f"d{index}", BODY, 5) for index in range(4)]
    result = deduplicate(documents)
    assert result.collapsed == 3
    assert result.collapse_ratio == pytest.approx(0.75)


def test_dedupe_carries_across_batches_through_a_shared_index() -> None:
    """A story syndicated into two months must still collapse."""
    index = DedupeIndex()
    first = deduplicate([dated("a", BODY, 5)], index=index)
    second = deduplicate([dated("b", BODY + " ", 6)], index=index)
    assert len(first.kept) == 1
    assert second.kept == []
    assert second.collapsed == 1


def test_a_seeded_index_suppresses_already_stored_documents() -> None:
    """On resume, documents already in the corpus must not be re-admitted."""
    index = DedupeIndex()
    index.seed([simhash(BODY)])
    result = deduplicate([dated("a", BODY, 5)], index=index)
    assert result.kept == []
    assert result.collapsed == 1


def test_dedupe_is_order_deterministic() -> None:
    documents = [
        dated(f"d{index}", f"Distinct article number {index}. " * 20, 5) for index in range(20)
    ]
    first = [document.document_id for document in deduplicate(documents).kept]
    second = [document.document_id for document in deduplicate(documents).kept]
    assert first == second


# ---------------------------------------------------------------------------
# SimHash
# ---------------------------------------------------------------------------


def test_identical_text_hashes_identically() -> None:
    assert simhash(BODY) == simhash(BODY)


def test_hash_is_stable_across_processes() -> None:
    """blake2b, not ``hash()``: the built-in is salted per process.

    A per-process salt would refingerprint every document on every run and
    dedupe would silently stop working across a resume.
    """
    import subprocess
    import sys

    out = subprocess.run(
        [
            sys.executable,
            "-c",
            "from cascade.corpus.simhash import simhash; print(simhash('stable text here'))",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert int(out.stdout.strip()) == simhash("stable text here")


def test_minor_edits_stay_within_the_collapse_threshold() -> None:
    assert hamming(simhash(BODY), simhash(BODY.replace(".", ". "))) <= MAX_HAMMING


def test_signed_conversion_round_trips_for_postgres() -> None:
    """Postgres has no unsigned bigint; the bit pattern must survive."""
    from cascade.corpus.simhash import from_signed

    for text in (BODY, "another", "éè"):
        value = simhash(text)
        assert from_signed(to_signed(value)) == value


def test_empty_text_hashes_to_zero_rather_than_raising() -> None:
    assert simhash("") == 0


def test_timezone_representation_does_not_affect_validation() -> None:
    shifted = datetime(2024, 1, 1, 12, tzinfo=timezone(timedelta(hours=-5)))
    result = validate(raw(published_at=shifted), now=NOW)
    assert not isinstance(result, str)
    assert result.published_at == shifted


# ---------------------------------------------------------------------------
# When the stored text was knowable (ADR-0044)
# ---------------------------------------------------------------------------


def test_a_page_fetched_after_the_date_it_states_is_dated_by_the_fetch() -> None:
    """A 2025 article re-crawled in 2026 carries the 2026 page -- update notes,
    sidebars. Its text was knowable in 2026, whatever the article says."""
    stated = datetime(2025, 8, 23, 18, 31, tzinfo=UTC)
    crawled = datetime(2026, 4, 9, 2, 0, tzinfo=UTC)
    result = validate(raw(published_at=stated, crawled_at=crawled), now=NOW + timedelta(days=900))
    assert not isinstance(result, str)
    assert result.published_at == crawled
    assert (result.stated_published_at, result.crawled_at) == (stated, crawled)


def test_a_stated_date_after_the_fetch_is_kept() -> None:
    """Metadata can claim a later instant than the fetch (a scheduled story, a
    timezone mislabel). The later of the two is still the safe one."""
    stated = datetime(2024, 1, 1, 12, tzinfo=UTC)
    crawled = datetime(2024, 1, 1, 9, tzinfo=UTC)
    result = validate(raw(published_at=stated, crawled_at=crawled), now=NOW)
    assert not isinstance(result, str)
    assert result.published_at == stated


def test_without_a_fetch_time_the_stated_date_stands() -> None:
    result = validate(raw(), now=NOW)
    assert not isinstance(result, str)
    assert result.published_at == datetime(2024, 1, 1, tzinfo=UTC)
    assert result.crawled_at is None


def test_a_naive_fetch_time_is_refused_not_assumed() -> None:
    naive = datetime(2024, 1, 2, 9)
    assert validate(raw(crawled_at=naive), now=NOW) == "naive_date"


def test_the_earlier_copy_keeps_its_own_text_not_only_its_date() -> None:
    """When the earlier copy of a syndicated story arrives second, the survivor
    must be that copy -- text and date together. Moving only the date back
    paired a later-fetched text with an earlier date."""
    early = dated("early", BODY + " early", 3)
    late = dated("late", BODY + " later update", 9)
    kept = deduplicate([late, early]).kept
    assert len(kept) == 1
    assert kept[0].document_id == early.document_id
    assert kept[0].body == early.body
    assert kept[0].published_at == early.published_at
