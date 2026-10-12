"""The Action Group schema is generated, and it still cannot name a cutoff (ADR-0051).

Two claims are held here, and they are different in kind.

The first is a *drift* claim: the OpenAPI document Bedrock would hold is
generated from the same Pydantic models ``ToolSession.execute`` validates
against, so a change to ``LookupEvidenceArgs`` is a change to the schema and
there is no second copy to forget. The tests for it widen a model on purpose
and assert the generator either carries the change or refuses it by name --
never that it silently emits a document whose meaning is a guess.

The second is the *time lock* claim ADR-0049 made in-process, asked again at a
boundary where an AWS service sits in the middle. ``extra="forbid"`` has
exactly one expression in OpenAPI -- ``additionalProperties: false`` on an
object schema -- and Bedrock documents its parser only as "a subset of the
OpenAPI 3.0 specification". So the generated document is asserted to carry that
member, and the *end-to-end* test drives a real ``ToolBelt`` with an ``as_of``
that Bedrock has passed through anyway, asserting it comes back a refusal and
not a dropped key. That distinction is the whole of ADR-0049's argument and it
is the one this file exists to check survives the translation.

The shapes are checked against the installed botocore service models for
``bedrock-agent`` and ``bedrock-agent-runtime``, not against memory: there is
no AWS account here, so every member name a request would carry is verified
against the service's own model or it is not claimed at all.

Everything runs offline against a stub retrieval port.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict, Field

from cascade.canonical import canonical_json
from cascade.llm.bedrock_agent import (
    ACTION_GROUP,
    ACTION_GROUP_EXECUTOR,
    API_PATHS,
    HTTP_METHOD,
    MEDIA_TYPE,
    OPENAPI_VERSION,
    MalformedInvocation,
    TimeParameterRefused,
    UnknownToolPath,
    UnsupportedByBedrock,
    action_group,
    action_group_payload,
    action_group_schema,
    api_result,
    assert_no_time_parameter,
    invocation_arguments,
    session_state,
    tool_for_path,
)
from cascade.llm.bedrock_agent import _openapi_schema as openapi_schema
from cascade.retrieval.schema import RetrievedChunk
from cascade.sim.tools import (
    LOOKUP_EVIDENCE,
    QUERY_MAX_CHARS,
    RECALL,
    TOOL_NAMES,
    LookupEvidenceArgs,
    ToolBelt,
    ToolResult,
)

CUTOFF = datetime(2024, 3, 1, tzinfo=UTC)
LOOKUP_PATH = "/lookup-evidence"


def stub_search(query: str, *, as_of: datetime, k: int) -> tuple[RetrievedChunk, ...]:
    """One admissible chunk, whatever is asked. The port is injected, so this is all it takes."""
    return (
        RetrievedChunk(
            chunk_id="c1",
            document_id="d1",
            ordinal=0,
            body=f"an answer to {query}",
            published_at=datetime(2024, 2, 1, tzinfo=UTC),
            source="ccnews",
            url="https://example.invalid/1",
            title="a title",
            distance=0.1,
        ),
    )


def belt() -> ToolBelt:
    """A bound surface with the cutoff held privately, exactly as a run builds one."""
    return ToolBelt(
        scenario_id="s1",
        actor_id="a1",
        as_of=CUTOFF,
        search=stub_search,
        k=6,
        excerpt_chars=200,
    )


def body_schema(document: dict[str, Any], path: str) -> dict[str, Any]:
    """The media-type schema of one operation's request body."""
    operation = document["paths"][path][HTTP_METHOD]
    schema: dict[str, Any] = operation["requestBody"]["content"][MEDIA_TYPE]["schema"]
    return schema


def api_input(**overrides: Any) -> dict[str, Any]:
    """One ``apiInvocationInput`` in the shape the return-control event carries."""
    payload: dict[str, Any] = {
        "actionGroup": ACTION_GROUP,
        "apiPath": LOOKUP_PATH,
        "httpMethod": HTTP_METHOD,
        "requestBody": {
            "content": {
                MEDIA_TYPE: {"properties": [{"name": "query", "type": "string", "value": "why"}]}
            }
        },
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# The schema is generated, not written beside the model
# ---------------------------------------------------------------------------


def test_the_body_schema_carries_the_model_s_own_bounds() -> None:
    """The bounds come from `LookupEvidenceArgs`, so editing it edits the schema."""
    schema = body_schema(action_group_schema(), LOOKUP_PATH)
    assert schema["properties"] == {
        "query": {"maxLength": QUERY_MAX_CHARS, "minLength": 1, "type": "string"}
    }
    assert schema["required"] == ["query"]
    # And they really are the model's, not a copy that happens to agree today.
    emitted = LookupEvidenceArgs.model_json_schema()["properties"]["query"]
    assert schema["properties"]["query"]["maxLength"] == emitted["maxLength"]


def test_extra_forbid_survives_as_additional_properties_false() -> None:
    """`extra="forbid"` has one expression in OpenAPI and the document carries it."""
    document = action_group_schema()
    for _tool, path in API_PATHS:
        assert body_schema(document, path)["additionalProperties"] is False


def test_a_model_without_extra_forbid_is_refused() -> None:
    """An open model would make a supplied cutoff a dropped key rather than a refusal."""

    class Open(BaseModel):
        query: str

    with pytest.raises(UnsupportedByBedrock, match="additionalProperties"):
        openapi_schema(Open)


def test_no_argument_goes_in_a_parameter_list() -> None:
    """A parameter list has no object schema, so it has nowhere to carry the closure."""
    document = action_group_schema()
    for _tool, path in API_PATHS:
        assert "parameters" not in document["paths"][path][HTTP_METHOD]


# ---------------------------------------------------------------------------
# What the translation refuses rather than guesses at
# ---------------------------------------------------------------------------


def test_an_enum_is_refused_by_name() -> None:
    """Bedrock documents `enum` as unsupported; emitting one would be a silent narrowing."""

    class Choice(BaseModel):
        model_config = ConfigDict(extra="forbid")
        mode: str = Field(json_schema_extra={"enum": ["a", "b"]})

    with pytest.raises(UnsupportedByBedrock, match="enum"):
        openapi_schema(Choice)


def test_a_nested_model_is_refused_rather_than_emitted_with_defs() -> None:
    """2020-12 puts nested models in `$defs`; OpenAPI 3.0 has no such keyword."""

    class Inner(BaseModel):
        model_config = ConfigDict(extra="forbid")
        value: str

    class Outer(BaseModel):
        model_config = ConfigDict(extra="forbid")
        inner: Inner

    with pytest.raises(UnsupportedByBedrock, match=r"\$defs"):
        openapi_schema(Outer)


def test_an_optional_field_is_refused_rather_than_emitted_as_anyof() -> None:
    """Pydantic renders `str | None` as `anyOf` with a null arm; OpenAPI 3.0 uses `nullable`."""

    class Maybe(BaseModel):
        model_config = ConfigDict(extra="forbid")
        query: str | None = None

    with pytest.raises(UnsupportedByBedrock, match="anyOf"):
        openapi_schema(Maybe)


# ---------------------------------------------------------------------------
# The time lock, at the boundary
# ---------------------------------------------------------------------------


def test_a_temporal_field_name_is_refused() -> None:
    """A cutoff the model can name is the failure this whole surface is shaped around."""

    class Widened(BaseModel):
        model_config = ConfigDict(extra="forbid")
        query: str
        as_of: str

    document = {
        "paths": {
            "/x": {
                "post": {
                    "requestBody": {"content": {MEDIA_TYPE: {"schema": openapi_schema(Widened)}}}
                }
            }
        }
    }
    with pytest.raises(TimeParameterRefused, match="asof"):
        assert_no_time_parameter(document)


def test_a_temporal_format_is_refused_even_under_an_innocent_name() -> None:
    """The format check is exact where the name check is a heuristic, and catches this."""
    document = {
        "paths": {
            "/x": {
                "post": {
                    "requestBody": {
                        "content": {
                            MEDIA_TYPE: {
                                "schema": {
                                    "type": "object",
                                    "additionalProperties": False,
                                    "properties": {
                                        "horizonMarker": {"type": "string", "format": "date-time"}
                                    },
                                    "required": [],
                                }
                            }
                        }
                    }
                }
            }
        }
    }
    with pytest.raises(TimeParameterRefused, match="date-time"):
        assert_no_time_parameter(document)


def test_a_document_missing_the_closure_is_refused() -> None:
    """Without `additionalProperties: false` a supplied cutoff is dropped, not refused."""
    document = {
        "paths": {
            "/x": {
                "post": {
                    "requestBody": {
                        "content": {
                            MEDIA_TYPE: {"schema": {"type": "object", "properties": {"query": {}}}}
                        }
                    }
                }
            }
        }
    }
    with pytest.raises(TimeParameterRefused, match="additionalProperties"):
        assert_no_time_parameter(document)


def test_a_cutoff_bedrock_passed_through_is_refused_and_not_dropped() -> None:
    """The crux, end to end: an unexpected `as_of` reaches the closed model and is refused.

    This is the case the schema alone cannot settle. Bedrock's parser is
    documented as a subset and there is no account here to ask it what it does
    with `additionalProperties: false`, so the surface is driven as though the
    field had come through: the invocation is read, handed to the one
    validator, and the answer has to be a refusal that spends the turn -- not
    an answer to the query with the date quietly discarded.
    """
    invocation = api_input(
        requestBody={
            "content": {
                MEDIA_TYPE: {
                    "properties": [
                        {"name": "query", "type": "string", "value": "when does this resolve"},
                        {"name": "as_of", "type": "string", "value": "2030-01-01T00:00:00Z"},
                    ]
                }
            }
        }
    )
    tool, payload = invocation_arguments(invocation)
    assert payload == {"query": "when does this resolve", "as_of": "2030-01-01T00:00:00Z"}

    session = belt().session(memory="")
    result = session.execute(tool, payload)

    assert result.ok is False
    assert result.error is not None and result.error.startswith("bad_arguments:")
    # The refusal does not hand back the name it refused, or the cutoff.
    assert "as_of" not in result.content
    assert "2024" not in result.content
    # And the result Bedrock would be sent is a 200 carrying that refusal, so
    # the agent spends one turn rather than the run.
    answer = api_result(invocation, result)
    assert answer["httpStatusCode"] == 200
    assert "responseState" not in answer


def test_a_well_formed_invocation_is_answered_at_the_bound_cutoff() -> None:
    """The positive control: without it, a surface that refused everything would pass above."""
    invocation = api_input()
    tool, payload = invocation_arguments(invocation)
    assert tool == LOOKUP_EVIDENCE

    result = belt().session(memory="").execute(tool, payload)
    assert result.ok is True
    assert result.n_results == 1
    assert result.dropped == 0
    assert "an answer to why" in result.content


# ---------------------------------------------------------------------------
# Reading the invocation
# ---------------------------------------------------------------------------


def test_a_loose_parameter_is_collected_rather_than_discarded() -> None:
    """Discarding it here would be this module performing the drop it refuses the schema for."""
    invocation = api_input(
        parameters=[{"name": "as_of", "type": "string", "value": "2030-01-01"}],
    )
    _tool, payload = invocation_arguments(invocation)
    assert payload == {"as_of": "2030-01-01", "query": "why"}


def test_a_repeated_argument_is_refused() -> None:
    """Letting the last one win would make which value was used a fact about iteration order."""
    invocation = api_input(parameters=[{"name": "query", "type": "string", "value": "other"}])
    with pytest.raises(MalformedInvocation, match="twice"):
        invocation_arguments(invocation)


def test_a_body_under_another_media_type_is_not_read() -> None:
    """A body the schema never declared is not this operation's body."""
    invocation = api_input(
        requestBody={"content": {"text/plain": {"properties": [{"name": "query", "value": "x"}]}}}
    )
    _tool, payload = invocation_arguments(invocation)
    assert payload == {}


def test_an_unknown_path_names_the_surface() -> None:
    """A path that does not resolve means the deployment and this process disagree."""
    with pytest.raises(MalformedInvocation, match="no operation at"):
        tool_for_path("/get-weather")
    with pytest.raises(MalformedInvocation, match="no apiPath"):
        invocation_arguments({"actionGroup": ACTION_GROUP})


def test_every_tool_on_the_surface_has_a_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """A tool with no path would ship an action group smaller than the cell claims.

    The guard is checked by removing a path, which is the drift it exists for:
    a tool added to ``TOOL_NAMES`` and not to ``API_PATHS`` would otherwise
    produce a document describing one operation while the arm was configured
    for two -- ADR-0025's failure mode, where nothing downstream can tell.
    """
    assert tuple(name for name, _path in API_PATHS) == TOOL_NAMES

    monkeypatch.setattr("cascade.llm.bedrock_agent.API_PATHS", ((RECALL, "/recall"),), raising=True)
    with pytest.raises(UnknownToolPath, match=LOOKUP_EVIDENCE):
        action_group_schema()


# ---------------------------------------------------------------------------
# The channel a cutoff would have to travel through, and is not given
# ---------------------------------------------------------------------------


def test_session_state_has_no_parameter_for_a_session_attribute() -> None:
    """`promptSessionAttributes` is rendered into the prompt, so a cutoff there is one the model reads."""
    parameters = set(inspect.signature(session_state).parameters)
    assert parameters == {"invocation_id", "results"}
    assert not parameters & {"session_attributes", "prompt_session_attributes", "attributes"}

    state = session_state(invocation_id="inv-1", results=[api_result(api_input(), _ok())])
    assert set(state) == {"invocationId", "returnControlInvocationResults"}


def test_session_state_refuses_a_turn_that_says_nothing() -> None:
    """`inputText` is ignored when this field is present, so an empty list buys a billed turn."""
    with pytest.raises(MalformedInvocation, match="empty list"):
        session_state(invocation_id="inv-1", results=[])
    with pytest.raises(MalformedInvocation, match="invocationId"):
        session_state(invocation_id="", results=[api_result(api_input(), _ok())])


def _ok() -> ToolResult:
    return ToolResult(tool=LOOKUP_EVIDENCE, ok=True, content="something", n_results=1)


# ---------------------------------------------------------------------------
# Determinism of the document itself
# ---------------------------------------------------------------------------


def test_the_document_is_emitted_in_the_surface_s_own_order() -> None:
    """Emission order is a function of the set, not of how the operator typed it.

    Asserted on the returned mapping rather than on the payload, because
    ``canonical_json`` sorts keys and would hide the difference -- the first
    version of this test compared two payloads and passed against a generator
    that emitted ``allow`` order verbatim, which is the vacuous-assertion shape
    M12 found in the derived-budget test.
    """
    typed_backwards = list(action_group_schema([RECALL, LOOKUP_EVIDENCE])["paths"])
    typed_forwards = list(action_group_schema([LOOKUP_EVIDENCE, RECALL])["paths"])
    assert typed_backwards == typed_forwards == [path for _tool, path in API_PATHS]


def test_the_payload_is_the_canonical_serialisation() -> None:
    """One definition of these bytes, the same one the cache key and the manifest use."""
    assert action_group_payload() == canonical_json(action_group_schema())


def test_a_narrowed_allow_describes_only_what_it_enables() -> None:
    """An agent told about a tool the executor refuses generates refusals the arm is charged for."""
    document = action_group_schema([RECALL])
    assert sorted(document["paths"]) == ["/recall"]


# ---------------------------------------------------------------------------
# The prose the model is shown
# ---------------------------------------------------------------------------


def test_the_maintainer_docstring_does_not_reach_the_document() -> None:
    """`LookupEvidenceArgs`'s docstring names `as_of` and `ToolBelt`; the model is not shown it."""
    payload = action_group_payload()
    for leaked in ("ToolBelt", "mypy", ":class:", "args.as_of"):
        assert leaked not in payload
    schema = body_schema(action_group_schema(), LOOKUP_PATH)
    assert "description" not in schema
    assert "title" not in schema
    assert "title" not in schema["properties"]["query"]


def test_every_operation_carries_what_bedrock_requires() -> None:
    """`description`, `operationId` and `responses` are how the agent decides what to call."""
    document = action_group_schema()
    assert document["openapi"] == OPENAPI_VERSION
    for path, operations in sorted(document["paths"].items()):
        assert path.startswith("/")
        for _method, operation in sorted(operations.items()):
            assert operation["operationId"] in TOOL_NAMES
            assert operation["description"].strip()
            assert "200" in operation["responses"]


# ---------------------------------------------------------------------------
# Verified against the installed service models, not against memory
# ---------------------------------------------------------------------------


def service_model(name: str) -> Any:
    botocore_session = pytest.importorskip("botocore.session")
    return botocore_session.get_session().get_service_model(name)


def test_the_action_group_members_are_the_service_model_s_own() -> None:
    """There is no account here, so a member name is verified against botocore or not claimed."""
    shape = service_model("bedrock-agent").shape_for("CreateAgentActionGroupRequest")
    assert set(action_group()) <= set(shape.members)
    executor = service_model("bedrock-agent").shape_for("ActionGroupExecutor")
    assert set(ACTION_GROUP_EXECUTOR) <= set(executor.members)
    assert ACTION_GROUP_EXECUTOR["customControl"] in executor.members["customControl"].enum
    # The schema travels inline, in the member that takes a string.
    assert "payload" in service_model("bedrock-agent").shape_for("APISchema").members


def test_the_session_state_and_result_members_are_the_service_model_s_own() -> None:
    """Including the two attribute maps this module deliberately never writes into."""
    runtime = service_model("bedrock-agent-runtime")
    state = runtime.shape_for("SessionState")
    built = session_state(invocation_id="inv-1", results=[api_result(api_input(), _ok())])
    assert set(built) <= set(state.members)
    # The channels exist in the service; they are simply not reachable from here.
    assert {"sessionAttributes", "promptSessionAttributes"} <= set(state.members)

    result = runtime.shape_for("ApiResult")
    assert set(api_result(api_input(), _ok())) <= set(result.members)
    # `responseState` is a member, and is the one this module declines to use.
    assert "responseState" in result.members


def test_neither_agent_service_offers_a_batch_operation() -> None:
    """ADR-0020 makes the 50% batch rate functional; an agent loop cannot reach it."""
    for name in ("bedrock-agent", "bedrock-agent-runtime"):
        operations = sorted(service_model(name).operation_names)
        assert not [op for op in operations if "Batch" in op]
