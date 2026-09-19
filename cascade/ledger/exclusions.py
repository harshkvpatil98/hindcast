"""Sealed scenarios declared unscoreable before any forecast existed (M14, ADR-0043).

Here, beside the registry, because it is a property of the registry that more
than one layer needs: the split and the report exclude these scenarios from
scoring, and the corpus and every model-spending command skip them.

The sealed registry is kept exactly as sealed: its manifest, its YES rate and
its frozen split are the study's and do not move. What this module declares is
narrower. Some sealed scenarios are not questions -- an exchange pre-lists the
legs of an open-ended event before the names are known ("Will Candidate B win
the 2026 Busan Mayoral Election?", "Will Company K be the largest company in
the world by market cap on June 30?"), and those legs resolved, so they passed
every rule. A forecast of "Candidate B" measures nothing about forecasting.

They are excluded from every scored figure and **counted**, like an
unparseable baseline answer or a market with no price. The rule is a pure
function of the question's wording, fixed and applied before any forecast, and
the excluded ids are part of the split's pinned fingerprint -- so the rule
cannot be widened afterwards to drop a scenario that went badly without the pin
refusing it.

Pure: no I/O, no clock, no label.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict

__all__ = ["EXCLUSION_REASON", "ExcludedScenario", "exclusions", "is_placeholder"]

EXCLUSION_REASON = "placeholder_leg"

# "placeholder", or a role noun followed by one or two capital letters. The
# letters are case-sensitive, so "Team USA", "Company 3M" and "the candidate
# who wins" are names or prose, not stand-ins.
_PLACEHOLDER = re.compile(
    r"\bplaceholder\b"
    r"|\b(?:candidate|company|person|player|team|artist|option|driver|party|country|movie|song)"
    r"\s+(?-i:[A-Z]{1,2})\b(?!\.)",
    re.IGNORECASE,
)


class ExcludedScenario(BaseModel):
    """One sealed scenario left out of every scored figure, and why."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    scenario_id: str
    reason: str


def is_placeholder(question: str) -> bool:
    """Whether a question names a stand-in rather than a party.

    Preserves outcome-independence by signature: the wording is the only
    input. Stand-ins almost always resolve NO, so a rule that could see the
    outcome would be indistinguishable from this one on the data -- which is
    why it must be unable to.
    """
    return _PLACEHOLDER.search(question) is not None


def exclusions(scenarios: Sequence[tuple[str, str]]) -> tuple[ExcludedScenario, ...]:
    """The excluded scenarios among ``(scenario_id, question)`` pairs, sorted.

    Preserves invariant 7 and reproducibility: the result depends on the set
    of pairs, not their order.
    """
    return tuple(
        ExcludedScenario(scenario_id=scenario_id, reason=EXCLUSION_REASON)
        for scenario_id, question in sorted(scenarios)
        if is_placeholder(question)
    )
