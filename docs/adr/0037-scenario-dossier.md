# ADR-0037 — A cited, machine-checked situation report per scenario

- **Status:** accepted as a mechanism, **off by default**; whether the headline
  configuration uses it is decided on the development partition by the rule
  below, written before any forecast existed (2026-09-19).
- **Milestone:** M14 (touches M4's compiler prompt and M5's agent prefix)
- **Requested by the project owner:** *"why each scenario gets only 150 chunks,
  why not more if we have it"*

## Context

The corpus holds a median of tens of thousands of admissible chunks per
scenario. The system reads almost none of them. The compiler sees the 60
chunks nearest the question text; each actor sees six, retrieved once for its
own query (ADR-0019). A scenario with ten thousand chunks from its final month
and one with sixty put the same amount of evidence in front of the model. The
owner's question was why, and the honest answer was: prompt budget, and nothing
else. Every agent call carries its evidence in the cached prefix, so tripling
the chunks triples a per-call cost that is paid half a million times.

The way to read a hundred documents without carrying a hundred documents is to
read them **once** and carry what they say. That is a summary, and a summary
written by a model is where this gets dangerous.

**The laundering channel.** The writer model has parametric knowledge of many
of these situations, for some of them including how they ended. The agents'
own parametric knowledge is already a known risk, and it is *measured*: the
memorisation probe asks each question with no context and reports the
confidence. A free-text summary would open a second, unmeasured channel —
the writer's memory of the outcome, reaching every agent under the heading
"what was known before the cutoff". A summary that says *"regulators, who
ultimately blocked the deal, opened a review in March"* leaks the label through
the one door the grants cannot guard, because it arrives as evidence.

## Decision

**1. The dossier is structured claims with citations, not prose.** One
compiler-model call per scenario reads a pool of up to 100 pre-cutoff excerpts
and emits four sections — a dated timeline, stated positions, the state of
play at the cutoff, and what the record leaves open — as one-sentence claims,
each citing the numbered excerpts that state it. The tool schema is generated
from the Pydantic model, as the compiler's are.

**2. A model-free check decides what survives** (`dossier.verify`, pure). A
claim is refused unless:

- every citation resolves to an excerpt that was actually offered;
- a timeline entry carries a date strictly before the cutoff's own day;
- **every number and every proper name in the claim occurs in the cited
  excerpts** — not the pool, the *cited* excerpts — or in the documents' own
  publication dates, or in the scenario's own wording;
- at least `min_support_ratio` (0.6) of its content words do too.

The excerpts come through Chronofence, so they are pre-cutoff by construction,
and both `pool_evidence` and `verify` re-assert `published_at < as_of` and
**raise** rather than filter: a post-cutoff excerpt is a retrieval defect, and
filtering would hide it. A scheduled future event ("a hearing is set for 20
April") is reportable under *status* when a pre-cutoff excerpt announces it —
that is knowledge a forecaster at the cutoff had — and never as a timeline
entry.

The rules are lexical, and deliberately conservative in one direction: a true
paraphrase can be dropped; an unsupported name or number cannot pass.

**3. Refused claims are stored and counted, never discarded.** The rate at
which the writer asserts what its sources do not say is the dossier's own
integrity figure. `cascade compile dossier` prints it by reason, and it is
measurable with no forecast and no label.

**4. The dossier is evidence, so it follows factor C — and reaches every arm
that gets evidence.** The compiler's draft prompt, every agent's cached prefix,
the A=off panel arm, **and both single-model baselines**. Were the baselines
left without it, "Cascade beats a single model" would partly mean "a model
given a briefing beats one that was not". A `parametric_only` cell gets no
dossier, exactly as it gets no chunks, and does not read the table.

**5. All or nothing.** With `dossier.enabled` set, a scenario without a stored
dossier is exit 3 in every phase that builds a prompt — never an empty report.
A run that mixed briefed and unbriefed scenarios would produce a headline over
a mixture nobody configured.

**6. Off means byte-identical.** With the dossier off, the draft prompt, the
persona block and the baseline prompt are the same bytes they were before this
ADR (asserted), so existing recordings keep resolving and the on/off comparison
has exactly two arms. Turning it on requires `llm.prompt_rev >= r3`, enforced
by `Settings`; migration 019 records r3 for both subsystems in
`prompt_revisions`, because an audit trail that depends on remembering is not
one.

**7. Provenance.** `scenario_dossiers` stores the canonical JSON, its SHA-256
and the evidence pool each citation indexes into; `cascade compile verify`
re-hashes every row. `causal_graphs.dossier_sha256` records which report a
graph was compiled against. `cascade_sim` may read dossiers and cannot write
them: the simulation does not author its own evidence.

## The decision rule, fixed in advance

Whether the headline configuration reads the dossier is an empirical question
and the test partition may not be used to answer it. On the **development
partition only** (ADR-0038): compile and run the headline configuration with
the dossier off and on, and adopt it if the paired mean Brier difference
(on − off) is below zero. The interval is reported beside the decision; with
~40 scenarios it will be wide, and the rule is a point-estimate rule on purpose
— a threshold chosen after seeing the interval would be a second decision made
on the same data. Whichever way it goes, the other arm is reported as a
supplementary comparison on the test partition, in the supplementary Holm
family, so the choice is auditable against held-out data.

## What this does not close

Lexical support does not read meaning. A hindsight-coloured *verb* over
supported nouns — "Acme **abandoned** its bid" where the excerpt says Acme
"was reviewing" it — is caught only if enough of the sentence's other content
is also foreign to the excerpt. Two measurements bound what remains:

- **The refusal rate split by whether the scenario resolved before the writer
  model's training cutoff.** A writer leaking memory attempts more unsupported
  claims where it has memory to leak. No labels needed. The training-cutoff
  date is read from the provider's model documentation when the analysis is
  run and recorded with it, never transcribed from memory.
- **The dossier's gain, split the same way**, on the development partition. A
  benefit concentrated in scenarios the writer could remember is contamination,
  not comprehension. Small samples; reported with their intervals.

## Cost

One call per scenario over ~30k input tokens, under its own `dossier` phase
ceiling ($30) so the `compile` ceiling keeps meaning what M4 sized it for. In
every agent call the report adds ~900 cached-prefix tokens; at the batch and
cache-read rates that is on the order of a tenth of the simulate phase's
measured projection, and `cascade simulate estimate` gates it like any other
prefix change. It also lifts more actor prefixes over the 4,096-token cache
floor (ADR-0001), which M5 measured some as missing.

## Consequences

- `cascade compile dossier` — resumable per scenario; runs whether or not the
  flag is on, because writing the reports is how the comparison gets its "on"
  arm, and a stored dossier changes nothing until a configuration reads it.
- The corpus and the retrieval path must be final before dossiers are written:
  a dossier is a function of what retrieval returns.
- 41 unit tests; migration 019 applied and the **4 integration tests passed** on a
  scratch PostgreSQL 16 holding the rebuilt sealed registry (the hash survives
  `jsonb`, an edited row is detected, `cascade_sim` reads and cannot write). 23 of 23 seeded mutants are killed, including: support
  drawn from the whole pool instead of the cited excerpts; the cutoff's own day
  admitted; a late excerpt filtered instead of raised; an ungrounded cell given
  the dossier; the baseline left without it; a missing dossier rendered as
  empty.
