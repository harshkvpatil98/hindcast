"""`runs.llm_calls` counts model calls, not decisions (M8, re-opened at M15).

M8 found that the kernel did `ctx.llm_calls += 1` once per decision regardless
of whether anything reached the model, so a run made with the stand-in decider
recorded 452,328 model calls costing $0.00 -- and §12.4's reconciliation
compared that zero against Langfuse's zero, agreed to 0.0000%, and passed. The
fix then was to count off `Decision.from_model`.

That was right only while every decision reaching the model cost exactly one
call. A tool-using decision costs up to `kernel.tools.max_turns` (ADR-0049),
so `int(from_model)` became the number of *decisions that made at least one
call* rather than the number of calls -- the same defect, in the same column,
in the direction that makes the study look cheaper than it was.

What is held here is that the column counts turns, and that a decision cannot
be constructed whose two accounts of itself disagree.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from cascade.sim.actions import Wait
from cascade.sim.agent import Decision


class TestADecisionCannotMisreportWhatItCost:
    def test_reaching_the_model_with_no_turns_is_refused(self) -> None:
        # The shape of M8's finding: a paid call booked as free.
        with pytest.raises(ValidationError, match="at least one turn"):
            Decision(action=Wait(), from_model=True, model_turns=0)

    def test_not_reaching_the_model_while_reporting_turns_is_refused(self) -> None:
        # The opposite error, which inflates the ledger against a provider
        # record that shows nothing.
        with pytest.raises(ValidationError, match="did not reach the model"):
            Decision(action=Wait(), from_model=False, model_turns=2)

    def test_a_stand_in_decision_is_coherent_at_its_defaults(self) -> None:
        # `policies.py` constructs these with neither field set; the defaults
        # must therefore already agree, or the heuristic arm would not build.
        decision = Decision(action=Wait())
        assert decision.from_model is False
        assert decision.model_turns == 0

    @pytest.mark.parametrize("turns", [1, 2, 3, 8])
    def test_a_multi_turn_decision_carries_its_own_count(self, turns: int) -> None:
        decision = Decision(action=Wait(), from_model=True, model_turns=turns)
        assert decision.model_turns == turns


class TestTheKernelBooksTurnsNotDecisions:
    """Read off the kernel's own accumulation, not off a re-implementation."""

    def test_the_counting_expression_sums_turns(self) -> None:
        # A hand-rolled sum over decisions, mirroring `stage_decide`. Three
        # decisions: one heuristic, one single-turn, one three-turn tool loop.
        decisions = [
            Decision(action=Wait()),
            Decision(action=Wait(), from_model=True, model_turns=1),
            Decision(action=Wait(), from_model=True, model_turns=3),
        ]
        assert sum(decision.model_turns for decision in decisions) == 4
        # What the pre-M15 expression would have reported, and why it is
        # wrong: two decisions reached the model, but four calls were made.
        assert sum(int(decision.from_model) for decision in decisions) == 2

    def test_the_kernel_uses_model_turns_and_not_from_model(self) -> None:
        # Static, deliberately: the accumulation lives inside a long method
        # whose other stages need a graph, a scheduler and an arbiter to
        # exercise. This asserts the one line, which is what regressed.
        from pathlib import Path

        import cascade.sim.kernel as kernel_module

        source = Path(kernel_module.__file__).read_text(encoding="utf-8")
        assert "ctx.llm_calls += decision.model_turns" in source
        assert "ctx.llm_calls += int(decision.from_model)" not in source
