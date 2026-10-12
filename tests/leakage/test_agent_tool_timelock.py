"""Can an agent widen its own time lock through a tool call? (ADR-0049, §4.2)

The poison-pill probe next door asks whether the *retriever* can be made to
return post-cutoff evidence. This one asks the newer question: a tool arm hands
the model a channel into retrieval, so the model now supplies part of the
request. The claim is that the part it supplies cannot carry a cutoff --
because `LookupEvidenceArgs` has one field and forbids the rest, and because
the belt passes only that field into the search it was constructed with.

**The probes and their controls.** A probe that cannot fail proves nothing, so
each negative here is paired with a positive that would catch a retrieval path
returning nothing at all:

1. *The corpus really does hold post-cutoff material for these queries.* Asked
   at a later cutoff, the same query returns documents published after the
   scenario's own. Without this, every assertion below would hold against an
   empty answer.
2. *The belt's backstop can fire.* Given a retrieval port that deliberately
   returns those later documents, the belt drops every one and says how many.
   Without this, `dropped == 0` on the real path would be a number produced by
   a check that was incapable of producing anything else.
3. *A well-formed call retrieves.* The belt returns documents for an ordinary
   query, so "nothing came back" is never the reason a widening attempt
   failed.

Then the negatives: a payload carrying `as_of` (or a date under any other
name, or a chunk id) is refused before retrieval is reached; a query whose
*text* demands later material returns only pre-cutoff documents; and nothing
the belt renders carries a date at or after the cutoff.

Read-only throughout. Unlike the poison-pill probe this inserts nothing, so
there is no transaction to roll back and no way for the probe to become the
leak it is testing for.

Labels are never read: only ``record.scenario`` is touched, and the scenarios
could equally have come from ``load_scenarios``. The fixture is shared with the
suite next door, which needs the labels for a different reason.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from cascade.config import Settings
from cascade.retrieval.schema import RetrievedChunk
from cascade.retrieval.search import Chronofence
from cascade.sim.tools import (
    LOOKUP_EVIDENCE,
    RECALL,
    TOOL_NAMES,
    LookupEvidenceArgs,
    ToolBelt,
    chronofence_evidence,
)

pytestmark = pytest.mark.leakage

# Scenarios probed. Chosen as the earliest cutoffs in the sealed registry,
# deterministically, because those are the ones with the most post-cutoff
# corpus behind them -- which is what gives control (1) something to find. A
# random sample would make a failure depend on which scenarios were drawn.
PROBE_SCENARIOS = 5

K = 12


class Probe:
    """One scenario's bound belt, plus the pieces the controls need."""

    def __init__(
        self, scenario: Any, belt: ToolBelt, counting: CountingSearch, fence: Any, embed: Any
    ) -> None:
        self.scenario = scenario
        self.belt = belt
        self.counting = counting
        """The port the belt was constructed with, held here so a test can say
        what did and did not reach the corpus without reaching into the belt."""
        self.fence = fence
        self.embed = embed

    @property
    def cutoff(self) -> datetime:
        return self.scenario.cutoff_ts

    def query(self) -> str:
        """A real query about this scenario, capped to the tool's own limit."""
        return " ".join(self.scenario.question.split())[:180]

    def at(self, as_of: datetime) -> tuple[RetrievedChunk, ...]:
        """Retrieve outside the belt, at whatever cutoff is asked for.

        This is the control's instrument and deliberately *not* something the
        tool surface can do: it calls the fence directly, which is the one
        place `as_of` is a parameter at all.
        """
        search = chronofence_evidence(fence=self.fence, embed=self.embed)
        return tuple(search(self.query(), as_of=as_of, k=K))


class CountingSearch:
    """Wraps a real port and records every call that reached it."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.calls: list[tuple[str, datetime, int]] = []

    def __call__(self, query: str, *, as_of: datetime, k: int) -> Sequence[RetrievedChunk]:
        self.calls.append((query, as_of, k))
        return self.inner(query, as_of=as_of, k=k)


@pytest.fixture(scope="module")
def probes(live_settings: Settings, records: tuple[Any, ...], embedder: Any) -> Any:
    """Bind one tool belt per probed scenario, at that scenario's own cutoff.

    Opened as ``eval``, which is the role the study's own evidence retrieval
    runs under (`cli._maybe_chronofence`). The point of the probe is the
    Python boundary, not the grant -- ``tests/leakage/test_chronofence_grants``
    holds the grant.
    """
    scenarios = sorted(
        (record.scenario for record in records), key=lambda item: (item.cutoff_ts, item.scenario_id)
    )[:PROBE_SCENARIOS]
    assert scenarios, "the sealed registry is empty; nothing to probe"

    def embed(text: str) -> Sequence[float]:
        vector: Any = embedder.encode([text])[0]
        return list(vector)

    with Chronofence(live_settings, role="eval") as fence:
        search = chronofence_evidence(fence=fence, embed=embed)
        built: list[Probe] = []
        for scenario in scenarios:
            counting = CountingSearch(search)
            built.append(
                Probe(
                    scenario,
                    ToolBelt(
                        scenario_id=scenario.scenario_id,
                        actor_id="actor_0",
                        as_of=scenario.cutoff_ts,
                        search=counting,
                        k=K,
                        excerpt_chars=live_settings.kernel.evidence_chars,
                        allow=TOOL_NAMES,
                    ),
                    counting,
                    fence,
                    embed,
                )
            )
        yield built


def later_than(cutoff: datetime) -> datetime:
    """A cutoff comfortably after ``cutoff`` but not in the future.

    Clamped to now, because Chronofence's promise is about `published_at` and
    a cutoff past the end of the corpus would simply return the same rows,
    making the control vacuous in the other direction.
    """
    return min(cutoff + timedelta(days=365), datetime.now(UTC))


# ---------------------------------------------------------------------------
# Controls
# ---------------------------------------------------------------------------


def test_the_corpus_holds_post_cutoff_material_for_these_queries(probes: Any) -> None:
    """Control 1. Without it, every negative below holds against an empty answer.

    Asked at a later cutoff, at least one probed scenario's own query returns
    a document published after that scenario's cutoff. That is the material the
    belt must never return, proved to exist before anything asserts that it
    does not.
    """
    reachable = 0
    for probe in probes:
        later = later_than(probe.cutoff)
        if later <= probe.cutoff:
            continue
        if any(chunk.published_at >= probe.cutoff for chunk in probe.at(later)):
            reachable += 1

    assert reachable > 0, (
        "no probed scenario had any post-cutoff evidence reachable even with the "
        "cutoff moved forward a year; the widening probes below would pass without "
        "testing anything"
    )


def test_a_well_formed_tool_call_actually_retrieves(probes: Any) -> None:
    """Control 3. "Nothing came back" must never be why a widening attempt failed."""
    answered = sum(1 for probe in probes if probe.belt.lookup(probe.query()).n_results > 0)
    assert answered > 0, (
        "no probed scenario retrieved anything through the tool surface at its own "
        "cutoff; the refusals below would be indistinguishable from a broken tool"
    )


def test_the_belts_backstop_fires_on_real_post_cutoff_documents(probes: Any) -> None:
    """Control 2. The port is injected, so the belt must not assume Chronofence.

    A stub that hands back the *real* documents published after the cutoff --
    the ones control 1 proved exist -- must be caught and counted by
    `ToolBelt.admissible`. Without this, `dropped == 0` on the real path is a
    number from a check that could not have produced another.
    """
    fired = 0
    for probe in probes:
        later = later_than(probe.cutoff)
        if later <= probe.cutoff:
            continue
        leaked = tuple(chunk for chunk in probe.at(later) if chunk.published_at >= probe.cutoff)
        if not leaked:
            continue
        kept, dropped = probe.belt.admissible(leaked)
        assert kept == (), f"{probe.scenario.scenario_id}: a post-cutoff chunk survived the belt"
        assert dropped == len(leaked)
        fired += 1

    assert fired > 0, "the backstop was never exercised; this control tested nothing"


# ---------------------------------------------------------------------------
# The claim
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "extra",
    [
        {"as_of": "2030-01-01T00:00:00+00:00"},
        {"as_of": None},
        {"before": "2030-01-01"},
        {"published_after": "2024-06-01"},
        {"cutoff_ts": "2030-01-01T00:00:00+00:00"},
        {"chunk_id": "chunks_2026q1|0001"},
        {"k": 5000},
    ],
)
def test_a_payload_that_tries_to_widen_the_lock_never_reaches_retrieval(
    probes: Any, extra: dict[str, Any]
) -> None:
    """The claim, against the live corpus: the attempt is refused before the query runs.

    Not "the extra key is ignored" -- ignoring it would answer the query and
    drop the date, which is correct behaviour that looks identical, from both
    sides, to having honoured it. `extra="forbid"` makes the same payload a
    validation error, and the counting port proves nothing was retrieved under
    it.
    """
    probe = probes[0]
    before = len(probe.counting.calls)

    session = probe.belt.session(memory="")
    result = session.execute(LOOKUP_EVIDENCE, {"query": probe.query(), **extra})

    assert not result.ok
    assert result.error is not None and result.error.startswith("bad_arguments:")
    assert len(probe.counting.calls) == before, "a refused payload reached the corpus"


def test_the_refusal_does_not_disclose_the_cutoff(probes: Any) -> None:
    """An error naming the value would hand over, through the refusal itself,
    the one fact the refusal exists to withhold."""
    probe = probes[0]
    session = probe.belt.session(memory="")
    result = session.execute(
        LOOKUP_EVIDENCE, {"query": probe.query(), "as_of": "2030-01-01T00:00:00+00:00"}
    )

    assert str(probe.cutoff.year) not in result.content
    assert probe.cutoff.date().isoformat() not in result.content


def published_dates(content: str) -> list[datetime]:
    """The publication dates the rendered block actually shows the agent.

    Read off the frame `cascade.quoting` writes, so this asserts over what
    reaches the model rather than over the chunk list behind it: a renderer
    that leaked a date the filter had excluded would pass a chunk-level check.
    """
    out: list[datetime] = []
    for line in content.splitlines():
        if not line.startswith("<<<document"):
            continue
        out.append(datetime.fromisoformat(line.split("| published ", 1)[1].split(" | ", 1)[0]))
    return out


def test_a_query_that_asks_in_words_for_later_material_still_gets_none(probes: Any) -> None:
    """Prompt injection into the query text cannot move a lock the query never carries.

    The most favourable wording the model could choose: the query names the
    later period explicitly and asks for material published in it.
    """
    offenders: list[str] = []
    for probe in probes:
        later = later_than(probe.cutoff)
        text = f"news published after {later.date().isoformat()} about {probe.query()}"
        result = probe.belt.lookup(text[:180])
        assert result.dropped == 0, (
            f"{probe.scenario.scenario_id}: retrieval returned post-cutoff rows and the "
            "belt's backstop had to catch them; the time lock itself did not hold"
        )
        offenders.extend(
            f"{probe.scenario.scenario_id}@{published.isoformat()}"
            for published in published_dates(result.content)
            if published >= probe.cutoff
        )

    assert offenders == [], f"a query naming a later date retrieved it: {offenders[:5]}"


def test_nothing_the_tool_renders_is_dated_at_or_after_the_cutoff(probes: Any) -> None:
    """The property, read off what the agent would actually have been shown.

    Asserted over the rendered block rather than over the chunk list, because
    the rendered block is what reaches the model: a renderer that leaked a date
    the filter had excluded would pass a chunk-level assertion.
    """
    for probe in probes:
        result = probe.belt.lookup(probe.query())
        assert result.dropped == 0
        for published in published_dates(result.content):
            assert published < probe.cutoff, (
                f"{probe.scenario.scenario_id}: rendered a document published "
                f"{published.isoformat()} against a cutoff of {probe.cutoff.isoformat()}"
            )


def test_the_argument_model_the_live_probe_exercised_is_the_one_the_model_is_shown(
    probes: Any,
) -> None:
    """Guard the guard. Everything above tests a schema; this pins which schema.

    If a time field were ever added, the probes would keep passing -- they send
    keys that would then be valid -- while the property they exist to hold had
    been given away.
    """
    del probes
    assert sorted(LookupEvidenceArgs.model_fields) == ["query"]
    assert LookupEvidenceArgs.model_json_schema().get("additionalProperties") is False


def test_recall_cannot_be_pointed_at_another_actors_record(probes: Any) -> None:
    """The second namespace, checked here because it is the same kind of claim.

    A session is built from one rendered record and there is no argument that
    names an actor, so the only thing to assert is that the surface makes the
    crossing inexpressible -- and that a payload trying it is refused rather
    than silently answered from the session's own record.
    """
    probe = probes[0]
    mine = probe.belt.session(memory="s3: COMMIT factor_0 0.40")
    theirs = probe.belt.session(memory="s3: DEFECT actor_9")

    assert mine.execute(RECALL, {"query": "defect"}).n_results == 0
    assert theirs.execute(RECALL, {"query": "defect"}).n_results == 1
    refused = mine.execute(RECALL, {"query": "defect", "actor_id": "actor_9"})
    assert not refused.ok
    assert refused.error is not None and refused.error.startswith("bad_arguments:")
