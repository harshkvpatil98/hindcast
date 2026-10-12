"""The scenario dossier: one cited situation report per scenario (ADR-0037).

Until this module the compiler saw the 60 chunks nearest the question and each
agent saw six, and nothing else. A scenario whose corpus holds ten thousand
admissible chunks used the same six as one that holds sixty. The dossier
spends evidence where it exists: many queries draw a wide pool, one model call
reads all of it, and the result -- a dated timeline, stated positions, the
state of play at the cutoff, and what the evidence leaves open -- travels in
the compiler's prompt and in every agent's cached prefix.

**What this adds is a laundering channel, and the module is built around
closing it.** The writer model carries parametric knowledge, for some
scenarios including how they ended. A free-text summary would let that
knowledge reach the agents dressed as retrieved fact, which is strictly worse
than the agents' own memory: the memorisation probe measures that, and
nothing measures this. So the dossier is not free text. Every claim cites the
numbered excerpts it rests on, and :func:`verify` -- pure, no model -- drops
any claim whose names, numbers and wording the cited excerpts do not carry.
The excerpts come through Chronofence, so they are pre-cutoff by
construction; a claim that survives is one the pre-cutoff record supports.
Dropped claims are kept and counted, because a writer that keeps trying to
say things its sources do not is itself a measurement.

Pure core, thin shell: pooling, verification, rendering and hashing take no
clock, no RNG and do no I/O. :class:`DossierWriter` is the shell.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from cascade.canonical import canonical_json, canonical_timestamp
from cascade.config import Settings
from cascade.ledger.schema import Scenario
from cascade.llm.types import LLMRequest

__all__ = [
    "DOSSIER_TOOL",
    "SECTIONS",
    "SYSTEM_PROMPT",
    "Claim",
    "Dossier",
    "DossierDraft",
    "DossierWriter",
    "DroppedClaim",
    "Excerpt",
    "Hit",
    "WriteOutcome",
    "dossier_hash",
    "pool_evidence",
    "queries_for",
    "render",
    "user_prompt",
    "verify",
]

Section = Literal["timeline", "positions", "status", "unknowns"]
SECTIONS: tuple[Section, ...] = ("timeline", "positions", "status", "unknowns")

_SECTION_TITLES: Mapping[str, str] = {
    "timeline": "What happened, in order",
    "positions": "Who has said they want what",
    "status": "Where things stood at the cutoff",
    "unknowns": "What the record leaves open",
}


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Excerpt(_Frozen):
    """One pre-cutoff chunk offered to the writer, under the number it cites.

    ``published_at`` is carried so :func:`verify` can re-assert the time lock
    on the pool itself rather than trusting the retrieval layer, and so a claim
    may lean on the document's own date ("on 12 March ...") when the body says
    only "on Tuesday".
    """

    index: int = Field(ge=1)
    chunk_id: str
    document_id: str
    published_at: datetime
    source: str
    body: str


class Claim(_Frozen):
    """One sentence of the report and the excerpts that state it."""

    text: str = Field(min_length=12, max_length=400)
    cites: tuple[int, ...] = Field(min_length=1, max_length=6)
    on: str | None = Field(
        default=None,
        pattern=r"^\d{4}-\d{2}(-\d{2})?$",
        description="Date of the event, YYYY-MM-DD or YYYY-MM, as the excerpts give it.",
    )


class DossierDraft(BaseModel):
    """What the writer emits. The tool schema is generated from this model, so
    the shape the model is held to and the shape :func:`verify` reads cannot
    drift (the same rule the compiler's tools follow)."""

    model_config = ConfigDict(extra="forbid")

    timeline: list[Claim] = Field(
        default_factory=list,
        description="Dated events relevant to the question, oldest first. Set `on`.",
    )
    positions: list[Claim] = Field(
        default_factory=list,
        description="What named parties have said they want, oppose, or will do.",
    )
    status: list[Claim] = Field(
        default_factory=list,
        description=(
            "The state of play in the most recent excerpts, including scheduled "
            "upcoming events the excerpts announce."
        ),
    )
    unknowns: list[Claim] = Field(
        default_factory=list,
        description="Questions the excerpts explicitly raise and do not answer.",
    )


class DroppedClaim(_Frozen):
    """A claim :func:`verify` refused, with the reason. Kept for the audit."""

    section: Section
    text: str
    reason: str


class Dossier(_Frozen):
    """The verified report. This, canonically serialised, is what is hashed."""

    scenario_id: str
    as_of: datetime
    timeline: tuple[Claim, ...]
    positions: tuple[Claim, ...]
    status: tuple[Claim, ...]
    unknowns: tuple[Claim, ...]
    evidence: tuple[tuple[int, str, str], ...]
    """(index, chunk_id, published_iso) for every excerpt offered -- the pool
    the claims cite into, so a citation stays resolvable after the fact."""

    def section(self, name: Section) -> tuple[Claim, ...]:
        claims: tuple[Claim, ...] = getattr(self, name)
        return claims

    @property
    def n_claims(self) -> int:
        return sum(len(self.section(name)) for name in SECTIONS)

    def canonical(self) -> dict[str, Any]:
        """The dict the hash is taken over: every field, timestamps canonical."""
        return {
            "scenario_id": self.scenario_id,
            "as_of": canonical_timestamp(self.as_of),
            "evidence": [list(item) for item in self.evidence],
            **{
                name: [claim.model_dump(mode="json") for claim in self.section(name)]
                for name in SECTIONS
            },
        }


def dossier_hash(dossier: Dossier) -> str:
    """Content hash of a dossier, stable across processes and emission order
    of dict keys -- the same construction as ``graph_hash`` so a stored row is
    checkable against what it claims to be."""
    return hashlib.sha256(canonical_json(dossier.canonical()).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Pooling
# ---------------------------------------------------------------------------


def queries_for(scenario: Scenario, *, max_party_queries: int) -> tuple[str, ...]:
    """The retrieval queries for one scenario: a pure function of its text.

    Preserves outcome-independence: the question, the resolution criterion and
    the registry's party names are all fixed before resolution, so which
    evidence the writer sees cannot depend on how the question ended.
    """
    queries = [scenario.question, f"{scenario.question} {scenario.resolution_criterion}"]
    for name in sorted(scenario.party_names)[:max_party_queries]:
        queries.append(f"{name} {scenario.question}")
    seen: set[str] = set()
    unique: list[str] = []
    for query in queries:
        key = " ".join(query.split()).casefold()
        if key not in seen:
            seen.add(key)
            unique.append(" ".join(query.split()))
    return tuple(unique)


class Hit(_Frozen):
    """The fields of a retrieved chunk that pooling needs."""

    chunk_id: str
    document_id: str
    published_at: datetime
    source: str
    body: str


def pool_evidence(
    ranked_lists: Sequence[Sequence[Hit]],
    *,
    as_of: datetime,
    limit: int,
    max_per_document: int,
    excerpt_chars: int,
) -> tuple[Excerpt, ...]:
    """Merge several ranked result lists into one numbered, time-ordered pool.

    Preserves three things. The time lock: a hit with ``published_at >= as_of``
    is a retrieval-layer defect and raises rather than being filtered, because
    filtering would hide it. Determinism: lists are interleaved by rank
    (every list's best hit before any list's second), ties between lists
    broken by list order and duplicates by first appearance, so the pool is a
    function of its inputs alone. Breadth: no document contributes more than
    ``max_per_document`` chunks, so one long article cannot crowd out the rest.

    The pool is numbered in publication order so that the writer reads a
    chronology and a citation index carries a rough date.
    """
    chosen: list[Hit] = []
    seen_chunks: set[str] = set()
    per_document: dict[str, int] = {}
    depth = max((len(hits) for hits in ranked_lists), default=0)
    for rank in range(depth):
        for hits in ranked_lists:
            if rank >= len(hits) or len(chosen) >= limit:
                continue
            hit = hits[rank]
            if hit.published_at >= as_of:
                raise ValueError(
                    f"chunk {hit.chunk_id} is dated {hit.published_at.isoformat()}, not before "
                    f"the cutoff {as_of.isoformat()}: the time lock failed upstream"
                )
            if hit.chunk_id in seen_chunks:
                continue
            if per_document.get(hit.document_id, 0) >= max_per_document:
                continue
            seen_chunks.add(hit.chunk_id)
            per_document[hit.document_id] = per_document.get(hit.document_id, 0) + 1
            chosen.append(hit)
    ordered = sorted(chosen, key=lambda hit: (hit.published_at, hit.chunk_id))
    return tuple(
        Excerpt(
            index=index,
            chunk_id=hit.chunk_id,
            document_id=hit.document_id,
            published_at=hit.published_at,
            source=hit.source,
            body=hit.body.strip()[:excerpt_chars],
        )
        for index, hit in enumerate(ordered, start=1)
    )


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

# Hyphens split: "Microsoft-Activision" must be findable as "Microsoft".
_WORD = re.compile(r"[A-Za-z][A-Za-z'\u2019]*|\d[\d,]*(?:\.\d+)?")
_MONTHS = [
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
]

# Function words and reporting verbs: present in any sentence about anything,
# so their presence in a cited excerpt is no evidence the excerpt says it.
_STOPWORDS = frozenset(
    [
        "a",
        "about",
        "above",
        "after",
        "again",
        "against",
        "all",
        "also",
        "although",
        "among",
        "an",
        "and",
        "any",
        "are",
        "as",
        "at",
        "be",
        "because",
        "been",
        "before",
        "being",
        "below",
        "between",
        "both",
        "but",
        "by",
        "can",
        "could",
        "did",
        "do",
        "does",
        "doing",
        "down",
        "during",
        "each",
        "either",
        "few",
        "for",
        "from",
        "further",
        "had",
        "has",
        "have",
        "having",
        "he",
        "her",
        "here",
        "hers",
        "him",
        "his",
        "how",
        "however",
        "i",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "itself",
        "just",
        "may",
        "me",
        "might",
        "more",
        "most",
        "much",
        "must",
        "my",
        "neither",
        "no",
        "nor",
        "not",
        "now",
        "of",
        "off",
        "on",
        "once",
        "one",
        "only",
        "or",
        "other",
        "our",
        "ours",
        "out",
        "over",
        "own",
        "per",
        "said",
        "same",
        "say",
        "says",
        "she",
        "should",
        "since",
        "so",
        "some",
        "such",
        "than",
        "that",
        "the",
        "their",
        "theirs",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "those",
        "through",
        "to",
        "too",
        "under",
        "until",
        "up",
        "upon",
        "very",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "whether",
        "which",
        "while",
        "who",
        "whom",
        "whose",
        "why",
        "will",
        "with",
        "within",
        "without",
        "would",
        "yet",
        "you",
        "your",
        "according",
    ]
)


def _normalise(token: str) -> str:
    token = token.casefold().replace("\u2019", "'")
    if token.endswith("'s"):
        token = token[:-2]
    token = token.strip("'-")
    if token[:1].isdigit():
        token = token.replace(",", "")
        if "." in token:
            token = token.rstrip("0").rstrip(".")
    return token


def _stem(token: str) -> str:
    """A deliberately crude stem: enough that "announced" finds "announces".

    Crude on purpose. A real stemmer is a dependency, and the failure direction
    of a crude one is a *dropped* claim, which is the safe direction here.
    """
    if token[:1].isdigit() or len(token) <= 5:
        return token
    for suffix in ("ations", "ation", "ments", "ment", "ings", "ing", "ies", "ed", "es", "s"):
        if token.endswith(suffix) and len(token) - len(suffix) >= 4:
            return token[: -len(suffix)]
    return token


def _tokens(text: str) -> list[str]:
    return [token for token in (_normalise(raw) for raw in _WORD.findall(text)) if token]


def _date_tokens(moment: datetime) -> set[str]:
    """Tokens a document's own publication date lends to claims citing it."""
    return {str(moment.year), _MONTHS[moment.month - 1], str(moment.day)}


def _anchors(text: str) -> list[str]:
    """The tokens of a claim that assert something checkable: every number and
    every proper name.

    A capitalised word is a name unless it is merely opening a sentence. The
    opening word still counts when it is an acronym, a month, or the first
    word of a capitalised run ("Lina Khan ...", "European Commission ..."),
    because those are names wherever they stand.
    """
    matches = list(_WORD.finditer(text))
    anchors: list[str] = []
    for position, match in enumerate(matches):
        raw = match.group(0)
        token = _normalise(raw)
        if not token:
            continue
        if raw[0].isdigit():
            anchors.append(token)
            continue
        acronym = len(raw) >= 2 and raw.isupper()
        if not raw[0].isupper() or token in _STOPWORDS or (len(token) < 3 and not acronym):
            continue
        opens_sentence = position == 0 or text[: match.start()].rstrip().endswith((".", ":"))
        if opens_sentence and not acronym and token not in _MONTHS:
            run = False
            if position + 1 < len(matches):
                following = matches[position + 1]
                gap = text[match.end() : following.start()]
                run = gap == " " and following.group(0)[0].isupper()
            if not run:
                continue
        anchors.append(token)
    return anchors


def _check(
    claim: Claim,
    *,
    section: Section,
    by_index: Mapping[int, Excerpt],
    scenario_tokens: frozenset[str],
    as_of: datetime,
    min_support_ratio: float,
) -> str | None:
    """Return why a claim is refused, or ``None`` when the record carries it."""
    missing = [index for index in claim.cites if index not in by_index]
    if missing:
        return f"bad_citation:{','.join(str(index) for index in sorted(missing))}"

    if section == "timeline":
        if claim.on is None:
            return "undated_timeline_entry"
        # The cutoff is an instant and `on` is a calendar date, so an event
        # dated on the cutoff's own day cannot be shown to precede it: refused.
        # A month-precision date is refused only when the whole month is later;
        # within the cutoff's month the support check below has to carry it.
        cutoff_day = as_of.astimezone(UTC).strftime("%Y-%m-%d")
        if len(claim.on) == 10 and claim.on >= cutoff_day:
            return f"dated_after_cutoff:{claim.on}"
        if len(claim.on) == 7 and claim.on > cutoff_day[:7]:
            return f"dated_after_cutoff:{claim.on}"

    support: set[str] = set(scenario_tokens)
    for index in claim.cites:
        excerpt = by_index[index]
        support.update(_tokens(excerpt.body))
        support.update(_date_tokens(excerpt.published_at))
    stems = {_stem(token) for token in support}

    # The year of `on` is an assertion like any other number in the claim; its
    # month and day are not held to the same test, because an excerpt that
    # says "in March" carries "2026-03" without containing "03".
    anchors = _anchors(claim.text) + ([claim.on[:4]] if claim.on else [])
    for anchor in anchors:
        if anchor not in support and _stem(anchor) not in stems:
            return f"unsupported:{anchor}"

    content = [
        token for token in _tokens(claim.text) if token not in _STOPWORDS and len(token) >= 4
    ]
    if content:
        carried = sum(1 for token in content if token in support or _stem(token) in stems)
        ratio = carried / len(content)
        if ratio < min_support_ratio:
            return f"weak_support:{ratio:.2f}"
    return None


def verify(
    draft: DossierDraft,
    *,
    scenario: Scenario,
    excerpts: Sequence[Excerpt],
    min_support_ratio: float,
    max_claims_per_section: int,
) -> tuple[Dossier, tuple[DroppedClaim, ...]]:
    """Keep the claims the cited excerpts carry; return the rest with reasons.

    Preserves the property the dossier exists under: nothing reaches a prompt
    as "what was known before the cutoff" unless pre-cutoff text says it. Each
    claim must cite excerpts that exist; a timeline entry must be dated before
    the cutoff; every number and proper name in the claim must occur in the
    cited excerpts, the documents' own dates, or the scenario's own wording;
    and at least ``min_support_ratio`` of its content words must too.

    The rules are lexical and therefore conservative in one direction only: a
    true paraphrase can be dropped, an unsupported name or number cannot pass.
    They do not read meaning, so a hindsight-coloured *verb* over supported
    nouns can survive -- ADR-0037 records that as the residual risk and names
    the measurement that bounds it.

    Pure: no model, no clock, no I/O. Sections keep emission order; a section
    longer than ``max_claims_per_section`` is cut from the end and the cut
    claims are reported, not silently lost.
    """
    for excerpt in excerpts:
        if excerpt.published_at >= scenario.cutoff_ts:
            raise ValueError(
                f"excerpt [{excerpt.index}] is dated {excerpt.published_at.isoformat()}, not "
                f"before the cutoff {scenario.cutoff_ts.isoformat()}"
            )
    by_index = {excerpt.index: excerpt for excerpt in excerpts}
    scenario_tokens = frozenset(
        _tokens(
            " ".join(
                (scenario.question, scenario.resolution_criterion, *sorted(scenario.party_names))
            )
        )
    )

    kept: dict[str, tuple[Claim, ...]] = {}
    dropped: list[DroppedClaim] = []
    for section in SECTIONS:
        accepted: list[Claim] = []
        seen: set[str] = set()
        claims: list[Claim] = getattr(draft, section)
        for claim in claims:
            key = " ".join(_tokens(claim.text))
            if key in seen:
                dropped.append(DroppedClaim(section=section, text=claim.text, reason="duplicate"))
                continue
            seen.add(key)
            reason = _check(
                claim,
                section=section,
                by_index=by_index,
                scenario_tokens=scenario_tokens,
                as_of=scenario.cutoff_ts,
                min_support_ratio=min_support_ratio,
            )
            if reason is None and len(accepted) >= max_claims_per_section:
                reason = "over_section_limit"
            if reason is None:
                accepted.append(claim)
            else:
                dropped.append(DroppedClaim(section=section, text=claim.text, reason=reason))
        if section == "timeline":
            accepted.sort(key=lambda claim: (claim.on or "", claim.text))
        kept[section] = tuple(accepted)

    dossier = Dossier(
        scenario_id=scenario.scenario_id,
        as_of=scenario.cutoff_ts,
        timeline=kept["timeline"],
        positions=kept["positions"],
        status=kept["status"],
        unknowns=kept["unknowns"],
        evidence=tuple(
            (excerpt.index, excerpt.chunk_id, canonical_timestamp(excerpt.published_at))
            for excerpt in excerpts
        ),
    )
    return dossier, tuple(dropped)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render(dossier: Dossier, *, max_chars: int) -> str:
    """Render a dossier for a prompt, within a fixed character budget.

    Preserves prefix stability: the same dossier and budget give the same
    bytes, which is what the agents' cached prefix is worth (ADR-0019).
    Citation numbers are not rendered -- they index a pool the reader never
    sees -- but dates are, because recency is most of what a reader needs.

    Over budget, the *oldest* timeline entries go first, then trailing
    unknowns, positions and status lines in that order: what happened last and
    where things stand is the part a forecaster cannot do without. Returns the
    empty string for an empty dossier, so callers can omit the section.
    """
    lines: dict[str, list[str]] = {
        name: [
            f"- {claim.on}: {claim.text}" if claim.on else f"- {claim.text}"
            for claim in dossier.section(name)
        ]
        for name in SECTIONS
    }

    def assemble() -> str:
        blocks = [
            f"## {_SECTION_TITLES[name]}\n" + "\n".join(lines[name])
            for name in SECTIONS
            if lines[name]
        ]
        return "\n\n".join(blocks)

    text = assemble()
    for name, from_front in (
        ("timeline", True),
        ("unknowns", False),
        ("positions", False),
        ("status", False),
    ):
        while len(text) > max_chars and lines[name]:
            lines[name].pop(0 if from_front else -1)
            text = assemble()
    return text if len(text) <= max_chars else ""


# ---------------------------------------------------------------------------
# The writer's prompt
# ---------------------------------------------------------------------------

DOSSIER_TOOL_NAME = "emit_dossier"


def _tool() -> dict[str, Any]:
    return {
        "name": DOSSIER_TOOL_NAME,
        "description": (
            "Emit the situation report. Every claim cites the numbered excerpts that "
            "state it; a claim no excerpt states must not be emitted."
        ),
        "input_schema": DossierDraft.model_json_schema(),
    }


DOSSIER_TOOL = _tool()

SYSTEM_PROMPT = """\
You prepare situation reports for analysts who will forecast a question. You are \
given numbered excerpts from documents, every one of them published before a \
stated cutoff, and you report what those excerpts say that bears on the question.

The excerpts are your only source. You may know more about this situation than \
they contain -- including, possibly, how it later turned out. For this task that \
knowledge does not exist. The analysts are being tested on what could be known \
at the cutoff, and a single detail from after it invalidates their test without \
anyone being able to tell. If the excerpts do not say it, leave it out, however \
sure you are.

Rules.
1. Every claim cites the excerpt numbers that state it. Do not cite an excerpt \
for something it merely makes plausible.
2. Stay close to the excerpts' own words for names, dates, figures and what \
each party said. Do not round, convert or infer figures.
3. Report; do not predict and do not assess likelihood. If an excerpt reports \
someone else's forecast, poll or market price, you may report that as what it \
is, with its date.
4. Timeline entries carry the date the excerpt gives for the event (`on`), not \
the publication date unless they coincide. Events scheduled for after the \
cutoff belong under status, as announcements, never in the timeline.
5. Prefer the specific and the recent: what was decided, filed, announced, \
scheduled or said, by whom, when. Skip background any reader would know.
6. Many excerpts will be irrelevant. Ignore them. If nothing bears on the \
question, return empty sections; an empty report is a correct answer.
7. Keep each claim to one sentence."""


def user_prompt(scenario: Scenario, excerpts: Sequence[Excerpt]) -> str:
    """The per-scenario message: the question, the cutoff, the numbered pool."""
    parties = ", ".join(sorted(scenario.party_names)) or "(none recorded)"
    if excerpts:
        pool = "\n\n".join(
            f"[{excerpt.index}] {excerpt.published_at.date().isoformat()} · {excerpt.source}\n"
            f"{excerpt.body}"
            for excerpt in excerpts
        )
    else:
        pool = "(no pre-cutoff excerpts were retrievable)"
    return (
        f"# Question\n{scenario.question}\n\n"
        f"# Resolution criterion\n{scenario.resolution_criterion}\n\n"
        f"# Cutoff\n{canonical_timestamp(scenario.cutoff_ts)} — every excerpt predates this "
        "instant, and nothing after it may appear in the report.\n\n"
        f"# Parties named in the registry\n{parties}\n\n"
        f"# Excerpts\n{pool}\n\n"
        f"Emit the report with the {DOSSIER_TOOL_NAME} tool."
    )


# ---------------------------------------------------------------------------
# The shell
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WriteOutcome:
    """What writing one scenario's dossier produced."""

    scenario_id: str
    dossier: Dossier
    dossier_sha256: str
    dropped: tuple[DroppedClaim, ...]
    n_excerpts: int
    llm_calls: int


Search = Callable[[str, datetime, int], Sequence[Hit]]
"""(query, as_of, k) -> ranked hits. ``as_of`` is positional and required:
there is no default to forget (invariant 1)."""


@dataclass
class DossierWriter:
    """Pools evidence, asks the model once, verifies what comes back.

    ``search`` is injected, like the compiler's ``retrieve``, so the whole path
    runs against a stub corpus and recorded responses. The model call goes
    through :class:`~cascade.llm.client.LLMClient` (invariant 5): recorded,
    replayable, and metered against the ``dossier`` phase ceiling.
    """

    settings: Settings
    client: Any
    search: Search

    def write(self, scenario: Scenario) -> WriteOutcome:
        """Write one scenario's dossier. An empty pool yields an empty dossier
        without a model call: there is nothing to report and no reason to pay
        a model to say so."""
        config = self.settings.dossier
        ranked = [
            list(self.search(query, scenario.cutoff_ts, config.per_query_k))
            for query in queries_for(scenario, max_party_queries=config.max_party_queries)
        ]
        excerpts = pool_evidence(
            ranked,
            as_of=scenario.cutoff_ts,
            limit=config.pool_chunks,
            max_per_document=config.max_per_document,
            excerpt_chars=config.excerpt_chars,
        )
        calls = 0
        draft = DossierDraft()
        malformed: tuple[DroppedClaim, ...] = ()
        if excerpts:
            draft, malformed = self._ask(scenario, excerpts)
            calls = 1
        dossier, dropped = verify(
            draft,
            scenario=scenario,
            excerpts=excerpts,
            min_support_ratio=config.min_support_ratio,
            max_claims_per_section=config.max_claims_per_section,
        )
        return WriteOutcome(
            scenario_id=scenario.scenario_id,
            dossier=dossier,
            dossier_sha256=dossier_hash(dossier),
            dropped=malformed + dropped,
            n_excerpts=len(excerpts),
            llm_calls=calls,
        )

    def _ask(
        self, scenario: Scenario, excerpts: Sequence[Excerpt]
    ) -> tuple[DossierDraft, tuple[DroppedClaim, ...]]:
        request = LLMRequest(
            model=self.settings.models.compiler,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt(scenario, excerpts)}],
            tools=[DOSSIER_TOOL],
            tool_choice={"type": "tool", "name": DOSSIER_TOOL_NAME},
            temperature=self.settings.models.compiler_temperature,
            max_tokens=self.settings.dossier.max_tokens,
            prompt_rev=self.settings.llm.prompt_rev,
        )
        result = self.client.complete(request, trace_name="dossier.write")
        for block in result.tool_calls:
            if block.get("name") == DOSSIER_TOOL_NAME and isinstance(block.get("input"), dict):
                return _parse(block["input"])
        raise RuntimeError(
            f"dossier.write: model returned no {DOSSIER_TOOL_NAME!r} tool call "
            f"(stop_reason={result.stop_reason!r}, text={result.text[:160]!r})"
        )


def _parse(payload: Mapping[str, Any]) -> tuple[DossierDraft, tuple[DroppedClaim, ...]]:
    """Parse the tool input claim by claim, so one malformed claim costs that
    claim and not the scenario. A claim that fails the schema (too long, no
    citation, a malformed date) is returned as dropped rather than skipped:
    the drop count is a reported figure, and a silent skip would understate it."""
    sections: dict[str, list[Claim]] = {}
    malformed: list[DroppedClaim] = []
    for name in SECTIONS:
        raw = payload.get(name, [])
        parsed: list[Claim] = []
        for item in raw if isinstance(raw, list) else []:
            try:
                parsed.append(Claim.model_validate(item))
            except ValidationError as exc:
                text = str(item.get("text", item)) if isinstance(item, dict) else str(item)
                first = exc.errors()[0]
                where = ".".join(str(part) for part in first.get("loc", ()))
                malformed.append(
                    DroppedClaim(section=name, text=text[:400], reason=f"malformed:{where}")
                )
        sections[name] = parsed
    return DossierDraft(**sections), tuple(malformed)
