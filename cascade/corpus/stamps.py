"""Update stamps later than the document they sit in (ADR-0044).

The time lock checks that a document's date precedes the cutoff; nothing
checked that the date is *true* of the text. Two leaks in one day passed every
leakage test that way -- Wikipedia revisions rendered through today's
templates (ADR-0041) and CC-NEWS pages dated by what they state while carrying
a later fetch (ADR-0044) -- and both were found by the same thing: a page's own
"Updated: <date>" stamp, later than the document's date.

That is what this module detects. Only update stamps count. A future date in
prose ("the vote on 5 March 2026") is the scheduled-event evidence a forecaster
needs and says nothing about when the text was written; "Updated: 20 January
2026" is the page saying when it was.

Pure: no I/O, no clock.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

__all__ = ["STAMP_TOLERANCE", "stamps_after", "update_stamps"]

_MONTHS = {
    name: index
    for index, names in enumerate(
        (
            ("jan", "january"),
            ("feb", "february"),
            ("mar", "march"),
            ("apr", "april"),
            ("may",),
            ("jun", "june"),
            ("jul", "july"),
            ("aug", "august"),
            ("sep", "sept", "september"),
            ("oct", "october"),
            ("nov", "november"),
            ("dec", "december"),
        ),
        start=1,
    )
    for name in names
}
_MONTH = r"(?P<month>[A-Za-z]{3,9})\.?"
_PREFIX = r"(?:last\s+)?(?:updated|modified)(?:\s+on)?\s*:?\s*"
_STAMP = re.compile(
    _PREFIX
    + r"(?:"
    + _MONTH
    + r"\s+(?P<day>\d{1,2}),?\s+(?P<year>20\d\d)"
    + r"|(?P<day2>\d{1,2})\s+"
    + _MONTH.replace("month", "month2")
    + r",?\s+(?P<year2>20\d\d)"
    + r"|(?P<iso>20\d\d-\d\d-\d\d)"
    + r")",
    re.IGNORECASE,
)

# Timezones: a stamp carries a local date, the document a UTC instant. A day
# either side absorbs every offset; beyond that the stamp is later text.
STAMP_TOLERANCE = timedelta(days=1)


def update_stamps(text: str) -> list[datetime]:
    """Every parseable "Updated/Modified: <date>" in ``text``, as UTC midnight.

    Preserves precision in the conservative direction: a stamp is read as the
    start of its day, so it can only look *earlier* than it was, and a flag it
    raises is never an artefact of rounding up.
    """
    found: list[datetime] = []
    for match in _STAMP.finditer(text):
        try:
            if match.group("iso"):
                parsed = datetime.fromisoformat(match.group("iso")).replace(tzinfo=UTC)
            else:
                month_name = (match.group("month") or match.group("month2") or "").lower()
                month = _MONTHS.get(month_name)
                if month is None:
                    continue
                day = int(match.group("day") or match.group("day2"))
                year = int(match.group("year") or match.group("year2"))
                parsed = datetime(year, month, day, tzinfo=UTC)
        except ValueError:
            continue
        found.append(parsed)
    return found


def stamps_after(text: str, published_at: datetime) -> list[datetime]:
    """The update stamps in ``text`` later than ``published_at`` allows.

    Preserves the time lock's meaning: a document may not carry text written
    after the instant it is dated. Any stamp more than :data:`STAMP_TOLERANCE`
    past ``published_at`` is evidence that it does.
    """
    limit = published_at + STAMP_TOLERANCE
    return [stamp for stamp in update_stamps(text) if stamp > limit]
