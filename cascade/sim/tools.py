"""The tool surface an agent may reach for inside a turn (ADR-0049).

Two tools, and the shape of each is decided by what it must *not* be able to do.

``lookup_evidence``
    Time-locked retrieval. ``as_of`` is bound by the caller from the run's own
    scenario cutoff and is **not a field of the argument model**: the model
    emits a payload that validates into :class:`LookupEvidenceArgs`, which
    carries one string and forbids every other key, so a payload carrying a
    date does not validate and an executor that wanted to read one would not
    type-check. That is invariant 1 held at a boundary the model is allowed to
    speak across, which is the only new boundary this module opens.
``recall``
    Search of the actor's own record. §6.3 keys a memory ``(run_id,
    actor_id)``; a session here is built from exactly the one memory string the
    kernel handed to this actor's turn, and there is no constructor, method or
    argument that takes two. An actor reading another actor's record would
    dissolve the information-asymmetry factor into a rounding error while every
    cell of the grid still ran and reported.

Everything here is pure. Retrieval is injected as :class:`EvidenceSearch`,
exactly as :class:`~cascade.decompose.compiler.Lathe` injects ``embed`` and
``retrieve``, so the tool loop is testable without a corpus and so this module
cannot acquire a connection, a clock or a generator of its own.

Tool schemas are **generated from the Pydantic models**. A hand-written schema
beside the model it mirrors drifts, and the drift here would be a model sending
a field the executor silently drops -- which for an argument named ``as_of`` is
the difference between a refusal and a leak nobody sees.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Annotated, Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

__all__ = [
    "LOOKUP_EVIDENCE",
    "LOOKUP_EVIDENCE_TOOL",
    "MAX_TURNS_CEILING",
    "QUERY_MAX_CHARS",
    "RECALL",
    "RECALL_TOOL",
    "TOOL_NAMES",
    "EvidenceHit",
    "EvidenceSearch",
    "LookupEvidenceArgs",
    "RecallArgs",
    "ToolBelt",
    "ToolPolicy",
    "ToolResult",
    "ToolSession",
    "UnknownTool",
    "tool_schemas",
]

LOOKUP_EVIDENCE = "lookup_evidence"
RECALL = "recall"

# Sorted, and the sort is load-bearing: this tuple orders the tool block in
# every request, and a set's iteration order would change the prompt bytes --
# and therefore the cache key -- between two processes that agree on
# everything else (invariant 7).
TOOL_NAMES: tuple[str, ...] = (LOOKUP_EVIDENCE, RECALL)

# A query is a search string, not a place to hide a payload. The cap is what
# makes a turn's request length bounded: the tool loop appends the query and
# its results to the conversation, so an unbounded query would grow the prompt
# without bound across `max_turns` turns and bill for it every turn.
QUERY_MAX_CHARS = 200

# A configuration guard, not a measurement. Every turn is a full call against
# the cached prefix, so the arm's per-decision cost is linear in `max_turns`;
# a typo that set it to 100 would multiply the ablation cell's spend by an
# order of magnitude and look like a slow run rather than a wrong one.
MAX_TURNS_CEILING = 16


class UnknownTool(ValueError):
    """A tool name that is not one of :data:`TOOL_NAMES`.

    Raised only by :func:`tool_schemas`, which is called with configuration
    rather than with model output: a name the operator typed wrong must fail
    at construction, where the experiment is still being set up. Model-supplied
    names are answered with a :class:`ToolResult`, never an exception -- one
    unusable tool call costs one actor one turn.
    """


# ---------------------------------------------------------------------------
# What the model may send
# ---------------------------------------------------------------------------


class _Args(BaseModel):
    """Base for every tool payload: closed, frozen, one field at a time.

    ``extra="forbid"`` is the mechanism, not a tidiness preference. An
    argument model that ignored unknown keys would accept
    ``{"query": ..., "as_of": "2030-01-01"}``, drop the second key, and answer
    the first -- which is a refusal that looks exactly like compliance from
    both sides. Forbidding extras turns the same payload into a validation
    error the agent is told about.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)


class LookupEvidenceArgs(_Args):
    """The whole of what an agent may say to the retriever.

    There is no time field here and there is no way to add one without
    changing this file: the time lock is bound to :class:`ToolBelt` at
    construction from the scenario's own cutoff, and
    :meth:`ToolSession.execute` passes ``args.query`` and nothing else. An
    executor reaching for ``args.as_of`` is a mypy error before it is a leak.
    """

    query: Annotated[str, Field(min_length=1, max_length=QUERY_MAX_CHARS)]


class RecallArgs(_Args):
    """A search of this actor's own record. Same closure, same reason.

    No ``actor_id``: a session is built from one actor's memory text, so there
    is no namespace for this payload to name even if it tried.
    """

    query: Annotated[str, Field(min_length=1, max_length=QUERY_MAX_CHARS)]


def _tool(name: str, description: str, model: type[BaseModel]) -> dict[str, Any]:
    """Build one Anthropic tool definition from the model that validates it.

    Preserves the property M4 bought with the same helper: the schema the
    model is held to and the schema the executor enforces are one object, so
    they cannot disagree about which fields exist -- and the field that must
    not exist is the one this whole module is about.
    """
    return {"name": name, "description": description, "input_schema": model.model_json_schema()}


LOOKUP_EVIDENCE_TOOL: dict[str, Any] = _tool(
    LOOKUP_EVIDENCE,
    (
        "Search the situation's evidence for documents relevant to a question you "
        "have. Only material published before the situation's cutoff exists; the "
        "cutoff is fixed for this simulation and is not something you can set, ask "
        "for, or find out by asking. Nothing published after it will ever be "
        "returned, whatever you write in the query."
    ),
    LookupEvidenceArgs,
)

RECALL_TOOL: dict[str, Any] = _tool(
    RECALL,
    (
        "Search your own record of this situation -- what you observed and what you "
        "did. It holds only your own steps; no other party's record is reachable "
        "from here."
    ),
    RecallArgs,
)

_TOOLS: tuple[tuple[str, dict[str, Any], type[_Args]], ...] = (
    (LOOKUP_EVIDENCE, LOOKUP_EVIDENCE_TOOL, LookupEvidenceArgs),
    (RECALL, RECALL_TOOL, RecallArgs),
)


def tool_schemas(allow: Sequence[str]) -> list[dict[str, Any]]:
    """The tool block for ``allow``, in :data:`TOOL_NAMES` order.

    Ordered by the module's own tuple rather than by the caller's sequence, so
    two configurations listing the same tools in different orders produce
    byte-identical requests and share a cache entry. Raises
    :class:`UnknownTool` on a name that does not exist, because a mistyped
    entry would otherwise hand the agent a smaller tool surface than the cell
    claims to be testing.
    """
    wanted = set(allow)
    unknown = sorted(wanted - set(TOOL_NAMES))
    if unknown:
        raise UnknownTool(
            f"no such tool(s): {unknown}; the tool surface is {list(TOOL_NAMES)}. "
            "A name that does not resolve would silently narrow the arm under test."
        )
    return [schema for name, schema, _model in _TOOLS if name in wanted]


# ---------------------------------------------------------------------------
# What comes back
# ---------------------------------------------------------------------------


class EvidenceHit(BaseModel):
    """One retrieved chunk, as the tool surface sees it.

    ``published_at`` is carried as a datetime rather than a rendered string so
    :class:`ToolBelt` can re-check the time lock in Python against the same
    value it renders. A string would make the backstop a lexical comparison
    between timestamps that may not share an offset.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    published_at: datetime
    source: str
    excerpt: str


class EvidenceSearch(Protocol):
    """The injected retrieval port. ``as_of`` is keyword-only and undefaulted.

    Mirrors :meth:`cascade.retrieval.search.Chronofence.search` rather than
    wrapping it, so this module needs no database and the invariant-1 signature
    rule reaches the port as well as the implementation: a stub written for a
    test cannot quietly default the cutoff either.
    """

    def __call__(self, query: str, *, as_of: datetime, k: int) -> Sequence[EvidenceHit]: ...


class ToolResult(BaseModel):
    """One tool call's outcome, in the shape the event log and the model share.

    A failure is a value here, never an exception: the agent is told what it
    did wrong and spends one of its turns, which is the same bargain ADR-0018
    struck for an inadmissible action. An exception would cost the run.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool: str
    ok: bool
    content: str
    """Rendered for the model. Bounded, deterministic, and never carries a date
    the time lock would have excluded."""
    n_results: int = Field(default=0, ge=0)
    dropped: int = Field(default=0, ge=0)
    """Hits the belt refused after the search returned them -- see
    :meth:`ToolBelt.admissible`. Zero against a correct retrieval port; a
    non-zero value means the port is leaking and the backstop caught it."""
    error: str | None = None
    """A stable code, not prose: it is aggregated across an ablation cell."""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class ToolPolicy(BaseModel):
    """The tool arm's configuration, validated at the boundary.

    Read from ``kernel.tools`` by :func:`cascade.sim.agent.tool_policy`, which
    is where the Settings dependency lives so this module keeps none.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = False
    max_turns: int = Field(default=1, ge=1, le=MAX_TURNS_CEILING)
    """Model turns per decision, including the one that emits the action. The
    last permitted turn is pinned to the action tool, so this is a hard bound
    on calls per decision and not a hope about model behaviour."""
    k_tool: int = Field(default=1, ge=1)
    """Chunks per ``lookup_evidence`` call. Separate from ``retrieval.k_agent``
    because ADR-0019 made that one a per-(scenario, actor) cost and this one is
    per tool call -- sharing the name would hide a per-turn cost inside a
    per-run budget."""
    allow: tuple[str, ...] = ()

    @field_validator("allow", mode="after")
    @classmethod
    def _known_and_ordered(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Reject an unknown tool and normalise the order.

        Normalising rather than rejecting an out-of-order list keeps the
        request bytes a function of the *set* of tools, so two operators who
        wrote the same configuration in different orders do not split the
        cache. Rejecting an unknown name is the opposite call, for the reason
        :func:`tool_schemas` gives.
        """
        unknown = sorted(set(value) - set(TOOL_NAMES))
        if unknown:
            raise ValueError(f"no such tool(s): {unknown}; the tool surface is {list(TOOL_NAMES)}")
        return tuple(name for name in TOOL_NAMES if name in set(value))


# ---------------------------------------------------------------------------
# The bound surface
# ---------------------------------------------------------------------------


class ToolBelt:
    """One actor's tool surface for one scenario, with the time lock bound in.

    Constructed per ``(scenario_id, actor_id)``, exactly as the cacheable
    prefix is, and given ``as_of`` keyword-only with no default (invariant 1).
    The cutoff is held privately and is never rendered into a tool result: an
    agent that could read its own lock could reason about what it is being kept
    from, which is a different experiment from the one the study runs.
    """

    def __init__(
        self,
        *,
        scenario_id: str,
        actor_id: str,
        as_of: datetime,
        search: EvidenceSearch,
        k: int,
        excerpt_chars: int,
        allow: Sequence[str] = TOOL_NAMES,
    ) -> None:
        if as_of.tzinfo is None:
            raise ValueError(
                f"as_of must be timezone-aware, got naive {as_of!r}; a naive cutoff is "
                "compared in whatever zone the process happens to be in and silently "
                "shifts the time lock"
            )
        if k <= 0:
            raise ValueError(f"k must be positive, got {k}")
        if excerpt_chars <= 0:
            raise ValueError(f"excerpt_chars must be positive, got {excerpt_chars}")
        unknown = sorted(set(allow) - set(TOOL_NAMES))
        if unknown:
            raise UnknownTool(f"no such tool(s): {unknown}; the surface is {list(TOOL_NAMES)}")
        self.scenario_id = scenario_id
        self.actor_id = actor_id
        self.allow: tuple[str, ...] = tuple(name for name in TOOL_NAMES if name in set(allow))
        self._as_of = as_of
        self._search = search
        self._k = k
        self._excerpt_chars = excerpt_chars

    def admissible(self, hits: Sequence[EvidenceHit]) -> tuple[tuple[EvidenceHit, ...], int]:
        """Drop anything at or after the bound cutoff; return what survives and how many did not.

        A backstop, not the mechanism: Chronofence is the time lock, and this
        cannot substitute for it because it only sees what was returned. It
        exists because the retrieval port is injected, so a future adapter --
        or a test stub -- could hand this module post-cutoff rows, and the
        failure direction of a boundary that trusts its input is a leak that
        reads as evidence. Naive timestamps are inadmissible for the same
        reason the constructor refuses a naive ``as_of``.
        """
        kept = tuple(
            hit
            for hit in hits
            if hit.published_at.tzinfo is not None and hit.published_at < self._as_of
        )
        return kept, len(hits) - len(kept)

    def session(self, *, memory: str) -> ToolSession:
        """Open the per-turn surface over this actor's own record.

        ``memory`` is the string the kernel rendered for *this* actor's turn
        (§6.3, one namespace keyed ``(run_id, actor_id)``). It is the only
        memory this session will ever see, and there is no method that takes a
        second one -- which is what keeps the information-asymmetry factor a
        mechanism rather than a label on a configuration.
        """
        return ToolSession(belt=self, memory=memory)


class ToolSession:
    """One turn's worth of tool calls, over one actor's record.

    Counts what it served. A multi-turn decision's cost is the sum of its
    turns, and M8 found that a count nobody carried is a count that silently
    becomes wrong.
    """

    def __init__(self, *, belt: ToolBelt, memory: str) -> None:
        self._belt = belt
        self._memory = memory
        self._used: list[str] = []
        self.dropped_post_cutoff = 0

    @property
    def used(self) -> tuple[str, ...]:
        """Tool names served this turn, in the order the model asked for them."""
        return tuple(self._used)

    def execute(self, name: str, payload: Any) -> ToolResult:
        """Run one model-requested tool call. Never raises for model input.

        ``payload`` is whatever the provider put in the tool-use block. It is
        validated into a closed model before anything reads it, so the only
        value that reaches the retriever from this side is a query string.
        """
        self._used.append(name)
        if name not in self._belt.allow:
            return ToolResult(
                tool=name,
                ok=False,
                content=(
                    f"There is no tool called {name!r} available to you. "
                    f"Available: {', '.join(self._belt.allow) or 'none'}."
                ),
                error=f"tool_not_enabled:{name}",
            )
        if name == LOOKUP_EVIDENCE:
            return self._lookup(payload)
        if name == RECALL:
            return self._recall(payload)
        return ToolResult(
            tool=name,
            ok=False,
            content=f"There is no tool called {name!r}.",
            error=f"unknown_tool:{name}",
        )

    # -- the two tools ------------------------------------------------------

    def _lookup(self, payload: Any) -> ToolResult:
        """Retrieve at the bound cutoff. ``args`` contributes a query and nothing else."""
        try:
            args = LookupEvidenceArgs.model_validate(payload)
        except ValidationError as exc:
            return _bad_arguments(LOOKUP_EVIDENCE, exc)
        # The only value crossing from model-supplied data into retrieval. The
        # cutoff and the breadth come from the belt, which the model never
        # touched; `args` has no field that could carry either.
        hits = self._belt._search(args.query, as_of=self._belt._as_of, k=self._belt._k)
        kept, dropped = self._belt.admissible(hits)
        self.dropped_post_cutoff += dropped
        return ToolResult(
            tool=LOOKUP_EVIDENCE,
            ok=True,
            content=_render_evidence(kept, excerpt_chars=self._belt._excerpt_chars),
            n_results=len(kept),
            dropped=dropped,
        )

    def _recall(self, payload: Any) -> ToolResult:
        """Search this actor's own record. The namespace is the session's, not the payload's."""
        try:
            args = RecallArgs.model_validate(payload)
        except ValidationError as exc:
            return _bad_arguments(RECALL, exc)
        lines = search_memory(self._memory, args.query)
        return ToolResult(
            tool=RECALL,
            ok=True,
            content=(
                "\n".join(lines)
                if lines
                else "(nothing in your own record matches that)"
            ),
            n_results=len(lines),
        )


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def search_memory(memory: str, query: str, *, limit: int = 8) -> tuple[str, ...]:
    """Rank one actor's own record lines by term overlap with ``query``.

    Deterministic by construction: the score is an integer count and ties break
    on the line's position in the record, so two processes handed the same
    record and the same query return the same lines in the same order -- which
    is what M8's byte-identical event-log hash requires of anything that
    reaches the prompt.

    Takes the record as text rather than as an :class:`~cascade.aperture.memory.AgentMemory`
    so that there is nothing here to point at a second actor's namespace.
    """
    terms = [term for term in _words(query) if len(term) > 2]
    lines = [line for line in memory.splitlines() if line.strip()]
    if not terms or not lines:
        return ()
    scored = [
        (-sum(1 for term in terms if term in _words(line)), index, line)
        for index, line in enumerate(lines)
    ]
    return tuple(line for score, _index, line in sorted(scored)[:limit] if score < 0)


def _words(text: str) -> set[str]:
    """Lowercase alphanumeric tokens. Used only for scoring, never for output."""
    cleaned = "".join(character if character.isalnum() else " " for character in text.lower())
    return set(cleaned.split())


def _bad_arguments(tool: str, exc: ValidationError) -> ToolResult:
    """Answer a malformed payload without naming the cutoff it may have tried to set.

    The message says what the tool accepts. It deliberately does not echo the
    rejected keys or state the time lock's value: an error that reported "as_of
    is not accepted, the cutoff is 2024-03-01" would hand the agent, through
    the refusal, the fact the refusal exists to withhold.
    """
    return ToolResult(
        tool=tool,
        ok=False,
        content=(
            f"{tool} takes exactly one field, `query`, a string of at most "
            f"{QUERY_MAX_CHARS} characters. Nothing else is accepted. Send the "
            "question you want answered and nothing more."
        ),
        error=f"bad_arguments:{exc.error_count()}",
    )


def _render_evidence(hits: Sequence[EvidenceHit], *, excerpt_chars: int) -> str:
    """Render retrieved chunks for the model, bounded and whitespace-normalised.

    Normalising whitespace and truncating each excerpt is what makes a tool
    result stable bytes: the result is appended to the conversation and becomes
    part of the next turn's cache key, so an excerpt whose line breaks depend
    on how a crawler wrapped the source would split the cache for two runs that
    retrieved the same document.
    """
    if not hits:
        return "(no admissible evidence was found before the cutoff)"
    return "\n\n".join(
        f"[{hit.published_at.isoformat()}] {hit.source}\n"
        + " ".join(hit.excerpt.split())[:excerpt_chars]
        for hit in hits
    )
