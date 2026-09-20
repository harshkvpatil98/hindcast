"""The DECIDE stage: one agent turn, through the one LLM call site (§7.2, §8.3).

Everything model-facing about a turn is here, and nothing else is. The kernel
hands this module an observation and gets back an action; whether that action
came from the network, from the record/replay cache or from a stand-in policy
is invisible to the step loop and visible in the event log.

The action cache and the LLM cache are **the same cache**, which is the point
of rounding observed values at the projection. A turn's request is a pure
function of (rules, persona, evidence, observation, memory), so two replicates
whose trajectories have not diverged produce identical request bytes and the
second is served from disk. §12.1's 91% hit rate is therefore a property of the
prompt's construction rather than a separate subsystem to build and keep
consistent.

Prompt caching is applied per (scenario, actor) and only when the prefix
actually clears the provider's floor. ADR-0001: below 4,096 tokens Anthropic
silently declines to cache while still charging the write premium, so marking a
short prefix is strictly worse than not marking it. The decision is measured,
recorded on the run, and never assumed.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cascade.aperture.projection import Observation
from cascade.config import Settings, ToolsConfig
from cascade.llm.client import estimate_tokens
from cascade.llm.types import BatchItem, LLMRequest
from cascade.sim.actions import Action, ActionSpace, Wait, admit
from cascade.sim.prompts import (
    ACTION_TOOL,
    ACTION_TOOL_NAME,
    RULES,
    ActorBrief,
    persona_block,
    tool_rules,
)
from cascade.sim.prompts import turn_message as render_turn
from cascade.sim.tools import ToolBelt, ToolSession, allowed_tools, tool_schemas

__all__ = [
    "Decision",
    "DecisionPolicy",
    "LLMAgents",
    "PreparedActor",
    "ToolUsingAgents",
    "ToolsDisabled",
    "prepare_actor",
    "prepare_tool_actor",
    "tool_policy",
]


class Decision(BaseModel):
    """One agent turn's outcome, with the accounting the event log needs."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    action: Action
    coercion: str | None = None
    cache_hit: bool = False
    tokens_in: int = Field(default=0, ge=0)
    tokens_out: int = Field(default=0, ge=0)
    latency_ms: int | None = None
    from_model: bool = False
    """Whether this decision came from the model at all.

    The kernel counts `runs.llm_calls` off this rather than off the number of
    decisions. They are not the same number: a stand-in decider (ADR: runs are
    stamped `policy='heuristic'`) makes none, and §12.4's reconciliation sums
    the column as *calls* -- so counting decisions made a heuristic run report
    452,328 model calls costing nothing, which is a discrepancy the gate would
    have been unable to see because both sides were zero."""
    model_turns: int = Field(default=0, ge=0)
    """Model calls this one decision cost.

    M8's finding, carried one step further. `from_model` answers "did this
    reach the model", which was enough while every decision that did cost
    exactly one call. A tool-using decision costs up to `kernel.tools.max_turns`
    (ADR-0049), so `int(from_model)` is no longer the number of calls -- it is
    the number of *decisions that made at least one*. The truthful count is
    carried here rather than folded into a sum, because a ledger that
    disagrees with the provider by a factor it cannot name is the failure
    §12.4's gate exists to catch."""

    @model_validator(mode="after")
    def _turns_and_from_model_agree(self) -> Decision:
        """Refuse a decision whose two accounts of itself disagree.

        The kernel books `runs.llm_calls` off `model_turns` and stamps the run
        `policy` off `from_model`. A decision claiming to have reached the
        model while reporting no turns would be booked as free and reported as
        a model run -- which is exactly the shape of M8's finding, where a
        heuristic run recorded 452,328 calls costing nothing and the
        reconciliation compared zero against zero and passed.

        Enforced here rather than at the two call sites so a decider added
        later cannot reintroduce it by forgetting a field.
        """
        if self.from_model and self.model_turns == 0:
            raise ValueError(
                "a decision from the model must report at least one turn; "
                "model_turns=0 with from_model=True would book a paid call as free"
            )
        if not self.from_model and self.model_turns:
            raise ValueError(
                f"a decision that did not reach the model reports {self.model_turns} "
                "turn(s); a stand-in decider makes no calls and booking them would "
                "inflate the ledger against a provider record that shows none"
            )
        return self


class DecisionPolicy(Protocol):
    """What the kernel needs from whatever is deciding.

    A Protocol rather than a base class so the LLM-backed agents and the
    deterministic stand-in in :mod:`cascade.sim.policies` are interchangeable
    at the kernel boundary without either knowing about the other.
    """

    def decide(
        self,
        *,
        scenario_id: str,
        actor_id: str,
        observation: Observation,
        memory: str,
        space: ActionSpace,
    ) -> Decision: ...


class BatchingPolicy(Protocol):
    """A decider that can resolve a whole wave of turns before any is decided.

    Optional: the runner checks for it and falls back to one call per turn.
    This is the seam the 50% batch discount lives in (§12.2) -- the kernel
    still asks for one decision at a time, and by then every answer is already
    on disk.
    """

    def prepare(self, turns: Sequence[Any]) -> int: ...


@dataclass(frozen=True)
class PreparedActor:
    """One actor's cacheable prefix, built once per (scenario, actor)."""

    brief: ActorBrief
    persona: str
    prefix_tokens: int
    cacheable: bool


def prepare_actor(brief: ActorBrief, settings: Settings) -> PreparedActor:
    """Render an actor's persona block and decide whether to mark it cacheable.

    The measurement is over the whole static prefix -- tool schema, rules and
    persona -- because that is what the provider caches, not the last block
    alone.
    """
    persona = persona_block(brief, evidence_chars=settings.kernel.evidence_chars)
    tokens = estimate_tokens(RULES) + estimate_tokens(persona) + estimate_tokens(str(ACTION_TOOL))
    cacheable = settings.prompt_cache.enabled and tokens >= settings.prompt_cache.min_prefix_tokens
    return PreparedActor(brief=brief, persona=persona, prefix_tokens=tokens, cacheable=cacheable)


@dataclass
class LLMAgents:
    """Decides every actor's turn through :class:`~cascade.llm.client.LLMClient`.

    One instance per run; the prepared prefixes are shared across the 200
    replicates of a scenario, which is where the cache economics live.
    """

    settings: Settings
    client: Any
    prepared: dict[tuple[str, str], PreparedActor]
    """(scenario_id, actor_id) -> its cacheable prefix.

    Keyed by both because one instance serves every scenario in a wave, and
    actor ids are per-graph slugs that repeat across scenarios -- two different
    parties called ``incumbent_party`` would otherwise share a persona. Serving
    the whole wave from one decider is what lets a step's misses go out as a
    single batch instead of one per scenario."""

    calls: int = field(default=0)
    cache_hits: int = field(default=0)

    def build_request(
        self,
        *,
        scenario_id: str,
        actor_id: str,
        observation: Observation,
        memory: str,
    ) -> LLMRequest:
        """Assemble one turn's request. The single definition of the prompt.

        Shared by :meth:`decide` and :meth:`prepare` on purpose: if the batch
        path built its own request, a one-character difference would make every
        batched answer a cache miss at decide time and the study would pay
        twice for every decision while looking like it worked.
        """
        actor = self.prepared.get((scenario_id, actor_id))
        if actor is None:
            raise KeyError(
                f"no prepared prompt for actor {actor_id!r} of scenario {scenario_id!r}; "
                "every actor is prepared before step 0, so this means the graph and the "
                "run disagree"
            )
        persona: dict[str, Any] = {"type": "text", "text": actor.persona}
        if actor.cacheable:
            # ADR-0007: stripped from the cache key, so this is a pure cost
            # change and does not invalidate a single recorded call.
            persona["cache_control"] = {
                "type": "ephemeral",
                "ttl": self.settings.prompt_cache.ttl,
            }
        return LLMRequest(
            model=self.settings.models.agent,
            system=[{"type": "text", "text": RULES}, persona],
            messages=[
                {
                    "role": "user",
                    "content": render_turn(
                        observation,
                        memory=memory,
                        horizon=self.settings.kernel.steps,
                    ),
                }
            ],
            tools=[ACTION_TOOL],
            tool_choice={"type": "tool", "name": ACTION_TOOL_NAME},
            temperature=self.settings.models.temperature,
            max_tokens=self.settings.models.agent_max_tokens,
            prompt_rev=self.settings.llm.prompt_rev,
        )

    def prepare(self, turns: Sequence[Any]) -> int:
        """Resolve a whole wave of turns up front, batching the cache misses.

        Returns the number of turns whose answers are now available locally.
        Nothing is decided here: this only fills the cache, so the decisions
        that follow are byte-identical to the ones an unbatched run would make.
        That equivalence is what lets M6 take the 50% discount without M8's
        replay hash changing.
        """
        if not turns:
            return 0
        items = [
            BatchItem(
                custom_id=f"{turn.run_id}|{turn.observation.step}|{turn.actor_id}",
                request=self.build_request(
                    scenario_id=turn.scenario_id,
                    actor_id=turn.actor_id,
                    observation=turn.observation,
                    memory=turn.memory,
                ),
            )
            for turn in sorted(turns, key=lambda t: (t.scenario_id, t.run_id, t.actor_id))
        ]
        self.client.complete_batch(items, trace_name="loom.decide.batch")
        return len(items)

    def decide(
        self,
        *,
        scenario_id: str,
        actor_id: str,
        observation: Observation,
        memory: str,
        space: ActionSpace,
    ) -> Decision:
        """Ask the model for one action and admit it (spec §7.2, stage 5)."""
        request = self.build_request(
            scenario_id=scenario_id,
            actor_id=actor_id,
            observation=observation,
            memory=memory,
        )
        result = self.client.complete(request, trace_name="loom.decide")
        self.calls += 1
        if result.served_from_cache:
            self.cache_hits += 1

        payload = _tool_input(result)
        if payload is None:
            # Not an error the run should die of: one unusable answer costs one
            # actor one turn, and the run has 23 more steps of behaviour in it.
            return Decision(
                action=Wait(),
                coercion="no_tool_call",
                cache_hit=result.served_from_cache,
                tokens_in=result.usage.input_tokens + result.usage.cache_read_input_tokens,
                tokens_out=result.usage.output_tokens,
                latency_ms=int(result.latency_ms),
                from_model=True,
                model_turns=1,
            )
        action, coercion = admit(payload, space)
        return Decision(
            action=action,
            coercion=coercion,
            cache_hit=result.served_from_cache,
            tokens_in=result.usage.input_tokens + result.usage.cache_read_input_tokens,
            tokens_out=result.usage.output_tokens,
            latency_ms=int(result.latency_ms),
            from_model=True,
            model_turns=1,
        )

    @property
    def hit_rate(self) -> float:
        """Measured action-cache hit rate. The M6 criterion is read off this."""
        return self.cache_hits / self.calls if self.calls else 0.0


def _tool_input(result: Any) -> Any:
    """Pull the action payload out of a tool call, or None if there is none."""
    for block in result.tool_calls:
        if block.get("name") != ACTION_TOOL_NAME:
            continue
        payload = block.get("input")
        if isinstance(payload, dict):
            inner = payload.get("action")
            return inner if isinstance(inner, dict) else payload
    return None


# ---------------------------------------------------------------------------
# The tool-using arm (ADR-0049)
#
# A second decider rather than an option on the first. `LLMAgents` is one call
# per decision and is therefore batchable: `prepare` resolves a whole step of
# the wavefront in one submission, which is what keeps the simulate phase
# inside its ceiling at the 50% batch rate (ADR-0020). A tool loop is multi-
# turn by construction -- turn 2 depends on what turn 1 retrieved -- so it
# cannot be submitted as one batch per step, and a class that claimed otherwise
# would be discovered at 36,000 runs rather than here. `ToolUsingAgents`
# deliberately has no `prepare`: the runner looks for the method and falls back
# to deciding turn by turn (`ensemble/runner.py::_prepare`).
# ---------------------------------------------------------------------------


class ToolsDisabled(RuntimeError):
    """A tool decider was built against a configuration that has no tools.

    Refused rather than degraded to the plain arm. The two arms differ in the
    prompt, the tool block and the cost model, so a tool decider that silently
    behaved like :class:`LLMAgents` would produce runs labelled as one arm and
    made by the other -- the defect ADR-0025 found in factors A and C, where a
    cell ran, reported, and measured nothing.
    """


def tool_policy(settings: Settings) -> ToolsConfig:
    """Read and validate ``kernel.tools``. Fails closed on an unusable arm.

    Normalises ``allow`` into :data:`~cascade.sim.tools.TOOL_NAMES` order, so
    two operators who listed the same tools differently address one cache
    rather than two, and raises on a name that does not resolve: a typo would
    otherwise hand the agent a narrower surface than the cell claims to be
    measuring, and nothing downstream could tell.
    """
    policy = settings.kernel.tools
    allow = allowed_tools(policy.allow)
    if not policy.enabled:
        raise ToolsDisabled(
            "kernel.tools.enabled is false; a tool decider built against it would send "
            "the tool arm's prompt and never be given a tool. Enable it in the ablation "
            "overlay for the cell under test, or use LLMAgents."
        )
    if not allow:
        raise ToolsDisabled(
            "kernel.tools.allow is empty; a tool arm with no tools is the plain arm with "
            "a different cache key, which would be reported as a measured difference."
        )
    return policy.model_copy(update={"allow": allow})


def prepare_tool_actor(
    brief: ActorBrief, settings: Settings, *, policy: ToolsConfig
) -> PreparedActor:
    """Render an actor's persona and measure the **tool arm's** prefix.

    A separate measurement rather than a reuse of :func:`prepare_actor`,
    because the two arms do not send the same prefix: this one carries an extra
    system block and two extra tool schemas. Reusing the plain arm's number
    would under-report by the difference and, on a thin persona, could mark a
    prefix uncacheable that in fact clears the floor -- paying list price for
    every call of the cell, which ADR-0001 records as the failure that stays
    invisible until the ledger.
    """
    persona = persona_block(brief, evidence_chars=settings.kernel.evidence_chars)
    rules = tool_rules(max_turns=policy.max_turns, k=policy.k_tool, allow=policy.allow)
    tools = [ACTION_TOOL, *tool_schemas(policy.allow)]
    tokens = (
        estimate_tokens(RULES)
        + estimate_tokens(rules)
        + estimate_tokens(persona)
        + sum(estimate_tokens(str(tool)) for tool in tools)
    )
    cacheable = settings.prompt_cache.enabled and tokens >= settings.prompt_cache.min_prefix_tokens
    return PreparedActor(brief=brief, persona=persona, prefix_tokens=tokens, cacheable=cacheable)


@dataclass
class ToolUsingAgents:
    """Decides a turn through a bounded tool loop, through the one call site.

    Every model call goes through :class:`~cascade.llm.client.LLMClient`
    (invariant 5), so each turn is cached, metered, traced and replayable
    exactly as a single-call decision is. That is what makes the arm replayable
    at all: the loop's *n*-th request is a pure function of the first *n-1*
    responses and the tool results they produced, and a tool result is a pure
    function of (query, cutoff, corpus) -- so a replayed decision walks the
    same turns and lands on the same action.
    """

    settings: Settings
    client: Any
    prepared: dict[tuple[str, str], PreparedActor]
    """(scenario_id, actor_id) -> the tool arm's prefix, from
    :func:`prepare_tool_actor`."""
    belts: dict[tuple[str, str], ToolBelt]
    """(scenario_id, actor_id) -> its bound tool surface.

    Keyed identically to ``prepared``, for the same reason, with one addition
    that matters more: a belt carries this scenario's ``as_of``, so a mis-keyed
    lookup would retrieve an actor's evidence under another scenario's cutoff
    and return something that looks entirely normal. :meth:`_belt` asserts the
    belt it found names the actor it was asked for rather than trusting the
    key."""
    policy: ToolsConfig

    calls: int = field(default=0)
    """Model calls, not decisions. The two differ here by up to
    ``policy.max_turns``."""
    cache_hits: int = field(default=0)
    decisions: int = field(default=0)
    tool_calls: int = field(default=0)
    dropped_post_cutoff: int = field(default=0)
    """Chunks the belts refused after retrieval had returned them. Zero against
    Chronofence; anything else is a leaking retrieval port, and it is reported
    rather than absorbed."""

    def __post_init__(self) -> None:
        if not self.policy.enabled or not self.policy.allow:
            raise ToolsDisabled(
                "ToolUsingAgents needs an enabled policy with at least one tool; build it "
                "from tool_policy(settings), which refuses both cases with the reason."
            )
        self.allow = allowed_tools(self.policy.allow)
        self.rules = tool_rules(
            max_turns=self.policy.max_turns, k=self.policy.k_tool, allow=self.allow
        )
        # Fixed order and fixed contents: the tool block is part of the prompt
        # and therefore part of the cache key (`LLMRequest.cache_domain`).
        self.tools: list[dict[str, Any]] = [ACTION_TOOL, *tool_schemas(self.allow)]

    # -- request construction ------------------------------------------------

    def _belt(self, scenario_id: str, actor_id: str) -> ToolBelt:
        """The bound tool surface for one turn, or a loud failure.

        Checks the belt's own ``(scenario_id, actor_id)`` against what was
        asked for. Key and fields are set by the same caller, so this only ever
        fires on a wiring mistake -- which is exactly the mistake that would run
        one actor's queries under another scenario's time lock.
        """
        belt = self.belts.get((scenario_id, actor_id))
        if belt is None:
            raise KeyError(
                f"no tool belt for actor {actor_id!r} of scenario {scenario_id!r}; every "
                "actor is given one before step 0, so this means the graph and the run "
                "disagree"
            )
        if belt.scenario_id != scenario_id or belt.actor_id != actor_id:
            raise KeyError(
                f"tool belt keyed ({scenario_id!r}, {actor_id!r}) belongs to "
                f"({belt.scenario_id!r}, {belt.actor_id!r}); it carries a cutoff and a "
                "memory namespace, and using it here would retrieve under the wrong one"
            )
        return belt

    def build_request(
        self,
        *,
        scenario_id: str,
        actor_id: str,
        messages: list[dict[str, Any]],
        final: bool,
    ) -> LLMRequest:
        """Assemble one turn of the loop. The single definition of this arm's prompt.

        ``final`` pins ``tool_choice`` to the action tool, which is what makes
        ``max_turns`` a bound on calls rather than a hope: the last permitted
        turn cannot be spent on another lookup. Earlier turns are pinned to
        ``any`` -- some tool must be called -- because prose is not read by the
        simulation, and a turn spent on it is a turn bought and discarded.
        """
        actor = self.prepared.get((scenario_id, actor_id))
        if actor is None:
            raise KeyError(
                f"no prepared prompt for actor {actor_id!r} of scenario {scenario_id!r}; "
                "every actor is prepared before step 0, so this means the graph and the "
                "run disagree"
            )
        persona: dict[str, Any] = {"type": "text", "text": actor.persona}
        if actor.cacheable:
            # ADR-0007: stripped from the cache key, so marking it is a pure
            # cost change and invalidates nothing already recorded.
            persona["cache_control"] = {
                "type": "ephemeral",
                "ttl": self.settings.prompt_cache.ttl,
            }
        choice: dict[str, Any] = (
            {"type": "tool", "name": ACTION_TOOL_NAME} if final else {"type": "any"}
        )
        return LLMRequest(
            model=self.settings.models.agent,
            system=[
                {"type": "text", "text": RULES},
                {"type": "text", "text": self.rules},
                persona,
            ],
            messages=messages,
            tools=self.tools,
            tool_choice=choice,
            temperature=self.settings.models.temperature,
            max_tokens=self.settings.models.agent_max_tokens,
            prompt_rev=self.settings.llm.prompt_rev,
        )

    # -- the loop ------------------------------------------------------------

    def decide(
        self,
        *,
        scenario_id: str,
        actor_id: str,
        observation: Observation,
        memory: str,
        space: ActionSpace,
    ) -> Decision:
        """Run a bounded tool loop and admit whatever action it ends on (§7.2, stage 5).

        Exhausting the budget is a *recorded outcome*, never an exception and
        never another turn: the decision becomes a WAIT carrying the reason, on
        ADR-0018's precedent, because one unusable turn costs one actor one
        turn and the run has 23 more steps of behaviour in it. Retrying until
        the model cooperates would make the per-decision cost a function of
        model mood, which is the one thing §12.1's arithmetic cannot absorb.
        """
        belt = self._belt(scenario_id, actor_id)
        session = belt.session(memory=memory)
        messages: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": render_turn(
                    observation, memory=memory, horizon=self.settings.kernel.steps
                ),
            }
        ]
        tally = _Turns()
        self.decisions += 1

        for turn in range(self.policy.max_turns):
            final = turn == self.policy.max_turns - 1
            request = self.build_request(
                scenario_id=scenario_id, actor_id=actor_id, messages=messages, final=final
            )
            result = self.client.complete(request, trace_name="loom.decide.tool")
            self.calls += 1
            tally.absorb(result)
            if result.served_from_cache:
                self.cache_hits += 1

            payload = _tool_input(result)
            if payload is not None:
                action, coercion = admit(payload, space)
                return tally.decision(action, coercion)

            asked = [
                block
                for block in result.tool_calls
                if str(block.get("name", "")) != ACTION_TOOL_NAME
            ]
            if not asked:
                # Prose where a tool call was required. The same verdict the
                # plain arm gives, and stopping here rather than re-asking
                # keeps one confused turn from consuming the whole budget.
                return tally.decision(Wait(), "no_tool_call")
            if final:
                # The last turn was pinned to the action tool and answered with
                # something else. Recorded, not retried: another turn is
                # another call, and the budget is what makes this arm's cost a
                # function of the configuration rather than of the model.
                return tally.decision(Wait(), "tool_budget_exhausted")

            messages.extend(self._exchange(result, asked, session))

        return tally.decision(Wait(), "tool_budget_exhausted")

    def _exchange(
        self, result: Any, asked: list[dict[str, Any]], session: ToolSession
    ) -> list[dict[str, Any]]:
        """Echo the model's turn back and answer each tool call it made.

        Blocks are handled in the order the provider returned them, not sorted.
        That order is part of the recorded response body and is reproduced byte
        for byte on replay, while sorting would reorder the answers relative to
        the assistant turn they belong to -- a different conversation from the
        one that was recorded, assembled from the same parts.
        """
        assistant: list[dict[str, Any]] = []
        if result.text.strip():
            assistant.append({"type": "text", "text": result.text})
        assistant.extend(result.tool_calls)

        answers: list[dict[str, Any]] = []
        for block in asked:
            outcome = session.execute(str(block.get("name", "")), block.get("input"))
            self.tool_calls += 1
            self.dropped_post_cutoff += outcome.dropped
            answers.append(
                {
                    "type": "tool_result",
                    "tool_use_id": str(block.get("id", "")),
                    "content": outcome.content,
                    "is_error": not outcome.ok,
                }
            )
        return [
            {"role": "assistant", "content": assistant},
            {"role": "user", "content": answers},
        ]

    @property
    def hit_rate(self) -> float:
        """Measured cache hit rate over **model calls**, not over decisions.

        §12.1's 88% criterion is stated over decisions, and this arm makes
        several calls per decision, so the two are different numbers and a
        report has to say which one it is quoting.
        :attr:`turns_per_decision` is the conversion between them.
        """
        return self.cache_hits / self.calls if self.calls else 0.0

    @property
    def turns_per_decision(self) -> float:
        """Measured model calls per decision. This arm's cost multiplier.

        §12.1 prices a run at 10.5 calls on the assumption of one call per
        decision. This is what that assumption becomes here, and it is measured
        rather than set: an agent that never reaches for a tool measures 1.0,
        one that always spends its budget measures ``max_turns``, and the
        number between them is a property of the arm rather than of the
        configuration.
        """
        return self.calls / self.decisions if self.decisions else 0.0


@dataclass
class _Turns:
    """Accumulates one decision's cost across the turns it took.

    Separate from the loop so the accounting is stated once. A decision counts
    as a cache hit only when *every* turn was served from disk: a partially
    cached decision reached the network, and calling it a hit would flatter
    exactly the number §12.2's discount is judged on.
    """

    tokens_in: int = 0
    tokens_out: int = 0
    latency_ms: float = 0.0
    turns: int = 0
    cached: bool = True

    def absorb(self, result: Any) -> None:
        """Add one turn's usage. Called once per model call, before dispatch."""
        self.turns += 1
        self.tokens_in += result.usage.input_tokens + result.usage.cache_read_input_tokens
        self.tokens_out += result.usage.output_tokens
        self.latency_ms += result.latency_ms
        self.cached = self.cached and result.served_from_cache

    def decision(self, action: Action, coercion: str | None) -> Decision:
        """Close the tally into the boundary type the kernel reads."""
        return Decision(
            action=action,
            coercion=coercion,
            cache_hit=self.cached and self.turns > 0,
            tokens_in=self.tokens_in,
            tokens_out=self.tokens_out,
            latency_ms=int(self.latency_ms),
            from_model=True,
            model_turns=self.turns,
        )
