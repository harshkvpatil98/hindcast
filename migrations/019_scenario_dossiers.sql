-- 019: scenario dossiers (ADR-0037).
--
-- One cited situation report per scenario, written once from a wide pool of
-- pre-cutoff evidence and read by the compiler and by every agent's cached
-- prefix. `scenario_id` is the primary key for the reason it is on
-- `causal_graphs` (008): a scenario has one dossier for the whole study, and
-- a table that could hold two would make "which report did this graph read"
-- unanswerable.
--
-- `dossier_sha256` is the content hash of the canonical JSON, stored so a
-- hand-edited row is detectable: a report is evidence the agents act on, and
-- an edit to it after the fact is an edit to the experiment.
--
-- `dropped` keeps the claims verification refused, with reasons. They never
-- reach a prompt. They are kept because the rate at which the writer asserts
-- what its sources do not say is the measurement that bounds the laundering
-- risk the ADR describes, and a count without the text cannot be audited.
--
-- Forward-only. Never edit a migration that has been applied; add a new one.

BEGIN;

CREATE TABLE IF NOT EXISTS scenario_dossiers (
    scenario_id    text        PRIMARY KEY REFERENCES scenarios (scenario_id),
    dossier_sha256 text        NOT NULL,
    dossier        jsonb       NOT NULL,
    n_claims       integer     NOT NULL CHECK (n_claims >= 0),
    n_dropped      integer     NOT NULL CHECK (n_dropped >= 0),
    dropped        jsonb       NOT NULL DEFAULT '[]'::jsonb,
    n_excerpts     integer     NOT NULL CHECK (n_excerpts >= 0),
    llm_calls      integer     NOT NULL CHECK (llm_calls >= 0),
    writer_model   text        NOT NULL,
    prompt_rev     text        NOT NULL,
    written_at     timestamptz NOT NULL DEFAULT now()
);

-- Which report a graph was compiled against. NULL for a graph compiled with
-- the dossier off, which is a different artifact from one compiled with it on
-- even if the JSON happened to match.
ALTER TABLE causal_graphs ADD COLUMN IF NOT EXISTS dossier_sha256 text;

-- The prompt change this migration carries, recorded here so it cannot be
-- skipped (the rule 010 set for r2). Both subsystems read the report.
INSERT INTO prompt_revisions (prompt_rev, subsystem, summary, rationale)
VALUES
(
    'r3',
    'compiler',
    'When dossier.enabled, the draft prompt carries a "Situation report" '
    'section ahead of the retrieved evidence: a verified, dated summary built '
    'from up to 100 pre-cutoff chunks.',
    'The compiler previously saw only the 60 chunks nearest the question text, '
    'however much admissible evidence the corpus held. Every claim in the '
    'report cites pre-cutoff excerpts and is dropped by a model-free check if '
    'those excerpts do not carry its names, numbers and wording (ADR-0037). '
    'With the dossier off the prompt is byte-identical to r2. No Brier exists '
    'on either side: this predates the first backtest.'
),
(
    'r3',
    'agent',
    'When dossier.enabled, the cached persona block carries the same '
    'situation report between "The situation" and the per-actor evidence.',
    'Each agent previously saw six chunks retrieved for its own query and '
    'nothing about the wider state of play. The report is shared by every '
    'actor in a scenario and lives in the cached prefix, so it is a cache-read '
    'cost per call and not a dynamic-token cost (ADR-0019). With the dossier '
    'off the prompt is byte-identical to r2. No Brier exists on either side.'
)
ON CONFLICT (prompt_rev, subsystem) DO NOTHING;

-- --------------------------------------------------------------------------
-- Grants
--
-- `cascade_sim` reads dossiers for the same reason it reads graphs: it builds
-- the agents' prompts. The table carries no outcome -- it is built from the
-- question, the registry's party names and pre-cutoff evidence -- so
-- invariant 2 is untouched. Writes are the admin role's, as for graphs.
-- --------------------------------------------------------------------------

GRANT SELECT ON scenario_dossiers TO cascade_sim, cascade_eval;

COMMIT;
