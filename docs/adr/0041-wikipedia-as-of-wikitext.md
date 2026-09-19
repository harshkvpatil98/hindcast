# ADR-0041 — Wikipedia as it read before the cutoff: chosen from the question, stored as raw wikitext

- **Status:** accepted (2026-09-19). Amends ADR-0010's Wikipedia anchoring.
- **Milestone:** M14 (an M2 mechanism)
- **Corrects a defect found in this build** — a time-lock leak the chunk
  dates could not show

## Context

CC-NEWS is a random slice of the world's news. Wikipedia, read as it stood
before a scenario's cutoff, is the one source *about the question by
construction*: the article on the merger, the day before the cutoff, is often
the best single briefing that exists. The previous adapter held 14,380 chunks
of it against a corpus of ~2M.

A dry run of that adapter over all 180 scenarios found it mostly fetching the
wrong things, and one thing it must never fetch.

- **Titles.** It used the first six `party_names` as literal page titles and
  nothing from the question. Of 735 titles requested, 163 had no page, 140
  were redirects it never followed ("NFL", "FOMC"), and 143 were
  disambiguation pages it stored as evidence ("Other" ×24, "Any" ×13).
- **The leak.** It obtained an old revision's text by *rendering* it
  (`action=parse&oldid=…`), and a render expands today's templates. The
  2024–25 Houston Rockets season article as of 2025-02-14 — three days before
  that scenario's cutoff, when the team stood at 34–21 — rendered
  "Updated: August 26, 2026" and the final 52–30 standings. The document's
  `published_at` was the revision's timestamp, correctly before the cutoff, so
  Chronofence served it and every leakage test passed: **the date was honest
  and the text was not.**
- **The boundary.** `rvstart` is inclusive (measured), so a revision saved at
  exactly the cutoff was eligible.

## Decision

- **Text is the revision's own wikitext**, converted to prose locally;
  templates are dropped, never expanded. Nothing is rendered.
- **The as-of lookup anchors one second before the cutoff** and re-checks the
  result with `>=`; there is no fallback to a page's first revision, so a page
  created after the cutoff yields nothing.
- **Redirects are followed as they read at the cutoff**, and when a title held
  nothing then, the move log finds the page that was there — read before the
  cutoff, filed under the old title. Disambiguation pages are followed only
  where, at the cutoff, they named a primary meaning the scenario's own words
  confirm.
- **Titles come from the question first** (its dated event, its names, their
  season articles), then the registry's parties with noise removed; a linked
  page is kept only if its as-of lead names something the question names.
  **Depth 2** follows links in the question's main articles' as-of leads.
- **Depth is in the unit key** (`d{depth}.{cutoff}.{scenario}`, ADR-0023's
  lesson): a deeper pass adds keys, and the old adapter's `done` rows (bare
  scenario ids) match nothing, so they are neither re-run nor in the way. New
  documents carry `source_ref = rev:{id}`.
- **Stand-ins excluded from scoring get no units** (ADR-0043).
- Politeness: 2 requests/second, `maxlag` always sent, a maxlag or rate-limit
  refusal retried, never read as an empty answer.

## Measured (dry run, 165 scored scenarios, nothing stored)

| | before | depth 1 | depth 1+2 (default) |
|---|---|---|---|
| scenarios with ≥ 1 on-topic article | 127 | 147 | **147** |
| the same, stricter test | 94 | 125 | 126 |
| median on-topic articles per scenario | 1 | 3 | **4** |
| articles / text | 400 / 16.0M chars | 552 / 22.4M | 778 / 28.9M |
| as-of revisions over a year before the cutoff | 34 | 3 | 4 |

About 17,400 chunks at 414 tokens (≈ 20k with overlap) — inside the 60,000 the
CC-NEWS ceiling was lowered to reserve. A production pass is ~3,300 revision
lookups, about 40 minutes. 4,715 real requests were made to Wikimedia during
development, with no 429s.

## Consequences

- **The Wikipedia documents already stored must be purged** (`source =
  'wikipedia' AND source_ref NOT LIKE 'rev:%'`): they were built from renders
  and may carry post-cutoff text. Nothing has consumed them — no graph, run or
  forecast exists.
- Page identity uses today's title mapping plus the move log; the text is
  always strictly pre-cutoff, but which page a title resolves to is decided
  now.
- The shared near-duplicate step keeps the earliest near-identical revision,
  so a later scenario can get a staler revision of an article several
  scenarios share; it can never get a later one.
- 18 scored scenarios still have nothing on-topic — mostly token launches and
  unnamed fixtures.
- 38 tests replaying 13 recorded Wikimedia exchanges (any unrecorded request
  fails); 26 of 26 seeded mutants killed, including `>` for `>=` at the cutoff,
  the request anchored at the cutoff itself, a first-revision fallback, the
  body rendered, the post-move title stored, and maxlag read as an answer.
