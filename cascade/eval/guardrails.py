"""What a Bedrock Guardrail *would* have done to the compiled graphs (ADR-0050).

ADR-0030 deferred guardrails on two grounds. The first was that applying one
would need a second model-adjacent call path outside ``LLMClient``, which
invariant 5 exists to prevent. The second was the one that mattered more: a
guardrail that alters a single decomposition changes the experiment, and
nobody had measured whether it would. ADR-0030 named the remedy itself --
"a measurement shows it alters no compiled graph on the 180 scenarios ... The
natural shape is a post-hoc audit over stored graphs rather than a filter in
the call path." This module is that audit.

**It is not a filter, and it is built so it cannot become one.** Nothing here
runs during ``cascade compile build``; nothing here returns a graph; nothing
here can write. It reads graphs that already exist, screens their text, and
reports. A filter in the compile path would be unmeasurable by construction:
the graph it changed would be the only graph that ever existed, so there would
be nothing to compare it against and no way to tell a safety control from a
silent edit to the study's inputs.

**A flagged graph is a confound, not a safety win.** The framing runs through
every name in this file. If the guardrail intervenes on even one of the 180
decompositions, then the arm that ran with it and the arm that ran without it
are not the same experiment, and the comparison the study publishes is between
two things that differ in an unrecorded way. If it intervenes on none, it is a
control with no effect on the measured result -- which is the only condition
under which ADR-0050 admits it into the call path at all.

**The three verdicts are kept apart on purpose.** M8 shipped a cost ledger
that reported "0.0000% -- reconciles within tolerance" over two zeros, and
``LedgerReconciliation.vacuous`` exists because of it. The same failure is
available here and is worse, because it would read as a safety assurance: an
audit that reached no guardrail flags nothing, and "nothing flagged" would
render identically to "we checked all 180 and none was touched". So
:meth:`GuardrailAudit.verdict` distinguishes *not configured / nothing
assessed*, *partly assessed*, *assessed and clear*, and *assessed and
confounded*, and :attr:`GuardrailAudit.headline` always prints the denominator
beside the count.

What the audit can and cannot see. A guardrail in the compile path would have
screened the prompt going in and the tool-use JSON coming back. Only the
graph survives, so this screens the graph's free text -- every actor name,
objective and constraint, and every factor name -- rendered as one payload per
scenario. That is a superset of the prose the model actually emitted inside
the tool call, so a guardrail that flags nothing here would have flagged
nothing there; the approximation errs toward finding a confound, which is the
conservative direction for this particular question. It cannot re-screen the
retrieved evidence, which is not stored with the graph: that gap is stated
rather than closed.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

from cascade.config import Settings
from cascade.decompose.schema import CausalGraph, graph_hash
from cascade.llm.types import SOURCE, GuardrailScreen, ScreenResult

__all__ = [
    "SOURCE",
    "GraphAssessment",
    "GuardrailAudit",
    "GuardrailScreen",
    "ScreenResult",
    "TextSegment",
    "Verdict",
    "audit_graphs",
    "graph_text",
    "run_audit",
    "screen_payload",
    "summarise",
]

# `ApplyGuardrail` takes INPUT or OUTPUT (verified against the installed
# botocore service model for `bedrock-runtime`). A compiled graph is what the
# compiler emitted, so it is screened as OUTPUT; screening it as INPUT would
# apply the prompt-attack filters to text no user ever sent.

Verdict = Literal["not_assessed", "incomplete", "clear", "confound"]


@dataclass(frozen=True, slots=True)
class TextSegment:
    """One free-text field of a compiled graph, carrying where it came from.

    Preserves the property that a flagged graph is diagnosable. The guardrail
    answers about the payload as a whole, so without the segment list a
    "flagged" verdict would name a scenario and nothing else, and the reviewer
    deciding whether the intervention is real would have to re-derive the text
    by hand from the stored JSON.
    """

    path: str
    text: str


@dataclass(frozen=True, slots=True)
class GraphAssessment:
    """One stored graph, and what the guardrail did or did not say about it."""

    scenario_id: str
    graph_sha256: str
    segments: int
    characters: int
    result: ScreenResult | None = None
    error: str = ""

    @property
    def assessed(self) -> bool:
        """True only when a guardrail actually answered about this graph.

        A refusal, a throttle and an absent configuration all leave this
        False. They are not the same fact as an answer of NONE, and the
        difference is the whole point of the audit.
        """
        return self.result is not None

    @property
    def flagged(self) -> bool:
        """True when applying this guardrail would have altered this graph."""
        return self.result is not None and self.result.action == "GUARDRAIL_INTERVENED"


@dataclass(frozen=True, slots=True)
class GuardrailAudit:
    """The measured answer to "would this guardrail change the study?".

    ``graphs`` is the number read from the store and is carried separately
    from the assessments, so a pass that read 180 graphs and assessed none
    cannot report the same way as one that assessed 180 and found nothing.
    """

    guardrail_id: str | None
    guardrail_version: str | None
    region: str | None
    graphs: int
    assessments: tuple[GraphAssessment, ...] = field(default_factory=tuple)

    @property
    def configured(self) -> bool:
        """Whether a guardrail was named at all (``providers.bedrock.guardrail_id``)."""
        return bool(self.guardrail_id)

    @property
    def assessed(self) -> int:
        return sum(1 for item in self.assessments if item.assessed)

    @property
    def errors(self) -> tuple[GraphAssessment, ...]:
        """Graphs the guardrail was asked about and did not answer for."""
        return tuple(item for item in self.assessments if not item.assessed and item.error)

    @property
    def flagged(self) -> tuple[GraphAssessment, ...]:
        """Every graph the guardrail would have intervened on, ordered by scenario."""
        return tuple(
            sorted(
                (item for item in self.assessments if item.flagged),
                key=lambda item: item.scenario_id,
            )
        )

    @property
    def verdict(self) -> Verdict:
        """The four states, kept apart because three of them are a zero.

        ``not_assessed`` and ``clear`` both report zero flagged graphs and
        mean opposite things: the first is "we did not check", the second is
        "we checked every one". ``incomplete`` is the third -- a pass that
        reached the service for some graphs and not others is not a clean
        result for the ones it missed, and rounding it to ``clear`` is how an
        outage becomes an assurance (the M8 ledger's lesson, `trace/ledger.py`).
        """
        if not self.configured or self.assessed == 0:
            return "not_assessed"
        if self.flagged:
            return "confound"
        if self.assessed < self.graphs:
            return "incomplete"
        return "clear"

    @property
    def confound(self) -> bool:
        """True when the guardrail would alter at least one decomposition.

        Named for what it does to the study rather than for what it catches.
        An intervention here is not a threat blocked; it is an arm of the
        experiment that no longer matches the arm it is compared against.
        """
        return bool(self.flagged)

    @property
    def clear(self) -> bool:
        """True only when every graph read was assessed and none was altered.

        This is ADR-0050's admission condition, and it is deliberately harder
        to satisfy than ``not self.confound``: an audit that assessed nothing
        also flagged nothing.
        """
        return self.verdict == "clear"

    @property
    def headline(self) -> str:
        """One sentence that cannot be mistaken for the wrong kind of zero.

        Prints the denominator beside every count, so a reader who sees "0
        flagged" also sees how many graphs that zero was measured over.
        """
        where = self.guardrail_id or "none configured"
        version = self.guardrail_version or "-"
        scope = f"guardrail {where} (version {version}), {self.graphs} graph(s) read"
        if self.verdict == "not_assessed":
            reason = (
                "no guardrail is configured"
                if not self.configured
                else f"the guardrail answered for none of them ({len(self.errors)} error(s))"
            )
            return f"NOT ASSESSED: {scope}; {reason}. Nothing was checked."
        if self.verdict == "confound":
            return (
                f"CONFOUND: {scope}; {self.assessed} assessed, {len(self.flagged)} would be "
                "altered by this guardrail. Applying it changes the experiment, so it is not "
                "admissible to the compile path (ADR-0050)."
            )
        if self.verdict == "incomplete":
            return (
                f"INCOMPLETE: {scope}; {self.assessed} assessed, {len(self.errors)} not "
                "answered for, 0 flagged. The unanswered graphs are unchecked, not clear."
            )
        return (
            f"CLEAR: {scope}; {self.assessed} of {self.graphs} assessed, 0 altered. "
            "This guardrail is a control with no measured effect on the study."
        )


def graph_text(graph: CausalGraph) -> tuple[TextSegment, ...]:
    """Every free-text field of a compiled graph, in a fixed order. Pure.

    Preserves the property that two audits of one graph send byte-identical
    text. Segments are sorted by path rather than emitted in the model's own
    order, for the reason :meth:`CausalGraph.canonical` gives: emission order
    is not part of a graph's identity, and a payload that depended on it would
    make a re-audit of an unchanged graph a different measurement.

    Numeric fields are excluded. A weight, a volatility or a lag carries no
    text for a guardrail to have an opinion about, and padding the payload
    with them would only dilute the text that does.
    """
    segments: list[TextSegment] = []
    for actor in sorted(graph.actors, key=lambda item: item.id):
        segments.append(TextSegment(path=f"actors.{actor.id}.name", text=actor.name))
        segments.append(TextSegment(path=f"actors.{actor.id}.objective", text=actor.objective))
        for index, constraint in enumerate(sorted(actor.constraints)):
            segments.append(
                TextSegment(path=f"actors.{actor.id}.constraints[{index}]", text=constraint)
            )
    for factor in sorted(graph.factors, key=lambda item: item.id):
        segments.append(TextSegment(path=f"factors.{factor.id}.name", text=factor.name))
    return tuple(sorted(segments, key=lambda item: item.path))


def screen_payload(segments: Sequence[TextSegment]) -> str:
    """The one text block sent for a graph. Pure.

    Each segment is labelled with its path, because a guardrail's
    ``actionReason`` quotes the text it objected to and an unlabelled
    concatenation would leave a reviewer grepping 180 graphs for a phrase.
    """
    return "\n".join(f"{segment.path}: {segment.text}" for segment in segments)


def summarise(
    assessments: Sequence[GraphAssessment],
    *,
    graphs: int,
    guardrail_id: str | None,
    guardrail_version: str | None,
    region: str | None,
) -> GuardrailAudit:
    """Assemble the audit from per-graph assessments. Pure.

    ``graphs`` is passed rather than derived from ``len(assessments)``: the
    count of graphs *read* and the count of graphs *reached* must be able to
    disagree, since their disagreement is exactly what separates a clear
    result from an outage.
    """
    if graphs < len(assessments):
        raise ValueError(
            f"assessed {len(assessments)} graph(s) but only {graphs} were read; "
            "the denominator cannot be smaller than the numerator"
        )
    return GuardrailAudit(
        guardrail_id=guardrail_id,
        guardrail_version=guardrail_version,
        region=region,
        graphs=graphs,
        assessments=tuple(sorted(assessments, key=lambda item: item.scenario_id)),
    )


def audit_graphs(
    graphs: Sequence[CausalGraph],
    *,
    screen: GuardrailScreen | None,
    guardrail_id: str | None,
    guardrail_version: str | None,
    region: str | None = None,
) -> GuardrailAudit:
    """Screen each graph and report. Reads nothing, writes nothing.

    Preserves the property that a graph is never modified by the thing
    measuring it: the screen's answer is recorded beside the graph and the
    graph is passed through untouched -- there is no return path for an
    edited decomposition, because a post-hoc audit that could rewrite its
    subject would be the filter ADR-0050 refuses.

    ``screen=None`` records every graph as unassessed rather than as clean.
    """
    assessments: list[GraphAssessment] = []
    for graph in sorted(graphs, key=lambda item: item.scenario_id):
        segments = graph_text(graph)
        base = GraphAssessment(
            scenario_id=graph.scenario_id,
            graph_sha256=graph_hash(graph),
            segments=len(segments),
            characters=sum(len(segment.text) for segment in segments),
        )
        if screen is None:
            assessments.append(base)
            continue
        try:
            result = screen.screen(text=screen_payload(segments))
        except Exception as error:  # noqa: BLE001 -- see below
            # A throttle, a permission error or an unreachable endpoint must
            # arrive as *not assessed*, with the message kept. Re-raising
            # would abandon 179 measurable graphs over one refusal; returning
            # a NONE would report an outage as a safety assurance, which is
            # the M8 ledger's failure with the sign flipped.
            assessments.append(
                GraphAssessment(
                    scenario_id=base.scenario_id,
                    graph_sha256=base.graph_sha256,
                    segments=base.segments,
                    characters=base.characters,
                    error=f"{type(error).__name__}: {error}",
                )
            )
            continue
        assessments.append(
            GraphAssessment(
                scenario_id=base.scenario_id,
                graph_sha256=base.graph_sha256,
                segments=base.segments,
                characters=base.characters,
                result=result,
            )
        )
    return summarise(
        assessments,
        graphs=len(graphs),
        guardrail_id=guardrail_id,
        guardrail_version=guardrail_version,
        region=region,
    )


def run_audit(
    settings: Settings,
    *,
    screen: GuardrailScreen | None = None,
    graphs: Sequence[CausalGraph] | None = None,
) -> GuardrailAudit:
    """Read the stored graphs and audit them. The shell.

    Preserves invariant 2's sibling property for this table: graphs are loaded
    as ``cascade_eval``, which migration 008 grants ``SELECT`` on
    ``causal_graphs`` and nothing else. An attempt to write one back is a
    Postgres permission error rather than a promise in a docstring -- the same
    mechanism ADR-0005 uses for the labels, and the reason this module can
    claim it never alters a decomposition.

    ``screen`` defaults to a Bedrock guardrail built from
    ``providers.bedrock``, and to ``None`` when none is configured -- which
    produces an audit whose verdict is ``not_configured``, not ``clear``.
    """
    from cascade.decompose.store import load_graphs

    loaded = tuple(graphs) if graphs is not None else load_graphs(settings, role="eval")
    bedrock = settings.providers.bedrock
    if screen is None and bedrock.guardrail_id:
        from cascade.llm.client import BedrockGuardrail

        screen = BedrockGuardrail.from_settings(settings)
    return audit_graphs(
        loaded,
        screen=screen,
        guardrail_id=bedrock.guardrail_id,
        guardrail_version=bedrock.guardrail_version,
        region=bedrock.region,
    )
