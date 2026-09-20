"""The guardrail audit (ADR-0050): a measurement, never a filter.

Three claims are held here, and each is tested against the mistake it
prevents rather than only against the happy path.

1. **The three zeros stay apart.** "No guardrail configured", "the service
   refused" and "every graph assessed, none altered" all report zero flagged
   graphs. M8 shipped a cost ledger that could not tell the first two from the
   third and reported `0.0000% -- reconciles within tolerance` over two zeros;
   the same bug here would read as a safety assurance. Every verdict is
   asserted from a state that produces it, and the headline is asserted to
   say which.

2. **A flagged graph is a confound.** The audit reports an intervention as a
   reason the experiment is no longer comparable, not as a threat caught.

3. **Nothing here can alter a graph.** The subject is passed through
   untouched, and the module carries no SQL that writes -- asserted
   statically, and the detector asserted against a synthetic violation,
   because a guard that cannot fail is not a guard.

The AWS call is exercised against a stub client shaped from the installed
botocore service model for ``bedrock-runtime``: no account, no credentials, no
network.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from cascade.config import Settings
from cascade.decompose.schema import CausalGraph, graph_hash
from cascade.eval.guardrails import (
    SOURCE,
    BedrockGuardrail,
    GraphAssessment,
    GuardrailAudit,
    ScreenResult,
    audit_graphs,
    graph_text,
    run_audit,
    screen_payload,
    summarise,
)
from tests.conftest import make_actor, make_graph

MODULE = Path(__file__).resolve().parents[2] / "cascade" / "eval" / "guardrails.py"


class Screen:
    """A guardrail that intervenes on the scenarios it was told to."""

    def __init__(self, *, intervene_on: tuple[str, ...] = (), raises: Exception | None = None):
        self.intervene_on = intervene_on
        self.raises = raises
        self.seen: list[str] = []

    def screen(self, *, text: str) -> ScreenResult:
        self.seen.append(text)
        if self.raises is not None:
            raise self.raises
        if any(needle in text for needle in self.intervene_on):
            return ScreenResult(
                action="GUARDRAIL_INTERVENED",
                reason="blocked by the topic policy",
                policies=("topicPolicy",),
            )
        return ScreenResult(action="NONE")


def graphs(n: int = 3) -> tuple[CausalGraph, ...]:
    return tuple(make_graph(scenario_id=f"s-{index}") for index in range(n))


# ---------------------------------------------------------------------------
# The three zeros
# ---------------------------------------------------------------------------


def test_no_guardrail_configured_reads_as_not_assessed_not_as_clear() -> None:
    audit = audit_graphs(graphs(3), screen=None, guardrail_id=None, guardrail_version=None)
    assert audit.verdict == "not_assessed"
    assert not audit.clear
    assert not audit.confound
    assert audit.assessed == 0
    assert audit.graphs == 3
    assert "NOT ASSESSED" in audit.headline
    assert "Nothing was checked" in audit.headline


def test_a_service_that_refuses_every_graph_reads_as_not_assessed() -> None:
    """The failure this prevents: an outage rendering as a safety assurance."""
    screen = Screen(raises=RuntimeError("ThrottlingException: slow down"))
    audit = audit_graphs(graphs(3), screen=screen, guardrail_id="gr-1", guardrail_version="1")
    assert audit.configured
    assert audit.verdict == "not_assessed"
    assert not audit.clear
    assert len(audit.errors) == 3
    assert "ThrottlingException" in audit.errors[0].error
    assert "3 error(s)" in audit.headline


def test_every_graph_assessed_and_none_altered_is_the_only_clear_result() -> None:
    audit = audit_graphs(graphs(3), screen=Screen(), guardrail_id="gr-1", guardrail_version="2")
    assert audit.verdict == "clear"
    assert audit.clear
    assert not audit.confound
    assert audit.assessed == 3
    assert "CLEAR" in audit.headline
    assert "3 of 3 assessed" in audit.headline


def test_the_three_zeros_do_not_render_alike() -> None:
    """All three report zero flagged graphs; none may read like another."""
    unconfigured = audit_graphs(graphs(2), screen=None, guardrail_id=None, guardrail_version=None)
    refused = audit_graphs(
        graphs(2),
        screen=Screen(raises=RuntimeError("AccessDeniedException")),
        guardrail_id="gr-1",
        guardrail_version="1",
    )
    checked = audit_graphs(graphs(2), screen=Screen(), guardrail_id="gr-1", guardrail_version="1")

    assert len({a.headline for a in (unconfigured, refused, checked)}) == 3
    assert all(len(a.flagged) == 0 for a in (unconfigured, refused, checked))
    assert [a.clear for a in (unconfigured, refused, checked)] == [False, False, True]


def test_a_partial_pass_is_incomplete_not_clear() -> None:
    """Half an answer is not a clean result for the half that went unanswered."""
    ok, bad = graphs(2)
    assessments = (
        GraphAssessment(
            scenario_id=ok.scenario_id,
            graph_sha256=graph_hash(ok),
            segments=1,
            characters=1,
            result=ScreenResult(action="NONE"),
        ),
        GraphAssessment(
            scenario_id=bad.scenario_id,
            graph_sha256=graph_hash(bad),
            segments=1,
            characters=1,
            error="ThrottlingException",
        ),
    )
    audit = summarise(
        assessments, graphs=2, guardrail_id="gr-1", guardrail_version="1", region="us-east-1"
    )
    assert audit.verdict == "incomplete"
    assert not audit.clear
    assert "INCOMPLETE" in audit.headline
    assert "unchecked, not clear" in audit.headline


def test_the_headline_always_carries_the_denominator() -> None:
    """A "0 flagged" that does not say how many graphs it read says nothing."""
    for audit in (
        audit_graphs(graphs(4), screen=None, guardrail_id=None, guardrail_version=None),
        audit_graphs(graphs(4), screen=Screen(), guardrail_id="gr-1", guardrail_version="1"),
        audit_graphs(
            graphs(4),
            screen=Screen(raises=RuntimeError("boom")),
            guardrail_id="gr-1",
            guardrail_version="1",
        ),
    ):
        assert "4 graph(s) read" in audit.headline


# ---------------------------------------------------------------------------
# A flagged graph is a confound
# ---------------------------------------------------------------------------


def test_an_intervention_is_reported_as_a_confound() -> None:
    subject = make_graph(
        scenario_id="s-flagged",
        actors=tuple(
            make_actor(
                index,
                factor_id=f"factor_{index % 4}",
                **({"name": "Tripwire"} if index == 0 else {}),
            )
            for index in range(8)
        ),
    )
    audit = audit_graphs(
        (subject, *graphs(2)),
        screen=Screen(intervene_on=("Tripwire",)),
        guardrail_id="gr-1",
        guardrail_version="1",
    )
    assert audit.confound
    assert audit.verdict == "confound"
    assert not audit.clear
    assert [item.scenario_id for item in audit.flagged] == ["s-flagged"]
    assert "CONFOUND" in audit.headline
    assert "changes the experiment" in audit.headline


def test_a_flagged_graph_keeps_the_reason_and_the_policies_that_fired() -> None:
    audit = audit_graphs(
        (make_graph(scenario_id="s-0"),),
        screen=Screen(intervene_on=("Party 0",)),
        guardrail_id="gr-1",
        guardrail_version="1",
    )
    flagged = audit.flagged[0]
    assert flagged.result is not None
    assert flagged.result.reason == "blocked by the topic policy"
    assert flagged.result.policies == ("topicPolicy",)


def test_an_unrecognised_action_is_not_folded_into_not_flagged() -> None:
    """A third action read as NONE would understate a confound."""
    guardrail = BedrockGuardrail(
        client=StubClient(action="REDACTED"), guardrail_id="gr-1", guardrail_version="1"
    )
    with pytest.raises(ValueError, match="does not recognise"):
        guardrail.screen(text="anything")


# ---------------------------------------------------------------------------
# It reads; it never writes
# ---------------------------------------------------------------------------


def test_the_audit_does_not_alter_the_graphs_it_measures() -> None:
    subjects = graphs(3)
    before = tuple(graph_hash(graph) for graph in subjects)
    audit_graphs(subjects, screen=Screen(), guardrail_id="gr-1", guardrail_version="1")
    assert tuple(graph_hash(graph) for graph in subjects) == before


def test_the_recorded_hash_is_the_graph_the_audit_actually_read() -> None:
    subjects = graphs(2)
    audit = audit_graphs(subjects, screen=Screen(), guardrail_id="gr-1", guardrail_version="1")
    expected = {graph.scenario_id: graph_hash(graph) for graph in subjects}
    assert {item.scenario_id: item.graph_sha256 for item in audit.assessments} == expected


def _write_verbs(source: str) -> list[str]:
    """SQL that would modify a stored graph, in whatever case it is written."""
    upper = source.upper()
    return sorted(
        verb for verb in ("INSERT INTO", "UPDATE ", "DELETE FROM", "TRUNCATE ") if verb in upper
    )


def test_the_module_contains_no_sql_that_writes() -> None:
    """Reads, never writes -- asserted over the source, not promised.

    The grant is the real mechanism (``cascade_eval`` holds SELECT on
    ``causal_graphs`` and nothing else, migration 008); this catches the
    statement being written at all, which is the step before someone runs it
    as ``admin``.
    """
    assert _write_verbs(MODULE.read_text(encoding="utf-8")) == []


def test_the_write_detector_actually_detects() -> None:
    """Guard the guard."""
    assert _write_verbs("cur.execute('update causal_graphs set graph = %s')") == ["UPDATE "]
    assert _write_verbs("DELETE FROM causal_graphs") == ["DELETE FROM"]


def test_stored_graphs_are_read_as_the_label_blind_reader_role() -> None:
    """``cascade_eval`` is granted SELECT on ``causal_graphs`` and nothing else."""
    source = MODULE.read_text(encoding="utf-8")
    assert 'load_graphs(settings, role="eval")' in source


# ---------------------------------------------------------------------------
# The payload
# ---------------------------------------------------------------------------


def test_the_payload_is_the_graphs_free_text_and_nothing_numeric() -> None:
    graph = make_graph(scenario_id="s-0")
    segments = graph_text(graph)
    paths = [segment.path for segment in segments]
    assert paths == sorted(paths), "segments must be emitted in a fixed order"
    assert any(path.endswith(".objective") for path in paths)
    assert any(path.startswith("factors.") for path in paths)
    text = screen_payload(segments)
    assert graph.actors[0].objective in text
    assert "volatility" not in text
    assert "steepness" not in text


def test_two_graphs_differing_only_in_emission_order_send_identical_text() -> None:
    """The same property ``CausalGraph.canonical`` preserves for the hash.

    A payload that depended on the model's emission order would make a
    re-audit of an unchanged graph a different measurement.
    """
    graph = make_graph(scenario_id="s-0")
    reordered = graph.model_copy(
        update={
            "actors": tuple(reversed(graph.actors)),
            "factors": tuple(reversed(graph.factors)),
        }
    )
    assert screen_payload(graph_text(graph)) == screen_payload(graph_text(reordered))


def test_every_segment_is_labelled_with_where_it_came_from() -> None:
    """An unlabelled concatenation leaves a reviewer grepping 180 graphs."""
    graph = make_graph(scenario_id="s-0")
    for line in screen_payload(graph_text(graph)).splitlines():
        assert line.startswith(("actors.", "factors."))


# ---------------------------------------------------------------------------
# The AWS seam, against a stub shaped from the installed service model
# ---------------------------------------------------------------------------


class StubClient:
    """``bedrock-runtime`` as far as ``ApplyGuardrail`` is concerned.

    Keyword names and the ``content`` shape are those of the installed
    botocore service model, so a call built against a different shape fails
    here rather than against an account nobody has.
    """

    def __init__(self, *, action: str = "NONE", assessments: list[Any] | None = None):
        self.action = action
        self.assessments = assessments or []
        self.calls: list[dict[str, Any]] = []

    def apply_guardrail(
        self,
        *,
        guardrailIdentifier: str,
        guardrailVersion: str,
        source: str,
        content: list[dict[str, Any]],
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "guardrailIdentifier": guardrailIdentifier,
                "guardrailVersion": guardrailVersion,
                "source": source,
                "content": content,
            }
        )
        return {
            "action": self.action,
            "actionReason": "policy" if self.action != "NONE" else "",
            "assessments": self.assessments,
            "outputs": [],
        }


def test_the_request_matches_the_installed_service_model() -> None:
    client = StubClient()
    guardrail = BedrockGuardrail(client=client, guardrail_id="gr-1", guardrail_version="3")
    assert guardrail.screen(text="hello").action == "NONE"
    call = client.calls[0]
    assert call["guardrailIdentifier"] == "gr-1"
    assert call["guardrailVersion"] == "3"
    assert call["source"] == SOURCE == "OUTPUT"
    assert call["content"] == [{"text": {"text": "hello"}}]


def test_a_compiled_graph_is_screened_as_output_not_as_input() -> None:
    """It is what the compiler emitted; INPUT would run the prompt-attack
    filters over text no user ever sent."""
    assert SOURCE == "OUTPUT"


def test_only_policy_members_of_an_assessment_are_reported_as_policies() -> None:
    """``invocationMetrics`` is bookkeeping and would read as a policy firing."""
    client = StubClient(
        action="GUARDRAIL_INTERVENED",
        assessments=[
            {
                "topicPolicy": {"topics": [{"name": "t"}]},
                "contentPolicy": {},
                "invocationMetrics": {"guardrailProcessingLatency": 12},
            }
        ],
    )
    result = BedrockGuardrail(client=client, guardrail_id="gr-1", guardrail_version="1").screen(
        text="x"
    )
    assert result.action == "GUARDRAIL_INTERVENED"
    assert result.policies == ("topicPolicy",)


def test_a_partial_configuration_is_refused_by_name_never_guessed(settings: Settings) -> None:
    """``ResourceNotFoundException`` over 180 graphs reads as an outage."""
    with pytest.raises(ValueError) as caught:
        BedrockGuardrail.from_settings(settings)
    message = str(caught.value)
    assert "providers.bedrock.guardrail_id" in message
    assert "providers.bedrock.region" in message


def test_the_messages_endpoint_is_not_handed_to_a_bedrock_runtime_client() -> None:
    """``providers.bedrock.base_url`` is the Mantle endpoint, not this service.

    It serves the Messages API and not ``ApplyGuardrail``. Routing a screening
    client at it would produce 180 failures that read as an outage rather than
    as a configuration mistake -- the one verdict hardest to diagnose. Asserted
    over the source because there is no account here to route anything at.
    """
    source = MODULE.read_text(encoding="utf-8")
    assert "endpoint_url=" not in source
    assert "endpoint_url =" not in source
    assert 'session.client("bedrock-runtime", region_name=bedrock.region)' in source


# ---------------------------------------------------------------------------
# The shell
# ---------------------------------------------------------------------------


def test_run_audit_with_no_guardrail_configured_touches_no_service(
    settings: Settings,
) -> None:
    """The credential-free path: an unconfigured audit is a real artifact."""
    audit = run_audit(settings, graphs=graphs(3))
    assert audit.verdict == "not_assessed"
    assert audit.guardrail_id is None
    assert audit.graphs == 3


def test_run_audit_passes_the_configured_identifier_into_the_report(
    settings: Settings,
) -> None:
    configured = settings.model_copy(
        update={
            "providers": settings.providers.model_copy(
                update={
                    "bedrock": settings.providers.bedrock.model_copy(
                        update={
                            "guardrail_id": "gr-abc",
                            "guardrail_version": "4",
                            "region": "us-east-1",
                        }
                    )
                }
            )
        }
    )
    audit = run_audit(configured, screen=Screen(), graphs=graphs(2))
    assert audit.guardrail_id == "gr-abc"
    assert audit.guardrail_version == "4"
    assert audit.region == "us-east-1"
    assert audit.verdict == "clear"


def test_a_denominator_smaller_than_the_numerator_is_refused() -> None:
    """Assessing more graphs than were read is a bookkeeping error, not a rate."""
    graph = make_graph(scenario_id="s-0")
    assessment = GraphAssessment(
        scenario_id="s-0",
        graph_sha256=graph_hash(graph),
        segments=1,
        characters=1,
        result=ScreenResult(action="NONE"),
    )
    with pytest.raises(ValueError, match="denominator"):
        summarise(
            (assessment, assessment),
            graphs=1,
            guardrail_id="gr-1",
            guardrail_version="1",
            region=None,
        )


def test_assessments_are_ordered_by_scenario(settings: Settings) -> None:
    """Invariant 7 at the boundary: a report's row order is not a hash order."""
    unsorted = (
        make_graph(scenario_id="s-9"),
        make_graph(scenario_id="s-1"),
        make_graph(scenario_id="s-5"),
    )
    audit = audit_graphs(unsorted, screen=Screen(), guardrail_id="gr-1", guardrail_version="1")
    assert [item.scenario_id for item in audit.assessments] == ["s-1", "s-5", "s-9"]


def test_an_audit_is_frozen() -> None:
    """Nothing downstream may edit a measurement after it was taken."""
    audit = GuardrailAudit(
        guardrail_id=None, guardrail_version=None, region=None, graphs=0, assessments=()
    )
    with pytest.raises((AttributeError, TypeError)):
        audit.graphs = 1  # type: ignore[misc]
