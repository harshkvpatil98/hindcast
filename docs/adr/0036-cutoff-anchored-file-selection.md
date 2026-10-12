# ADR-0036 — CC-NEWS files are chosen by their distance from each scenario's cutoff

- **Status:** accepted — requested by the project owner, 2026-09-19. **Amends ADR-0023.**
- **Milestone:** M12 (an M2 mechanism)
- **Corrects a defect found in this build**

## Context

ADR-0023 fixed an ingest that walked forward from 2016 and never reached the
years the scenarios are in. Its queue ranks a **month** by *demand* — how many
scenarios have that month anywhere inside their 18-month window — and sweeps
every month once before deepening any. Two things were wrong with that, and
the owner's question found both: *"why are we not storing information from the
latest cutoff, going towards the past?"*

1. **Demand ignores distance.** A month seventeen months before a cutoff counts
   the same as the month of the cutoff. Mid-2025 won, because it is inside
   almost every window — as old background. Measured on the rebuilt corpus, the
   queue put the months before the largest cluster of cutoffs at positions 8
   (2026/04), 14 (2026/05), 19 (2026/06) and 26 (2026/07), behind 2024 and 2023.
2. **"Deepening a month" never moved through the month.** Unit `YYYY/MM#k` is
   the k-th file of the listing, k < 12. A month lists ~400 files in crawl
   order, ~13 a day (measured: 394 in 2026/03, the twelfth crawled on 2 March).
   So however deep, a month contributed its first two days. A scenario cut off
   on the 25th could never be given the 24th.

Coverage still passed — it asks for 200 chunks in 18 months — while 82 of 180
scenarios had under 1,000 chunks from their final thirty days and 7 had none.
The last weeks before a cutoff are the most informative ones for a forecast,
and the gate could not see that they were missing.

## Decision

Files are chosen by the crawl time in their names, relative to each cutoff
(`cascade/corpus/anchored.py`, pure). Two phases:

1. **A floor.** While one file would give at least two scenarios their first
   evidence from their final 30 days, take the file that rescues the most.
2. **Closeness.** Greedy on total utility: a file is worth
   `0.5 ** (days_before_cutoff / 14)` to each scenario whose cutoff it
   precedes; a scenario's utility is concave in its evidence (isoelastic,
   equity 2); **every scenario counts equally**, being 1/180 of the Brier.

Files already ingested count as evidence held, so a re-plan after an
interruption continues the allocation (invariant 8). `corpus.ccnews_planning:
sweep` keeps ADR-0023's queue available; `anchored` is the default.

**The plan is a function of cutoffs and listings alone — never of an outcome**
(precedents: ADR-0010, ADR-0023). Its parameters were chosen by looking at how
evidence is distributed over scenarios, before any forecast exists. They are
not to be tuned against one: choosing evidence by what improved the score is
steering (§1).

## Measured

Real cutoffs, real listings (57 months, 27,608 files), the 18 files left in the
owner's 2.0M-chunk budget:

| Scenarios whose nearest file is… | now | ADR-0023 queue | this plan |
|---|---|---|---|
| within 1 day of cutoff | 3 | 4 | **34** |
| within 7 days | 30 | 42 | **91** |
| within 30 days | 102 | 164 | **170** |
| *not* within 30 days | 78 | 16 | **10** |
| median gap | 25.5 d | 15.9 d | **6.9 d** |

It also wins at budgets of 14 and 24 files, so the result does not hang on how
many files fit under the ceiling.

## What the measurements corrected along the way

- **The first weighting was wrong.** A hyperbolic `1/(1+d/14)` has so heavy a
  tail — 13% at ninety days — that fifteen stale files summed to "well served",
  and the first plan skipped April–July 2026 entirely: a planner built for
  recency avoiding the most recent months. A true half-life fixed it.
- **Closeness alone lost on breadth**: 24 scenarios with nothing in their final
  30 days against the old queue's 16. The floor phase fixed that; with it the
  plan wins on every row.
- **The floor stops at two on purpose.** Ten files rescue 67 scenarios (23, 9,
  8, 7, 5, 4, 3, 3, 3, 2). The next eleven would rescue one each — the
  early-cutoff scenarios alone in their months — at over half the remaining
  budget for 6% of the study. They are not abandoned: phase 2 takes a lone
  scenario's file once that beats serving a cluster again. **This is the cost
  of the budget, not of the rule**: with more than 2.0M chunks, those are the
  files the plan spends the extra on.

## Cost of being wrong

If the final weeks are *not* the most informative evidence, the old queue's
broader background would have served better — but that is a claim about
forecasting nobody has measured here, in either direction. What is measurable
is the evidence distribution, and `cascade corpus coverage` still gates the
corpus. If file names stop carrying crawl times, `parse_listing` raises rather
than guessing.

## Verified by

`tests/unit/test_corpus_anchored.py` (16 tests) and
`tests/unit/test_corpus_pipeline_stops.py` (the plan's order is the ingest's
order; the sweep needs no listings; an unfetchable listing fails the source
loudly). Seven deliberately broken behaviours were each caught — two only after
tests were added for them.
