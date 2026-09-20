"""The bounded tool loop: what it costs, where it stops, and what it must not change.

Three claims.

*Bounded.* `kernel.tools.max_turns` is a bound on model calls per decision, not
a hope about model behaviour: the last permitted turn is pinned to the action
tool, and a turn that still fails to produce one ends as a recorded WAIT rather
than an exception or another call. A decider whose cost depended on model mood
would put §12.1's arithmetic out of reach for the whole arm.

*Truthfully accounted.* A multi-turn decision consumes several calls, and
`Decision.model_turns` carries the number rather than letting it disappear into
`int(from_model)` -- the shape of the defect M8 found in `runs.llm_calls`.

*Byte-identical for the arm it is not.* `LLMAgents` is what carries the
36,000-run headline through the ADR-0020 wavefront. Nothing here may touch what
it sends, so the plain arm's request is asserted from this file too.

The transport is the real SDK's, driven by the request body rather than by a
call counter, so a test that changed the loop's shape would not quietly keep
passing on a queue that happened to line up.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from cascade.aperture.projection import Observation
from cascade.config import Settings, ToolsConfig
from cascade.llm.cache import CallCache
from cascade.llm.client import LLMClient
from cascade.retrieval.schema import RetrievedChunk
from cascade.sim.actions import ActionSpace, Commit, Wait
from cascade.sim.agent import (
    LLMAgents,
    ToolsDisabled,
    ToolUsingAgents,
    prepare_actor,
    prepare_tool_actor,
    tool_policy,
)
from cascade.sim.prompts import ACTION_TOOL, ACTION_TOOL_NAME, RULES, brief_from
from cascade.sim.tools import LOOKUP_EVIDENCE, RECALL, TOOL_NAMES, ToolBelt
from tests.conftest import make_actor

CUTOFF = datetime(2024, 3, 1, tzinfo=UTC)
SPACE = ActionSpace(actor_id="actor_0", levers=("factor_0",), counterparties=("actor_1", "actor_2"))
KEY = ("scenario-1", "actor_0")

ACTION_PAYLOAD = {
    "type": "COMMIT",
    "target_factor": "factor_0",
    "magnitude": 0.4,
    "resource_spend": 0.2,
    "rationale": "hold the line",
}


def with_tools(settings: Settings, **overrides: Any) -> Settings:
    """Enable the tool arm on the real configuration, changing only `kernel.tools`."""
    tools = ToolsConfig(
        enabled=True,
        max_turns=overrides.pop("max_turns", 3),
        k_tool=overrides.pop("k_tool", 6),
        allow=overrides.pop("allow", TOOL_NAMES),
    )
    return settings.model_copy(
        update={"kernel": settings.kernel.model_copy(update={"tools": tools})}
    )


def observation() -> Observation:
    return Observation(
        actor_id="actor_0",
        step=3,
        factors={"factor_0": 0.42, "factor_1": 0.17},
        banded=("factor_1",),
        unobserved=("factor_2",),
        budget=0.55,
        pooled=0.0,
        trust={"actor_1": 0.25},
    )


def brief(evidence_chunks: int = 6, chunk_chars: int = 1200) -> Any:
    return brief_from(
        make_actor(0),
        levers={"factor_0": 0.4},
        counterparties=("actor_1", "actor_2"),
        horizon=24,
        question_context="Will the coalition hold through the vote?",
        evidence=tuple(
            (f"2019-0{index + 1}-01T00:00:00+00:00", "govpr", "e" * chunk_chars)
            for index in range(evidence_chunks)
        ),
    )


def chunk(chunk_id: str = "c1") -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk_id,
        document_id=f"doc-{chunk_id}",
        ordinal=0,
        body="the coalition whip has lost two members",
        published_at=datetime(2024, 1, 5, tzinfo=UTC),
        source="govpr",
        url="https://example.invalid/1",
        title="A document",
        distance=0.2,
    )


def stub_search(query: str, *, as_of: datetime, k: int) -> tuple[RetrievedChunk, ...]:
    """Deterministic and cutoff-respecting. The tool's content is not what is under test here."""
    del query, as_of, k
    return (chunk(),)


class MovingCorpus:
    """A retrieval port whose one document can be rewritten between decisions.

    Stands in for `corpus redate` (ADR-0044), which rewrites bodies in place:
    the point is that a tool result is *part of the next turn's request*, so a
    corpus that moved invalidates the turns after the one that retrieved.
    """

    def __init__(self) -> None:
        self.body = "the coalition whip has lost two members"

    def __call__(self, query: str, *, as_of: datetime, k: int) -> tuple[RetrievedChunk, ...]:
        del query, as_of, k
        return (chunk().model_copy(update={"body": self.body}),)


def envelope(model: str, blocks: list[dict[str, Any]], *, stop: str = "tool_use") -> dict[str, Any]:
    return {
        "id": "msg_01TEST",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": blocks,
        "stop_reason": stop,
        "stop_sequence": None,
        "usage": {
            "input_tokens": 4200,
            "output_tokens": 45,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
    }


def action_block(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "type": "tool_use",
        "id": "toolu_action",
        "name": ACTION_TOOL_NAME,
        "input": {"action": payload or ACTION_PAYLOAD},
    }


def lookup_block(query: str = "who is backing the coalition") -> dict[str, Any]:
    return {
        "type": "tool_use",
        "id": "toolu_lookup",
        "name": LOOKUP_EVIDENCE,
        "input": {"query": query},
    }


class Transport:
    """Answers from the request body, not from a call counter.

    A turn whose last message carries a `tool_result` is a turn after a
    lookup, so the double emits the action; otherwise it emits whatever the
    test asked for first. Driving off the body means a change to the loop's
    shape shows up as a different answer rather than as a queue that happens
    to still line up.
    """

    def __init__(self, *, first: list[dict[str, Any]] | None = None, always: Any = None) -> None:
        self.first = first if first is not None else [lookup_block()]
        self.always = always
        self.requests: list[dict[str, Any]] = []

    def client(self) -> httpx.Client:
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            self.requests.append(body)
            if self.always is not None:
                blocks = self.always
            elif _answered_a_tool(body):
                blocks = [action_block()]
            else:
                blocks = self.first
            return httpx.Response(
                200,
                json=envelope(body["model"], blocks),
                headers={"content-type": "application/json"},
            )

        return httpx.Client(transport=httpx.MockTransport(handler))


def _answered_a_tool(body: dict[str, Any]) -> bool:
    last = body["messages"][-1]["content"]
    return isinstance(last, list) and any(
        isinstance(block, dict) and block.get("type") == "tool_result" for block in last
    )


def agents(
    settings: Settings,
    tmp_path: Path,
    transport: Transport,
    *,
    allow: tuple[str, ...] = TOOL_NAMES,
    search: Any = None,
) -> ToolUsingAgents:
    tuned = with_tools(settings, allow=allow)
    policy = tool_policy(tuned)
    client = LLMClient(
        tuned.model_copy(update={"llm": tuned.llm.model_copy(update={"mode": "record"})}),
        phase="simulate",
        cache=CallCache(tmp_path / "llm"),
        http_client=transport.client(),
    )
    return ToolUsingAgents(
        settings=tuned,
        client=client,
        prepared={KEY: prepare_tool_actor(brief(), tuned, policy=policy)},
        belts={
            KEY: ToolBelt(
                scenario_id=KEY[0],
                actor_id=KEY[1],
                as_of=CUTOFF,
                search=stub_search if search is None else search,
                k=policy.k_tool,
                excerpt_chars=tuned.kernel.evidence_chars,
                allow=policy.allow,
            )
        },
        policy=policy,
    )


def decide(arm: ToolUsingAgents, *, memory: str = "s2: WAIT") -> Any:
    return arm.decide(
        scenario_id=KEY[0],
        actor_id=KEY[1],
        observation=observation(),
        memory=memory,
        space=SPACE,
    )


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------


def test_a_lookup_then_an_action_is_two_calls_and_says_so(
    settings: Settings, tmp_path: Path
) -> None:
    """The accounting claim, measured rather than asserted from the design.

    `model_turns` is 2 because two requests reached the client. `from_model`
    is still True and is still 1 if anyone counts it as a call -- which is
    exactly why the count needed a field of its own.
    """
    transport = Transport()
    arm = agents(settings, tmp_path, transport)

    decision = decide(arm)

    assert isinstance(decision.action, Commit)
    assert decision.coercion is None
    assert decision.model_turns == 2
    assert len(transport.requests) == 2
    assert arm.calls == 2
    assert arm.decisions == 1
    assert arm.tool_calls == 1
    assert arm.turns_per_decision == 2.0
    # Two turns at 45 output tokens each, summed rather than taken from the last.
    assert decision.tokens_out == 90
    assert decision.tokens_in == 8400


def test_the_retrieved_document_is_handed_back_as_a_quoted_tool_result(
    settings: Settings, tmp_path: Path
) -> None:
    """The second request must actually carry the evidence, or the loop is theatre."""
    transport = Transport()
    arm = agents(settings, tmp_path, transport)
    decide(arm)

    second = transport.requests[1]
    assert [message["role"] for message in second["messages"]] == ["user", "assistant", "user"]
    result_block = second["messages"][2]["content"][0]
    assert result_block["type"] == "tool_result"
    assert result_block["tool_use_id"] == "toolu_lookup"
    assert result_block["is_error"] is False
    assert "You have 1 quoted document(s)." in result_block["content"]
    assert "coalition whip" in result_block["content"]


def test_the_last_permitted_turn_is_pinned_to_the_action_tool(
    settings: Settings, tmp_path: Path
) -> None:
    """This is what makes `max_turns` a bound rather than a hope.

    Earlier turns are pinned to `any` -- some tool must be called -- because
    prose is not read by the simulation and a turn spent on it is bought and
    discarded.
    """
    transport = Transport()
    arm = agents(settings, tmp_path, transport)
    arm.policy = arm.policy.model_copy(update={"max_turns": 2})
    decide(arm)

    assert transport.requests[0]["tool_choice"] == {"type": "any"}
    assert transport.requests[1]["tool_choice"] == {"type": "tool", "name": ACTION_TOOL_NAME}


def test_exhausting_the_budget_is_a_recorded_wait_not_an_exception(
    settings: Settings, tmp_path: Path
) -> None:
    """A model that keeps looking things up must cost `max_turns` and no more.

    ADR-0018's bargain one level up: one unusable turn costs one actor one
    turn, and the run has 23 more steps of behaviour in it. The coercion is a
    stable code because it is aggregated over an ablation cell.
    """
    transport = Transport(always=[lookup_block()])
    arm = agents(settings, tmp_path, transport)
    arm.policy = arm.policy.model_copy(update={"max_turns": 2})

    decision = decide(arm)

    assert isinstance(decision.action, Wait)
    assert decision.coercion == "tool_budget_exhausted"
    assert decision.model_turns == 2
    assert len(transport.requests) == 2


def test_max_turns_of_one_makes_a_single_pinned_call(settings: Settings, tmp_path: Path) -> None:
    """The degenerate configuration must still be a valid arm, not a crash.

    At `max_turns: 1` the first turn is also the last, so it is pinned and the
    agent never gets to look anything up -- which is a legitimate cell of the
    grid and the natural floor of the cost curve.
    """
    transport = Transport(always=[action_block()])
    arm = agents(settings, tmp_path, transport)
    arm.policy = arm.policy.model_copy(update={"max_turns": 1})

    decision = decide(arm)

    assert isinstance(decision.action, Commit)
    assert decision.model_turns == 1
    assert transport.requests[0]["tool_choice"] == {"type": "tool", "name": ACTION_TOOL_NAME}


def test_prose_where_a_tool_call_was_required_costs_one_turn_not_the_budget(
    settings: Settings, tmp_path: Path
) -> None:
    """Same verdict the plain arm gives, and stopping keeps one confused turn cheap."""
    transport = Transport(always=[{"type": "text", "text": "I would prefer to negotiate."}])
    arm = agents(settings, tmp_path, transport)

    decision = decide(arm)

    assert isinstance(decision.action, Wait)
    assert decision.coercion == "no_tool_call"
    assert decision.model_turns == 1
    assert len(transport.requests) == 1


def test_an_inadmissible_action_is_coerced_by_the_same_rule_as_the_plain_arm(
    settings: Settings, tmp_path: Path
) -> None:
    """Admissibility is ADR-0018's, unchanged: a tool arm does not get its own."""
    transport = Transport(
        always=[action_block({"type": "ESCALATE", "target_factor": "factor_9", "magnitude": 0.8})]
    )
    arm = agents(settings, tmp_path, transport)

    decision = decide(arm)

    assert isinstance(decision.action, Wait)
    assert decision.coercion == "no_lever:factor_9"


def test_a_refused_tool_payload_is_reported_to_the_model_and_costs_a_turn(
    settings: Settings, tmp_path: Path
) -> None:
    """The refusal has to reach the agent, or it learns nothing and repeats it.

    The `is_error` flag and the message go back as the tool result; the loop
    continues, and the turn is charged.
    """
    transport = Transport(
        first=[
            {
                "type": "tool_use",
                "id": "toolu_lookup",
                "name": LOOKUP_EVIDENCE,
                "input": {"query": "coalition", "as_of": "2030-01-01T00:00:00+00:00"},
            }
        ]
    )
    arm = agents(settings, tmp_path, transport)

    decision = decide(arm)

    result_block = transport.requests[1]["messages"][2]["content"][0]
    assert result_block["is_error"] is True
    assert "takes exactly one field" in result_block["content"]
    assert decision.model_turns == 2


def test_recall_reads_the_memory_this_turn_was_given(settings: Settings, tmp_path: Path) -> None:
    """The namespace is the one the kernel rendered for this actor, and nothing else."""
    transport = Transport(
        first=[
            {
                "type": "tool_use",
                "id": "toolu_recall",
                "name": RECALL,
                "input": {"query": "signal"},
            }
        ]
    )
    arm = agents(settings, tmp_path, transport)

    decide(arm, memory="s2: WAIT\ns5: SIGNAL actor_1")

    result_block = transport.requests[1]["messages"][2]["content"][0]
    assert result_block["content"] == "s5: SIGNAL actor_1"


# ---------------------------------------------------------------------------
# Determinism and cost
# ---------------------------------------------------------------------------


def test_an_identical_decision_is_served_entirely_from_the_cache(
    settings: Settings, tmp_path: Path
) -> None:
    """Every turn goes through the one call site, so every turn is cached.

    That is what makes the arm replayable at all: the loop's second request is
    a pure function of the first response and the tool result it produced, and
    a tool result is a pure function of (query, cutoff, corpus).
    """
    transport = Transport()
    arm = agents(settings, tmp_path, transport)

    first = decide(arm)
    second = decide(arm)

    assert len(transport.requests) == 2, "the second decision must reach the network zero times"
    assert not first.cache_hit
    assert second.cache_hit
    assert first.action == second.action
    assert arm.hit_rate == 0.5


def test_a_decision_is_a_cache_hit_only_when_every_turn_was(
    settings: Settings, tmp_path: Path
) -> None:
    """A partially cached decision reached the network.

    Calling it a hit would flatter exactly the number §12.2's discount is
    judged on. The case is real rather than contrived: a tool result is part
    of the second turn's request, so a corpus that moved under a recorded
    decision leaves the first turn cached and the second a miss. `corpus
    redate` rewrites bodies in place (ADR-0044), which is precisely this.
    """
    corpus = MovingCorpus()
    arm = agents(settings, tmp_path, Transport(), search=corpus)
    decide(arm)

    corpus.body = "the coalition whip has lost four members"
    decision = decide(arm)

    assert decision.model_turns == 2
    assert not decision.cache_hit
    # Four turns across two decisions. The first decision recorded both of its
    # own; the second re-used only its first turn, because the second carried a
    # document that had changed underneath it.
    assert arm.calls == 4
    assert arm.cache_hits == 1


# ---------------------------------------------------------------------------
# The prefix, measured
# ---------------------------------------------------------------------------


def test_the_tool_arms_prefix_is_measured_over_what_it_actually_sends(
    settings: Settings, tmp_path: Path
) -> None:
    """Reusing the plain arm's measurement would under-report by the difference.

    Not a large error, and ADR-0001 records that the direction of this error is
    the one that stays invisible until the ledger: a prefix wrongly judged
    below the floor is sent unmarked and billed at list price on every call of
    the cell.
    """
    del tmp_path
    tuned = with_tools(settings)
    policy = tool_policy(tuned)

    plain = prepare_actor(brief(), tuned)
    tooled = prepare_tool_actor(brief(), tuned, policy=policy)

    assert tooled.prefix_tokens > plain.prefix_tokens
    assert tooled.persona == plain.persona
    assert tooled.cacheable


def test_the_arm_sends_three_system_blocks_and_both_tool_schemas(
    settings: Settings, tmp_path: Path
) -> None:
    """A separate block rather than an edit to RULES, which the other arm shares."""
    transport = Transport()
    arm = agents(settings, tmp_path, transport)
    decide(arm)

    sent = transport.requests[0]
    assert [block["type"] for block in sent["system"]] == ["text", "text", "text"]
    assert sent["system"][0]["text"] == RULES
    assert "Looking things up" in sent["system"][1]["text"]
    assert sent["system"][2]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert [tool["name"] for tool in sent["tools"]] == [
        ACTION_TOOL_NAME,
        LOOKUP_EVIDENCE,
        RECALL,
    ]


def test_a_cell_that_enables_one_tool_sends_one(settings: Settings, tmp_path: Path) -> None:
    """`allow` is a factor of the arm, so it must reach the wire."""
    transport = Transport()
    arm = agents(settings, tmp_path, transport, allow=(RECALL,))
    arm.policy = arm.policy.model_copy(update={"max_turns": 1})
    transport.always = [action_block()]
    decide(arm)

    assert [tool["name"] for tool in transport.requests[0]["tools"]] == [ACTION_TOOL_NAME, RECALL]
    assert RECALL in transport.requests[0]["system"][1]["text"]
    assert LOOKUP_EVIDENCE not in transport.requests[0]["system"][1]["text"]


# ---------------------------------------------------------------------------
# What must not have changed
# ---------------------------------------------------------------------------


def test_the_plain_arm_did_not_acquire_tools(settings: Settings, tmp_path: Path) -> None:
    """`LLMAgents` carries the 36,000-run headline through the ADR-0020 wavefront.

    Its request is two system blocks, one tool and a pinned choice, and it must
    stay that whatever this milestone added -- a third block or a second tool
    would change every cache key in the study's largest phase.
    """
    del tmp_path
    plain = LLMAgents(
        settings=settings,
        client=object(),
        prepared={KEY: prepare_actor(brief(), settings)},
    )
    request = plain.build_request(
        scenario_id=KEY[0], actor_id=KEY[1], observation=observation(), memory=""
    )

    assert request.system is not None and len(request.system) == 2
    assert request.tools == [ACTION_TOOL]
    assert request.tool_choice == {"type": "tool", "name": ACTION_TOOL_NAME}
    assert "Looking things up" not in RULES


def test_the_tool_arm_does_not_claim_to_be_batchable() -> None:
    """ADR-0020's wavefront submits one batch per step across the wave.

    A tool loop cannot be submitted that way -- turn 2 depends on what turn 1
    retrieved -- so this decider must not present the method the runner looks
    for. `_prepare` falls back to deciding turn by turn when it is absent;
    a `prepare` that quietly did nothing would leave the runner counting
    batches that were never sent.
    """
    assert not hasattr(ToolUsingAgents, "prepare")
    assert hasattr(LLMAgents, "prepare")


# ---------------------------------------------------------------------------
# Configuration fails closed
# ---------------------------------------------------------------------------


def test_a_disabled_policy_is_refused_rather_than_degraded(settings: Settings) -> None:
    """Silently behaving like the plain arm is ADR-0025's defect exactly.

    A cell would run, report a number, and have measured the arm it was being
    compared against.
    """
    with pytest.raises(ToolsDisabled, match="enabled is false"):
        tool_policy(settings)


def test_a_config_with_no_tools_cannot_be_constructed(settings: Settings) -> None:
    """The first of two guards: an enabled arm with no tools is not a config.

    Refused by `ToolsConfig` itself (M15 integration), so the incoherent
    combination cannot reach a run through the ordinary path at all.
    """
    with pytest.raises(ValidationError, match="empty allow-list"):
        with_tools(settings, allow=())


def test_a_policy_with_no_tools_is_refused_when_validation_was_bypassed(
    settings: Settings,
) -> None:
    """The second guard, and why it is not redundant.

    `model_copy(update=...)` and `model_construct` skip validation by design,
    and the test suite uses both to build awkward states. So the reader checks
    again at the point of use: a guard that only fires on the constructor is
    absent from exactly the paths that build a config without one.
    """
    unvalidated = ToolsConfig.model_construct(enabled=True, max_turns=3, k_tool=6, allow=())
    tuned = settings.model_copy(
        update={"kernel": settings.kernel.model_copy(update={"tools": unvalidated})}
    )
    with pytest.raises(ToolsDisabled, match="allow is empty"):
        tool_policy(tuned)


def test_the_decider_refuses_a_disabled_policy_too(settings: Settings) -> None:
    """The guard is on the constructor as well as on the reader, because a caller
    can build a `ToolsConfig` without going through `tool_policy`."""
    with pytest.raises(ToolsDisabled):
        ToolUsingAgents(
            settings=settings,
            client=object(),
            prepared={},
            belts={},
            policy=ToolsConfig(enabled=False, allow=TOOL_NAMES),
        )


def test_a_belt_keyed_to_the_wrong_actor_is_a_loud_failure(
    settings: Settings, tmp_path: Path
) -> None:
    """A belt carries a cutoff and a namespace, so a mis-keyed one retrieves under
    the wrong lock and returns something that looks entirely normal."""
    transport = Transport()
    arm = agents(settings, tmp_path, transport)
    arm.belts[KEY] = ToolBelt(
        scenario_id="scenario-9",
        actor_id="actor_9",
        as_of=CUTOFF,
        search=stub_search,
        k=6,
        excerpt_chars=900,
        allow=TOOL_NAMES,
    )

    with pytest.raises(KeyError, match="belongs to"):
        decide(arm)


def test_an_actor_with_no_belt_is_an_error_not_a_default(
    settings: Settings, tmp_path: Path
) -> None:
    transport = Transport()
    arm = agents(settings, tmp_path, transport)

    with pytest.raises(KeyError, match="no tool belt"):
        arm.decide(
            scenario_id=KEY[0],
            actor_id="ghost",
            observation=observation(),
            memory="",
            space=SPACE,
        )
