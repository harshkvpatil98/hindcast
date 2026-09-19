# ADR-0043 — Placeholder legs: excluded from scoring, the sealed set kept

- **Status:** accepted (2026-09-19). The owner chose to **keep the sealed set
  and exclude the stand-ins from scoring** rather than reseal; see "Decision".
- **Milestone:** M14 (corrects M1 code)
- **Corrects a defect found in this build** — by the market benchmark
  (ADR-0039), not by review

## Context

Fetching each market's price at its cutoff found 21 Polymarket scenarios with
no trading at all in the fortnight before the cutoff. Reading them: most are
not questions. Exchanges pre-list the legs of an open-ended event before the
names are known — *"Will Candidate B win the 2026 Busan Mayoral Election?"*,
*"Will Company K be the largest company in the world by market cap on
June 30?"*, *"Will Placeholder V be the #1 searched song on Google this
year?"* — and those legs resolve, always NO unless renamed, so they passed
every resolution rule. **15 of the 180 sealed scenarios are stand-ins** by the
rule below.

Three defects let them in, each small:

1. **Volume.** The screen read `market.volumeNum or event.volume`. A leg's
   honest `0` is falsy, so every untraded leg of a busy event inherited the
   event's volume and cleared the $5,000 floor. The floor never applied to a
   leg.
2. **Representative choice.** One scenario per event (M1), chosen by keyed hash
   among the resolved legs — correctly outcome-independent, and in a 60-leg
   event with placeholders it picks a placeholder about as often as the
   placeholders are common.
3. **Creation date.** A leg added to a running event inherits the event's
   `startDate`. The cutoff is a fraction of the way through the question's
   life, so one market (an ETF approval) had a cutoff four months before the
   market existed.

Separately, re-reading the set for this found that **every one of the 11
scenarios filed under `health` was misfiled** — Survivor contestants, an F1
constructors' title, NBA seeding. The classifier ran over question *and*
resolution criterion, case-insensitively, and matched `WHO` against the pronoun
in "the player who is selected". The domain feeds the ≤ 25% cap, the per-domain
report table and the dev/test stratification, so a label that tracks
boilerplate defeats all three.

## Decision

Two options were measured and put to the owner: reseal the registry with the
fixes below (40 of 180 scenarios change, and `make demo`'s stored events must be
deleted because they reference scenarios that leave), or keep the sealed set and
exclude the stand-ins from every scored figure. **The owner chose the second.**

1. **The sealed registry does not move.** Manifest `91ccd314…`, its YES rate,
   its domain labels and its cutoffs are the study's. The ledger code on this
   branch reproduces it byte for byte from the source cache (checked: a dry
   rebuild gives the same manifest with 0 fetches).
2. **Fifteen stand-ins are excluded from scoring and counted.**
   `cascade/eval/exclusions.py::is_placeholder` — "placeholder", or a role noun
   (candidate, company, person, player, team, …) followed by one or two capital
   letters, case-sensitive on the letters so "Team USA" and "Company 3M" are
   names. It reads the question's wording and nothing else. Stand-ins almost
   always resolve NO, so a rule that could see outcomes would look the same on
   the data; this one cannot see them, by signature.
3. **The exclusion happens before the split is drawn, and is inside its pin.**
   `declare_study_split` removes the stand-ins, then splits the remaining 165:
   **40 dev / 125 test**, so dev is 40 real questions. The excluded ids and
   their reason are in the fingerprint (`eval.split_sha256` = `4914fba3…`), so
   widening the rule later — to drop one more scenario that went badly — is
   refused like any re-drawn split. The first pin (`4b8dad2f…`, over all 180)
   was replaced on the same day; no forecast had been scored against either.
4. **Excluded means excluded everywhere.** `select` drops them from `dev`,
   `test` *and* `all`; the tuning guard refuses them; the grid's pools omit them
   (so the A=on and A=off cells draw the same 90); and compile, the dossier
   writer, the baselines, the estimate and the injection probe skip them, so no
   budget is spent on a number the report must discard. The report states the
   count and the reason above the headline, and `manifest.json` lists the ids.

**Kept, as the cost of not resealing:** the 11 `health` scenarios are misfiled
(game shows and sport, matched on the pronoun "who"), which loosens the ≤ 25%
domain cap and makes the per-domain table's `health` row meaningless; and one
scenario (an ETF approval) has a cutoff four months before its market was
created — forecastable from evidence, but with no market price to benchmark.
Both are named in the report's limitations, not fixed.

**The registry fixes are preserved** on branch `m14-registry-v2` (commit
`d5b0ef3`) for the next registry this project seals: leg-level volume (the
`or` that read a zero as missing), the representative leg drawn by keyed hash
among *named* legs — not traded ones, because a leg's final volume accumulates
after the cutoff and tracks the eventual winner — the leg's own `createdAt`,
the domain read from the question, `WHO` case-sensitive, "strike on" as
conflict, and "O/U" props as single-quantity. 14 of 14 seeded mutants killed
there.

## Measured — what the reseal would have done (a dry rebuild, 0 fetches)

| | sealed `91ccd314…` | with this ADR `1ef204c5…` |
|---|---|---|
| scenarios | 180 | 180 |
| stand-ins | **15** | **0** |
| YES rate / max domain share | 0.5000 / 0.2500 | 0.5000 / 0.2500 |
| eligible pool | 1,520 | 1,404 |
| `health` | 11 (all misfiled) | 0 |
| sources (poly / manifold / curated) | 165 / 6 / 9 | 167 / 5 / 8 |

**140 scenarios are shared; 40 leave and 40 enter.** Far more than the 15
stand-ins, because selection is a joint constraint problem — base rate, domain
cap, one per event, keyed-hash order — and a different eligible pool and
different domain labels re-solve it.

## Verification

- 22 tests in `tests/unit/test_eval_exclusions.py`, including a regression pin
  of exactly 15 over the real registry and a static check that every spend
  path filters. The split, CLI and report tests now derive their counts from
  the declaration instead of hard-coding 140/180.
- 10 of 10 seeded mutants killed: the rule's case-sensitivity and letter count
  loosened; the "placeholder" keyword dropped; the exclusion left out of the
  fingerprint; excluded ids kept in `all`; exclusion applied after the split;
  the CLI declaring the plain split; the tuning guard ignoring excluded ids;
  the report and the manifest silent about them.
