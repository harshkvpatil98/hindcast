# ADR-0043 — Placeholder legs, leg-level volume, and domain from the question

- **Status:** accepted as code; **the reseal it implies awaits the owner's
  decision** (2026-09-19)
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

- **A stand-in is not a party.** `is_placeholder`: "placeholder", or a role
  noun (candidate, company, person, player, team, …) followed by one or two
  capital letters. Case-sensitive on the letters, so "Company 3M" and "Team
  USA" are names. Reads the question text and nothing else.
- **The representative is chosen by keyed hash among named legs.** Not among
  traded ones: a leg's final volume accumulates after the cutoff and tracks the
  eventual winner, so preferring traded legs would let the outcome lean on the
  selection. Volume stays a screen on the chosen leg, now on its own figure.
  An event of nothing but stand-ins still yields a candidate so the screen
  rejects and counts it (`placeholder_leg`).
- **A leg's life starts at its own `createdAt`** when that is later than the
  event's start.
- **Domain from the question**, the criterion consulted only when the question
  alone matches nothing; `WHO` only in capitals; "strike on / attack on" is
  conflict, not labour; player props ("Points O/U 16.5") are single-quantity.

All of these read fields fixed before resolution. None reads an outcome.

## Measured — a dry rebuild from the recorded source cache (0 fetches)

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

## The reseal — not done, and why it is the owner's call

Writing this registry is a new frozen split. It is legitimate now and only now:
no forecast exists, so nothing can have been chosen after looking. Against it:

- `ledger build --write --replace` deletes the scenarios that leave, and
  `make demo`'s stored runs, forecasts and **452,374 events** reference some of
  them. The demo data is heuristic and regenerates in ~90 s, but clearing it
  is a `DELETE` on the append-only event log, and invariant 6 says never.
- The CC-NEWS ingest running now is anchored on the current cutoffs
  (ADR-0036); 40 new scenarios would want it re-planned (a restart of that step
  — completed units are kept).
- The dev/test pin, the market prices and the memorisation probe are all per
  scenario and cheap to redo, but they must be redone.

Not resealing leaves 15 stand-ins (8%) in the headline, a `health` row made of
game shows, and one scenario forecast from before its market existed.
