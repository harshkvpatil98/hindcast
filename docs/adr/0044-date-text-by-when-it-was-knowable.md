# ADR-0044 — A document is dated by when its text was knowable, not by the date it states

- **Status:** accepted (2026-09-19). Corrects ADR-0010's CC-NEWS rule.
- **Milestone:** M14 (an M2 mechanism)
- **Corrects a defect found in this build** — the second time-lock leak in one
  day whose dates looked correct (the first is ADR-0041)

## Context

The time lock filters on `published_at < as_of`, and every leakage test
checks exactly that. Both hold if the date is honest. ADR-0010 dated each
CC-NEWS page by the publication date the page states about itself, and
`ccnews.py` argued that the crawl time (`WARC-Date`) would be *unsafe*: "an
article would be dated later than it was knowable". That is backwards. A later
date removes a document from more cutoffs and can never leak. What leaks is an
*earlier* date on *later* text — and the text stored is the text Common Crawl
fetched, which is not the text as first published when the page changed in
between.

Found while auditing the Wikipedia purge: 3 CC-NEWS chunks dated in 2025 read
"Updated: January 20, 2026" and "Updated: Apr 08, 2026". Then measured on the
first 3,000 records of one 2026-03 CC-NEWS file (2,630 with a usable date):

| stated date → fetch | pages |
|---|---|
| median gap | 0.05 days (~1.2 h) |
| over 1 day | 253 (9.6%) |
| over 7 days | 97 (3.7%) |
| **over 180 days** | **96 (3.7%)** — re-crawls of old articles |
| stated *after* the fetch | 40 (1.5%) |

A 2023 article re-crawled in 2026 carries 2026 update notes and, through the
page's own chrome, 2026 headlines, and it was dated 2023 — served to every
scenario with a cutoff in between. The ADR-0036 planner reads 2026 files
heavily, so the most recent scenarios were the most exposed.

The same pairing hid in near-duplicate collapse: when the earlier copy of a
syndicated story arrived second, only its *date* was moved onto the surviving
copy, pairing a later-fetched text with an earlier date.

## Decision

- **`published_at` is when the stored text was knowable:** `max(stated,
  fetched)`. For CC-NEWS the fetch is the record's `WARC-Date`. A page that
  states no date is still dropped — the crawl is a bound on the text, not a
  publication date. Sources whose text is fixed at its date (a Wikipedia
  revision's own wikitext, ADR-0041; a Federal Register document) have no fetch
  bound and keep their date.
- **Provenance is kept:** migration 020 adds `stated_published_at` and
  `crawled_at` to `documents`, so a reader can see which rule set each date and
  by how much.
- **Near-duplicate collapse keeps the earlier copy whole** — text and date
  together.
- **The corpus already stored is repaired, not rebuilt:** `cascade corpus
  redate` re-reads the WARC headers of every finished CC-NEWS unit (no
  chunking, no embedding), moves each stored document and its chunks to
  `max(stated, fetched)` with `GREATEST`, so the pass is order-independent and
  idempotent, and records each file so it resumes. Documents no finished file
  holds came from interrupted units whose fetch time cannot be known; they are
  deleted (`--delete-orphans`, only once every file is done) and re-fetched
  under the new rule when the ingest resumes.

## Cost

Recency: a fresh article is available from its fetch rather than from its
stated minute — about an hour at the median. The ADR-0036 planner already
chose files by their crawl time, so its choices now mean what it assumed they
meant.

## Verification

- Unit tests for the rule (fetch later, stated later, neither, naive fetch),
  the WARC-Date parse, a WARC record end to end through the streaming parser,
  and the coherent dedupe; integration tests on a scratch PostgreSQL 16 that a
  2025 document and its chunks move together into `chunks_2026q1`, the stated
  date survives, a second pass changes nothing, and no date moves earlier.
- 10 of 10 seeded mutants killed, including the fetch ignored, `min` for
  `max`, the dedupe moving only the date, the repair moving dates earlier or
  leaving chunks behind, and the repair keying ids differently from the ingest
  (which would have deleted the corpus as orphans).
- The poison-pill and date-monotonicity suites could not have caught either
  leak: they test that the date is respected, not that it is true. What would
  catch it is a scan for text dated after its document — a page's own
  "Updated: <date>" stamp found both. That scan is now `cascade/corpus/stamps.py`
  and the leakage probe `tests/leakage/test_update_stamps.py`: only update
  stamps count, never a future date in prose, which is scheduled-event evidence.
  Measured before the repair: **381 chunks** carried a stamp later than their
  date (the largest gap 1,383 days). After it: **4**, and all four are stamps
  later than the moment the page was fetched — publisher typos ("Published
  Dec 31, 2025" on a page fetched 2025-01-01) that the fetched text cannot
  have. Where the fetch time is known it bounds every real stamp, and the probe
  says so.

## The repair, measured (2026-09-19)

All 29 finished files re-read. Of the **204,355** CC-NEWS documents they hold,
**200,122 moved to their fetch time** (median move 1.5 hours). **11,237 (5.5%)
had been fetched more than 180 days after the date they state**, 14,124 more
than 30 days, 26,247 more than a day. 5,000 documents (29,878 chunks) from
interrupted units matched no finished file and were deleted; their units
re-fetch them under the new rule. Common
Crawl answered HTTP 503 intermittently throughout; the pass resumed from its
per-file records each time.
