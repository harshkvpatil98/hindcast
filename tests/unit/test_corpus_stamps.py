"""The update-stamp detector (ADR-0044)."""

from __future__ import annotations

from datetime import UTC, datetime

from cascade.corpus.stamps import stamps_after, update_stamps


def test_the_stamps_that_found_the_leaks_are_read() -> None:
    """Verbatim from stored chunks: a 2025-dated CC-NEWS page and a 2025
    Wikipedia render, both carrying 2026 stamps."""
    assert update_stamps("Updated: January 20, 2026 at 3:56 AM") == [
        datetime(2026, 1, 20, tzinfo=UTC)
    ]
    assert update_stamps("Updated: Apr 08, 2026, 11:14 IST") == [datetime(2026, 4, 8, tzinfo=UTC)]
    assert update_stamps("Updated: August 26, 2026.") == [datetime(2026, 8, 26, tzinfo=UTC)]
    assert update_stamps("Last updated on 3 March 2026") == [datetime(2026, 3, 3, tzinfo=UTC)]
    assert update_stamps("Modified: 2026-02-01T10:00") == [datetime(2026, 2, 1, tzinfo=UTC)]


def test_a_future_date_in_prose_is_not_a_stamp() -> None:
    """The scheduled vote is evidence; only the page's own stamp says when the
    text was written."""
    assert update_stamps("The vote is set for March 5, 2026, officials said.") == []


def test_only_stamps_past_the_tolerance_are_flagged() -> None:
    published = datetime(2025, 12, 1, 18, tzinfo=UTC)
    text = "Updated: December 2, 2025 ... Updated: January 20, 2026"
    assert stamps_after(text, published) == [datetime(2026, 1, 20, tzinfo=UTC)]
    assert stamps_after("Updated: December 1, 2025", published) == []


def test_nonsense_is_skipped_not_raised() -> None:
    assert update_stamps("Updated: Smarch 40, 2026 and Updated: 2026-13-45") == []
