"""The tool surface an agent may reach for inside a turn (ADR-0049).

Two tools, and the shape of each is decided by what it must *not* be able to do.

``lookup_evidence``
    Time-locked retrieval. ``as_of`` is bound to a :class:`ToolBelt` by the
    caller, from the run's own scenario cutoff, and is **not a field of the
    argument model**: what the model emits validates into
    :class:`LookupEvidenceArgs`, which carries one string and forbids every
    other key. A payload naming a date does not validate, and an executor that
    wanted to read one would not type-check. ADR-0047 drew this line for the
    reranker -- a stage that cannot be handed ``as_of`` cannot default it --
    and the same shape is what makes a *model-supplied* payload safe here.

``recall``
    Search of the actor's own record. §6.3 keys a memory ``(run_id,
    actor_id)``; a :class:`ToolSession` is built from exactly the one memory
    string the kernel rendered for this actor's turn, and no constructor,
    method or argument in this module takes two. An actor that could read
    another's record would dissolve the information-asymmetry factor while
    every cell of the grid still ran and reported a number.

**Retrieved documents are quoted, not pasted.** A tool result arrives mid-
conversation in a block the provider labels as harness output, which is
precisely the frame the measured injection attack impersonated (20 of 30
scenarios obeyed a chunk addressing the model as the operator -- see
:mod:`cascade.quoting`). So a tool result goes through the same renderer the
cached prefix uses, with the same markers and the same stated count; there is
one way this system quotes a document and the newest arrival does not get its
own.

Everything here is pure: no connection, no clock, no generator. Retrieval is
injected as :class:`EvidenceSearch`, exactly as
:class:`~cascade.decompose.compiler.Lathe` injects ``embed`` and ``retrieve``.

Tool schemas are **generated from the Pydantic models**. A hand-written schema
beside the model that validates it drifts, and the drift here would be a model
sending a field the executor silently drops -- which, for a field named
``as_of``, is the difference between a refusal and a leak nobody sees.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Annotated, Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from cascade.quoting import quote_documents
from cascade.retrieval.queries import EvidenceQuery
from cascade.retrieval.schema import RetrievedChunk

__all__ = [
    "LOOKUP_EVIDENCE",
    "LOOKUP_EVIDENCE_TOOL",
    "QUERY_MAX_CHARS",
    "RECALL",
    "RECALL_LIMIT",
    "RECALL_TOOL",
    "TOOL_NAMES",
    "EvidenceSearch",
    "LookupEvidenceArgs",
    "PoolRetriever",
    "RecallArgs",
    "ToolBelt",
    "ToolResult",
    "ToolSession",
    "UnknownTool",
    "chronofence_evidence",
    "search_memory",
    "tool_evidence_query",
    "tool_schemas",
]

LOOKUP_EVIDENCE = "lookup_evidence"
RECALL = "recall"

# Sorted, and the sort is load-bearing. This tuple orders the tool block in
# every request the arm sends, so a set here would change the prompt bytes --
# and therefore the cache key -- between two processes that agree on
# everything else (invariant 7).
TOOL_NAMES: tuple[str, ...] = (LOOKUP_EVIDENCE, RECALL)

# A query is a search string, not somewhere to hide a payload. The cap is what
# keeps a turn's request bounded: the loop appends each query and its results
# to the conversation, so an unbounded query grows the prompt on every
# subsequent turn and is billed on each.
QUERY_MAX_CHARS = 200

# Lines one `recall` returns. The whole record is at most
# `kernel.memory.recent_observations` entries plus a summary line, so this is a
# bound on the answer rather than a sample of a larger store.
RECALL_LIMIT = 8


class UnknownTool(ValueError):
    """A tool name that is not one of :data:`TOOL_NAMES`.

    Raised only where the *operator* supplies the name -- ``kernel.tools.allow``
    and :func:`tool_schemas` -- so a typo fails while the experiment is still
    being set up rather than silently narrowing the arm under test. A name the
    *model* supplies is answered with a :class:`ToolResult`, never an
    exception: one unusable tool call costs one actor one turn, and the run has
    23 more steps in it (ADR-0018's bargain, one level up).
    """


# ---------------------------------------------------------------------------
# What the model may send
# ---------------------------------------------------------------------------


class _Args(BaseModel):
    """Base for every tool payload: closed, frozen, one field.

    ``extra="forbid"`` is the mechanism, not a tidiness preference. A model
    that ignored unknown keys would accept
    ``{"query": "...", "as_of": "2030-01-01"}``, drop the second key and answer
    the first -- a refusal indistinguishable from compliance, from both sides.
    Forbidding extras turns the same payload into a validation error the agent
    is told about and the event log counts.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)


class LookupEvidenceArgs(_Args):
    """The whole of what an agent may say to the retriever.

    There is no time field here and adding one is a change to this file. The
    lock is bound to :class:`ToolBelt` from the scenario's own cutoff, and
    :meth:`ToolBelt.lookup` takes a ``str``: there is no expression, in a
    payload or in the executor, that carries a cutoff from the model's side to
    the query. ``args.as_of`` is a mypy error before it is a leak.
    """

    query: Annotated[str, Field(min_length=1, max_length=QUERY_MAX_CHARS)]


class RecallArgs(_Args):
    """A search of this actor's own record. Same closure, same reason.

    No ``actor_id``: a session holds one actor's memory text, so this payload
    has no namespace to name even if it tried to.
    """

    query: Annotated[str, Field(min_length=1, max_length=QUERY_MAX_CHARS)]


def _tool(name: str, description: str, model: type[BaseModel]) -> dict[str, Any]:
    """Build one tool definition from the model that validates its payload.

    Preserves what M4 bought with the same helper: the schema the model is held
    to and the schema the executor enforces are one object, so they cannot come
    to disagree about which fields exist -- and the field that must not exist is
    what this module is about.
    """
    return {"name": name, "description": description, "input_schema": model.model_json_schema()}


LOOKUP_EVIDENCE_TOOL: dict[str, Any] = _tool(
    LOOKUP_EVIDENCE,
    (
        "Search the record for documents bearing on a question you have. Only "
        "material published before this situation's cutoff exists. The cutoff is "
        "fixed for the whole simulation: it is not a parameter of this tool, it "
        "cannot be set, moved or read, and nothing published after it will be "
        "returned however the query is worded."
    ),
    LookupEvidenceArgs,
)

RECALL_TOOL: dict[str, Any] = _tool(
    RECALL,
    (
        "Search your own record of this situation -- what you observed and what you "
        "did. It holds your own steps and nothing else; no other party's record is "
        "reachable from here."
    ),
    RecallArgs,
)

_TOOLS: tuple[tuple[str, dict[str, Any]], ...] = (
    (LOOKUP_EVIDENCE, LOOKUP_EVIDENCE_TOOL),
    (RECALL, RECALL_TOOL),
)


def tool_schemas(allow: Sequence[str]) -> list[dict[str, Any]]:
    """The tool block for ``allow``, in :data:`TOOL_NAMES` order.

    Ordered by this module's tuple rather than by the caller's sequence, so two
    configurations naming the same tools in different orders produce
    byte-identical requests and share cache entries instead of splitting them.
    Raises :class:`UnknownTool` on a name that does not resolve.
    """
    wanted = frozenset(allow)
    unknown = sorted(wanted - frozenset(TOOL_NAMES))
    if unknown:
        raise UnknownTool(
            f"no such tool(s): {unknown}; the surface is {list(TOOL_NAMES)}. A name that "
            "does not resolve would hand the agent a smaller surface than the cell claims "
            "to be measuring."
        )
    return [schema for name, schema in _TOOLS if name in wanted]


def allowed_tools(allow: Sequence[str]) -> tuple[str, ...]:
    """Normalise a configured ``allow`` list to :data:`TOOL_NAMES` order.

    Same reason as :func:`tool_schemas`: the order must be a function of the
    set, not of how the operator typed it, or two identical configurations
    address two caches.
    """
    wanted = frozenset(allow)
    unknown = sorted(wanted - frozenset(TOOL_NAMES))
    if unknown:
        raise UnknownTool(f"no such tool(s): {unknown}; the surface is {list(TOOL_NAMES)}")
    return tuple(name for name in TOOL_NAMES if name in wanted)


# ---------------------------------------------------------------------------
# What comes back
# ---------------------------------------------------------------------------


class ToolResult(BaseModel):
    """One tool call's outcome, in the shape the model and the accounting share.

    A failure is a value here and never an exception. The agent is told what it
    did wrong and spends one of its turns, which is the bargain ADR-0018 struck
    for an inadmissible action; an exception would spend the run.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool: str
    ok: bool
    content: str
    """Rendered for the model: bounded, whitespace-normalised, and quoted
    through :func:`cascade.quoting.quote_documents` when it carries documents."""
    n_results: int = Field(default=0, ge=0)
    dropped: int = Field(default=0, ge=0)
    """Chunks the belt refused after retrieval returned them -- see
    :meth:`ToolBelt.admissible`. Zero against Chronofence; a non-zero value
    means the injected port leaked and the backstop caught it."""
    error: str | None = None
    """A stable code, not prose: it is counted across an ablation cell."""


# ---------------------------------------------------------------------------
# The injected retrieval port
# ---------------------------------------------------------------------------


class EvidenceSearch(Protocol):
    """What a belt calls to retrieve. ``as_of`` is keyword-only and undefaulted.

    Mirrors the Chronofence signature rather than wrapping it, so this module
    needs no database and invariant 1's rule reaches the port as well as the
    implementation: a stub written for a test cannot quietly default the cutoff
    either, because there is nowhere in the signature to put a default.
    """

    def __call__(self, query: str, *, as_of: datetime, k: int) -> Sequence[RetrievedChunk]: ...


class PoolRetriever(Protocol):
    """The one method :func:`chronofence_evidence` is allowed to call.

    Structural rather than nominal so this module imports no part of
    :mod:`cascade.retrieval.search` -- but named ``retrieve`` on purpose. That
    is the single evidence entry point (ADR-0047): the retrieval mode and the
    rerank stage are decided there, so an arm that called ``search`` instead
    would read the corpus differently from the prefix arm it is being compared
    against, and the measured difference would be retrieval rather than tools.
    """

    def retrieve(
        self,
        vector: Sequence[float],
        *,
        text: str,
        entities: Sequence[str] = (),
        as_of: datetime,
        k: int,
    ) -> Any: ...


class EmbedQuery(Protocol):
    """Embeds one query string into the pinned 384-dimension space."""

    def __call__(self, text: str) -> Sequence[float]: ...


def tool_evidence_query(query: str) -> EvidenceQuery:
    """Build the two query forms from an agent's free text.

    The other three evidence queries (§5.2's compiler, ADR-0019's per-actor
    prefix, §10.2's baselines) are built in
    :mod:`cascade.retrieval.queries` from fields the study owns, and can
    therefore separate an entity from its surrounding prose. A tool query is a
    sentence the model wrote: there is no field to lift a party name out of, so
    the same text is offered to both pools and no entity is claimed. Claiming
    one would mean guessing at proper nouns and handing the keyword pool a term
    the agent never asked for.
    """
    text = " ".join(query.split())
    return EvidenceQuery(text=text, keyword_text=text)


def chronofence_evidence(*, fence: PoolRetriever, embed: EmbedQuery) -> EvidenceSearch:
    """Bind a Chronofence to the tool port. The adapter, and the only one.

    Written here rather than at the call site so that the tool arm provably
    goes through ``retrieve`` -- the mode-and-rerank entry point -- rather than
    through ``search``. The caller supplies the fence and the embedder; what
    this closure adds is that ``as_of`` arrives as an argument from the belt
    and is never read from anywhere else.
    """

    def search(query: str, *, as_of: datetime, k: int) -> tuple[RetrievedChunk, ...]:
        evidence = tool_evidence_query(query)
        found = fence.retrieve(
            embed(evidence.text),
            text=evidence.keyword_text,
            entities=evidence.entities,
            as_of=as_of,
            k=k,
        )
        return tuple(found.chunks)

    return search


# ---------------------------------------------------------------------------
# The bound surface
# ---------------------------------------------------------------------------


class ToolBelt:
    """One actor's tool surface for one scenario, with the time lock bound in.

    Built per ``(scenario_id, actor_id)``, exactly as the cacheable prefix is,
    and given ``as_of`` keyword-only with no default (invariant 1). The cutoff
    is held privately and never rendered into a tool result or an error
    message: an agent that could read its own lock could reason about what it
    is being kept from, which is a different experiment from the one the study
    is running.
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
                "compared in whatever zone the process happens to run in and silently "
                "shifts the time lock"
            )
        if k <= 0:
            raise ValueError(f"k must be positive, got {k}")
        if excerpt_chars <= 0:
            raise ValueError(f"excerpt_chars must be positive, got {excerpt_chars}")
        self.scenario_id = scenario_id
        self.actor_id = actor_id
        self.allow = allowed_tools(allow)
        self._as_of = as_of
        self._search = search
        self._k = k
        self._excerpt_chars = excerpt_chars

    def admissible(
        self, chunks: Sequence[RetrievedChunk]
    ) -> tuple[tuple[RetrievedChunk, ...], int]:
        """Drop anything at or after the bound cutoff; report what survived and what did not.

        A backstop, not the mechanism. Chronofence is the time lock and this
        cannot substitute for it, because it only ever sees what was already
        returned. It exists because the retrieval port is *injected*: a future
        adapter, or a stub written for a test, could hand this module
        post-cutoff rows, and the failure direction of a boundary that trusts
        its input is a leak that reads as evidence. The leakage suite asserts
        the count is zero against the real corpus and non-zero against a stub
        built to leak, so the backstop is known to fire.

        A naive timestamp is inadmissible for the same reason the constructor
        refuses a naive ``as_of``: it cannot be compared, and the comparison is
        the whole guarantee.
        """
        kept = tuple(
            chunk
            for chunk in chunks
            if chunk.published_at.tzinfo is not None and chunk.published_at < self._as_of
        )
        return kept, len(chunks) - len(kept)

    def lookup(self, query: str) -> ToolResult:
        """Retrieve at the bound cutoff. ``query`` is the only model-supplied value.

        Preserves invariant 1 at the one boundary this module opens to the
        model: the cutoff and the breadth come from the belt, which nothing the
        model emitted has touched, and the argument model has no field that
        could carry either.
        """
        chunks, dropped = self.admissible(self._search(query, as_of=self._as_of, k=self._k))
        return ToolResult(
            tool=LOOKUP_EVIDENCE,
            ok=True,
            content=quote_documents(
                tuple(
                    (chunk.published_at.isoformat(), chunk.source, chunk.body) for chunk in chunks
                ),
                excerpt_chars=self._excerpt_chars,
                empty="(no admissible evidence was found before the cutoff)",
            ),
            n_results=len(chunks),
            dropped=dropped,
        )

    def session(self, *, memory: str) -> ToolSession:
        """Open the per-turn surface over this actor's own record.

        ``memory`` is the string the kernel rendered for *this* actor's turn
        (§6.3: one namespace, keyed ``(run_id, actor_id)``). It is the only
        record this session will ever hold, and there is no method here that
        takes a second -- which is what keeps information asymmetry a mechanism
        rather than a label on a configuration field.
        """
        return ToolSession(belt=self, memory=memory)


class ToolSession:
    """One turn's tool calls, over one actor's record.

    Counts what it served, because a multi-turn decision's cost is the sum of
    its turns and M8 found that a count nobody carries is a count that
    silently becomes wrong.
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
        """Run one model-requested tool call. Never raises on model input.

        ``payload`` is whatever the provider put in the tool-use block. It is
        validated into a closed model before anything reads it, so the only
        value that crosses from here into retrieval is a query string.
        """
        self._used.append(name)
        if name not in self._belt.allow:
            return ToolResult(
                tool=name,
                ok=False,
                content=(
                    f"There is no tool called {name!r} available to you. Available: "
                    f"{', '.join(self._belt.allow) or 'none'}."
                ),
                error=f"tool_not_enabled:{name}",
            )
        if name == LOOKUP_EVIDENCE:
            try:
                lookup = LookupEvidenceArgs.model_validate(payload)
            except ValidationError as exc:
                return _bad_arguments(LOOKUP_EVIDENCE, exc)
            result = self._belt.lookup(lookup.query)
            self.dropped_post_cutoff += result.dropped
            return result
        try:
            recall = RecallArgs.model_validate(payload)
        except ValidationError as exc:
            return _bad_arguments(RECALL, exc)
        lines = search_memory(self._memory, recall.query)
        return ToolResult(
            tool=RECALL,
            ok=True,
            content="\n".join(lines) if lines else "(nothing in your own record matches that)",
            n_results=len(lines),
        )


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def search_memory(memory: str, query: str, *, limit: int = RECALL_LIMIT) -> tuple[str, ...]:
    """Rank one actor's own record lines by term overlap with ``query``.

    Deterministic by construction: the score is an integer count of shared
    terms and ties break on the line's position in the record, so two processes
    handed the same record and the same query return the same lines in the same
    order. Anything that reaches the prompt has to have that property or M8's
    byte-identical event-log hash is not available to this arm.

    Takes the record as text rather than as an
    :class:`~cascade.aperture.memory.AgentMemory`, so there is nothing here
    that could be pointed at a second actor's namespace.
    """
    terms = sorted(term for term in _words(query) if len(term) > 2)
    lines = [line for line in memory.splitlines() if line.strip()]
    if not terms or not lines:
        return ()
    scored = sorted(
        (-sum(1 for term in terms if term in _words(line)), index, line)
        for index, line in enumerate(lines)
    )
    return tuple(line for score, _index, line in scored[:limit] if score < 0)


def _words(text: str) -> frozenset[str]:
    """Lowercase alphanumeric tokens. Used for scoring only, never for output."""
    cleaned = "".join(character if character.isalnum() else " " for character in text.lower())
    return frozenset(cleaned.split())


def _bad_arguments(tool: str, exc: ValidationError) -> ToolResult:
    """Answer a malformed payload without naming the lock it may have tried to move.

    The message states what the tool accepts. It deliberately does not echo the
    rejected keys or the cutoff's value: an error reading "as_of is not
    accepted, the cutoff is 2024-03-01" would hand the agent, through the
    refusal itself, the fact the refusal exists to withhold.
    """
    return ToolResult(
        tool=tool,
        ok=False,
        content=(
            f"{tool} takes exactly one field, `query`, a string of at most "
            f"{QUERY_MAX_CHARS} characters. Nothing else is accepted. Send the question "
            "you want answered and nothing more."
        ),
        error=f"bad_arguments:{exc.error_count()}",
    )
