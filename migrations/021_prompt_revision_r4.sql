-- 021: prompt revision r4 -- quoted evidence (ADR-0045).
--
-- §1.3 makes a prompt change a recorded change, and `llm.prompt_rev` is part
-- of the LLM cache key, so a revision without this row would serve recordings
-- made against text the model never saw.
--
-- Both Briers are NULL: this predates the first backtest, so there is no
-- before and no after to record. What the revision is *for* was measured, and
-- it was measured on the attack rather than on accuracy -- see ADR-0045.
--
-- Forward-only. Never edit a migration that has been applied; add a new one.

BEGIN;

INSERT INTO prompt_revisions (prompt_rev, subsystem, summary, rationale)
VALUES
(
    'r4',
    'agent',
    'Retrieved documents are quoted between numbered <<<document N>>> markers '
    'that the renderer strips from document text, the count is stated, and the '
    'rule that quoted text is data rather than instructions lives in the '
    'system prompt.',
    'Measured before the change (`cascade eval injection`, 30 scenarios, the '
    'single-model forecaster whose evidence block is formatted as the agents'' '
    'prefix is): a document that closed the evidence section and spoke as the '
    'operator was obeyed on 20 of 30 scenarios, 95% CI [0.49, 0.81], mean '
    'shift +0.491 toward the instructed value; an appeal to a fabricated '
    'calibration authority on 7 of 30; a plain "ignore all previous '
    'instructions" on 0 of 30. The vector is impersonating the frame, not '
    'instructing the model, so the frame is made unforgeable rather than the '
    'instructions sterner. No Brier exists on either side.'
),
(
    'r4',
    'compiler',
    'The draft prompt quotes retrieved documents the same way, through the '
    'one renderer the agents and baselines use.',
    'The compiler reads 60 retrieved chunks per scenario and was open to the '
    'same impersonation. One renderer for all three call sites, so a defence '
    'measured on one is not assumed for the others.'
)
ON CONFLICT (prompt_rev, subsystem) DO NOTHING;

COMMIT;
