"""Bedrock's managed reranker: a permutation of the pool, and nothing wider (ADR-0047).

The claim this file holds is that a managed service can sit at the *ranking*
stage without weakening the time lock, and that the way it is reached does not
introduce a second way to be wrong:

* the seam is narrow -- a query and document bodies in, one score per body out.
  There is no parameter through which an ``as_of``, a ``chunk_id`` or a date
  could be passed, which is what makes a leak unrepresentable rather than
  merely forbidden;
* Bedrock answers ranked by relevance with an index into the request, so the
  response is mapped back to input order here. A missing or repeated index is
  an error -- a defaulted score sorts, and therefore silently reorders the
  evidence the prompt carries;
* routing is explicit. Every ambient variable that could redirect the call is
  set to a decoy and the request still reaches the configured region.

Nothing here touches AWS. The request-shaping tests run against a scripted
client; the routing tests let the real botocore build and sign the request and
replace only the wire, so a change in the service model is a failure here
rather than at the first paid call.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from cascade.config import Settings
from cascade.llm.client import RERANK_BILLING_KIND, BedrockReranker, RecordedReranker
from cascade.llm.meter import CostMeter
from cascade.llm.types import ProviderNotReady
from cascade.retrieval.rerank import LexicalReranker, Reranker, RerankError, apply_scores
from cascade.retrieval.schema import RetrievedChunk

# A real Bedrock rerank model id. Passed through verbatim as ``modelArn``: the
# field's published pattern makes the ``arn:`` prefix optional, so this code
# never assembles an ARN whose partition and account it cannot check.
MODEL = "amazon.rerank-v1:0"
RATE = "2.00"  # $2.00 per 1,000 queries -> $0.002 a query
DOCS = ["the first body", "the second body", "the third body"]


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def configured(settings: Settings, *, region: str | None = "us-east-1", **rerank: Any) -> Settings:
    """``settings`` with a Bedrock reranker wired, and everything else untouched."""
    fields: dict[str, Any] = {
        "enabled": True,
        "provider": "bedrock",
        "model_id": MODEL,
        "price_per_1k_queries": RATE,
    }
    fields.update(rerank)
    retrieval = settings.retrieval.model_copy(
        update={"rerank": settings.retrieval.rerank.model_copy(update=fields)}
    )
    providers = settings.providers.model_copy(
        update={"bedrock": settings.providers.bedrock.model_copy(update={"region": region})}
    )
    return settings.model_copy(update={"retrieval": retrieval, "providers": providers})


class ScriptedBedrock:
    """Answers ``rerank`` from a queue of pages and keeps every request it saw."""

    def __init__(self, *pages: dict[str, Any]) -> None:
        self.pages = list(pages)
        self.requests: list[dict[str, Any]] = []

    def rerank(self, **kwargs: Any) -> dict[str, Any]:
        self.requests.append(kwargs)
        if not self.pages:
            raise AssertionError("Rerank was called more times than the script allows")
        return self.pages.pop(0)


def page(*results: tuple[int, float], next_token: str | None = None) -> dict[str, Any]:
    """One Rerank response body, in the published shape."""
    body: dict[str, Any] = {
        "results": [{"index": index, "relevanceScore": score} for index, score in results]
    }
    if next_token is not None:
        body["nextToken"] = next_token
    return body


def meter_for(settings: Settings) -> CostMeter:
    return CostMeter(settings, "bench", ceiling_usd=Decimal("1000"))


# ---------------------------------------------------------------------------
# The seam is narrow (ADR-0047's whole argument)
# ---------------------------------------------------------------------------


def _leaky_parameters(target: type) -> list[str]:
    """Parameters on a reranker's public surface that could carry a time lock."""
    forbidden = ("as_of", "cutoff", "chunk_id", "document_id", "published", "date", "timestamp")
    found: list[str] = []
    for name in sorted(dir(target)):
        if name.startswith("_"):
            continue
        member = inspect.getattr_static(target, name)
        if not callable(member):
            continue
        for parameter in sorted(inspect.signature(member).parameters):
            if any(word in parameter.lower() for word in forbidden):
                found.append(f"{name}({parameter})")
    return found


def test_nothing_on_the_public_surface_can_carry_a_date_or_an_identifier() -> None:
    """Invariant 1 holds here by absence: there is no argument to default.

    ADR-0047 admits a managed reranker because it is handed a pool the
    database already filtered, and it cannot name a document that pool does
    not contain. Widening this signature is what would need a new record.
    """
    assert _leaky_parameters(BedrockReranker) == []


def test_the_narrowness_check_would_catch_a_widening() -> None:
    """A guard that cannot fail is not a guard."""

    class Widened:
        def score(self, *, query: str, documents: Sequence[str], as_of: str) -> list[float]:
            raise AssertionError("never called")

    assert _leaky_parameters(Widened) == ["score(as_of)"]


def test_it_satisfies_the_reranker_protocol(settings: Settings) -> None:
    """Checked by mypy strict at the assignment, and constructible here.

    `Reranker` is a plain Protocol, so this is a static conformance check that
    CI enforces rather than a runtime isinstance.
    """
    reranker: Reranker = BedrockReranker(configured(settings))
    assert reranker.model_id == MODEL


# ---------------------------------------------------------------------------
# The response is put back into input order
# ---------------------------------------------------------------------------


def test_a_relevance_ranked_answer_comes_back_positionally(settings: Settings) -> None:
    """Bedrock ranks its answer; the protocol's contract is one score per document.

    The response below says document 2 is the most relevant and document 0 the
    least. Read in arrival order it would attach 0.9 to the *first* body.
    """
    client = ScriptedBedrock(page((2, 0.9), (1, 0.5), (0, 0.1)))
    reranker = BedrockReranker(configured(settings), client=client)

    assert list(reranker.score(query="q", documents=DOCS)) == [0.1, 0.5, 0.9]


def test_the_permutation_that_reaches_the_prompt_is_the_one_bedrock_meant(
    settings: Settings,
) -> None:
    """End to end through `apply_scores`: the third chunk leads, not the first.

    This is the failure the index mapping prevents, stated as the artefact it
    would corrupt -- the ordered evidence a prompt carries.
    """
    chunks = tuple(
        RetrievedChunk(
            chunk_id=f"c{index}",
            document_id=f"d{index}",
            ordinal=0,
            body=body,
            published_at=datetime(2024, 1, 1, tzinfo=UTC),
            source="ccnews",
            url=f"https://example.invalid/{index}",
            title=f"document {index}",
            distance=0.1 * index,
        )
        for index, body in enumerate(DOCS)
    )
    client = ScriptedBedrock(page((2, 0.9), (1, 0.5), (0, 0.1)))
    reranker = BedrockReranker(configured(settings), client=client)

    ordered = apply_scores(chunks, reranker.score(query="q", documents=DOCS), top_k=3)
    assert [chunk.chunk_id for chunk in ordered] == ["c2", "c1", "c0"]


def test_a_single_document_pool_is_scored(settings: Settings) -> None:
    client = ScriptedBedrock(page((0, 0.42)))
    reranker = BedrockReranker(configured(settings), client=client)
    assert list(reranker.score(query="q", documents=["only"])) == [0.42]


# ---------------------------------------------------------------------------
# A response that cannot be applied is refused
# ---------------------------------------------------------------------------


def test_a_missing_index_is_an_error_not_a_zero(settings: Settings) -> None:
    """A defaulted score sorts, so filling the gap would reorder the evidence."""
    client = ScriptedBedrock(page((0, 0.1), (2, 0.9)))
    reranker = BedrockReranker(configured(settings), client=client)

    with pytest.raises(RerankError, match="not a zero"):
        reranker.score(query="q", documents=DOCS)


def test_a_repeated_index_is_an_error(settings: Settings) -> None:
    """Scoring one document twice leaves another unscored: not a permutation."""
    client = ScriptedBedrock(page((0, 0.1), (0, 0.2), (1, 0.3)))
    reranker = BedrockReranker(configured(settings), client=client)

    with pytest.raises(RerankError, match="twice"):
        reranker.score(query="q", documents=DOCS)


def test_an_index_outside_the_pool_is_an_error(settings: Settings) -> None:
    """It names a position in the request's own source list, so this one names nothing."""
    client = ScriptedBedrock(page((0, 0.1), (1, 0.2), (7, 0.3)))
    reranker = BedrockReranker(configured(settings), client=client)

    with pytest.raises(RerankError, match="names nothing"):
        reranker.score(query="q", documents=DOCS)


@pytest.mark.parametrize(
    "entry",
    [
        {"relevanceScore": 0.5},
        {"index": 0},
        "not a result at all",
    ],
)
def test_a_result_missing_a_required_field_is_an_error(settings: Settings, entry: Any) -> None:
    """Both fields are required by the published shape; a KeyError here would be opaque."""
    client = ScriptedBedrock({"results": [entry]})
    reranker = BedrockReranker(configured(settings), client=client)

    with pytest.raises(RerankError, match="which document it scored"):
        reranker.score(query="q", documents=DOCS)


# ---------------------------------------------------------------------------
# The whole pool is requested, not a top-N
# ---------------------------------------------------------------------------


def test_the_request_asks_for_one_score_per_document(settings: Settings) -> None:
    """``numberOfResults`` is the pool size: ``top_k`` is applied later, by `apply_scores`."""
    client = ScriptedBedrock(page((0, 0.1), (1, 0.2), (2, 0.3)))
    reranker = BedrockReranker(configured(settings), client=client)
    reranker.score(query="what happened", documents=DOCS)

    (sent,) = client.requests
    configuration = sent["rerankingConfiguration"]["bedrockRerankingConfiguration"]
    assert configuration["numberOfResults"] == len(DOCS)
    assert configuration["modelConfiguration"] == {"modelArn": MODEL}
    assert sent["rerankingConfiguration"]["type"] == "BEDROCK_RERANKING_MODEL"


def test_the_request_carries_one_query_and_the_bodies_in_order(settings: Settings) -> None:
    """One query per call -- the field is a fixed-size array of one -- and the pool as given.

    Order is in the cache key because a reranker is permitted to be
    position-sensitive, so it has to reach the service unchanged.
    """
    client = ScriptedBedrock(page((0, 0.1), (1, 0.2), (2, 0.3)))
    reranker = BedrockReranker(configured(settings), client=client)
    reranker.score(query="what happened", documents=DOCS)

    (sent,) = client.requests
    assert sent["queries"] == [{"type": "TEXT", "textQuery": {"text": "what happened"}}]
    assert [
        source["inlineDocumentSource"]["textDocument"]["text"] for source in sent["sources"]
    ] == DOCS
    assert {source["type"] for source in sent["sources"]} == {"INLINE"}


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


def test_a_continuation_token_is_followed_until_the_pool_is_scored(settings: Settings) -> None:
    """``nextToken`` is part of this API; a caller that read one page would drop scores."""
    client = ScriptedBedrock(
        page((2, 0.9), next_token="more"),
        page((1, 0.5), (0, 0.1)),
    )
    reranker = BedrockReranker(configured(settings), client=client)

    assert list(reranker.score(query="q", documents=DOCS)) == [0.1, 0.5, 0.9]
    first, second = client.requests
    assert "nextToken" not in first
    assert second["nextToken"] == "more"
    assert second["sources"] == first["sources"], "the pool is resent unchanged"


def test_a_token_that_makes_no_progress_is_refused(settings: Settings) -> None:
    """Following it again would not terminate, and the pool is still unscored."""
    client = ScriptedBedrock(
        page((0, 0.1), next_token="more"),
        page(next_token="more"),
    )
    reranker = BedrockReranker(configured(settings), client=client)

    with pytest.raises(RerankError, match="would not terminate"):
        reranker.score(query="q", documents=DOCS)


# ---------------------------------------------------------------------------
# Pools the service cannot describe
# ---------------------------------------------------------------------------


def test_an_empty_pool_is_scored_without_reaching_the_service(settings: Settings) -> None:
    """A reranker with nothing to order must not call a provider to say so."""
    client = ScriptedBedrock()
    meter = meter_for(settings)
    reranker = BedrockReranker(configured(settings), meter=meter, client=client)

    assert list(reranker.score(query="q", documents=[])) == []
    assert client.requests == []
    assert meter.units == {}


def test_a_pool_over_the_source_limit_is_refused_before_the_call(settings: Settings) -> None:
    """1,000 sources is Bedrock's published maximum; 1,001 cannot come back whole."""
    client = ScriptedBedrock()
    reranker = BedrockReranker(configured(settings), client=client)

    with pytest.raises(RerankError, match="exceeds Bedrock's limit"):
        reranker.score(query="q", documents=[f"body {n}" for n in range(1001)])
    assert client.requests == []


def test_an_empty_document_is_refused(settings: Settings) -> None:
    """Bedrock requires at least one character; a placeholder would be scored as evidence."""
    client = ScriptedBedrock()
    reranker = BedrockReranker(configured(settings), client=client)

    with pytest.raises(RerankError, match="position 1 is empty"):
        reranker.score(query="q", documents=["a", "", "c"])
    assert client.requests == []


def test_an_oversized_document_is_refused_rather_than_truncated(settings: Settings) -> None:
    """Truncating here would score text the agent never sees."""
    client = ScriptedBedrock()
    reranker = BedrockReranker(configured(settings), client=client)

    with pytest.raises(RerankError, match="over Bedrock's 32000-character limit"):
        reranker.score(query="q", documents=["x" * 32_001])
    assert client.requests == []


# ---------------------------------------------------------------------------
# Billing
# ---------------------------------------------------------------------------


def test_one_call_books_one_query_at_the_configured_rate(settings: Settings) -> None:
    """$2.00 per 1,000 queries is $0.002 for one call, whatever the pool size."""
    meter = meter_for(settings)
    client = ScriptedBedrock(page((0, 0.1), (1, 0.2), (2, 0.3)))
    reranker = BedrockReranker(configured(settings), meter=meter, client=client)
    reranker.score(query="q", documents=DOCS)

    assert meter.units == {RERANK_BILLING_KIND: 1}
    assert meter.total_usd == Decimal("0.002")
    assert meter.calls == 0, "a rerank query is not a model call"


def test_a_paginated_call_is_still_one_query(settings: Settings) -> None:
    """A continuation is part of the same query, not a second one."""
    meter = meter_for(settings)
    client = ScriptedBedrock(page((2, 0.9), next_token="more"), page((1, 0.5), (0, 0.1)))
    reranker = BedrockReranker(configured(settings), meter=meter, client=client)
    reranker.score(query="q", documents=DOCS)

    assert meter.units == {RERANK_BILLING_KIND: 1}


def test_a_refused_response_is_still_booked(settings: Settings) -> None:
    """AWS served it, so it was billed; a ledger that omitted it would understate spend."""
    meter = meter_for(settings)
    client = ScriptedBedrock(page((0, 0.1)))
    reranker = BedrockReranker(configured(settings), meter=meter, client=client)

    with pytest.raises(RerankError):
        reranker.score(query="q", documents=DOCS)
    assert meter.units == {RERANK_BILLING_KIND: 1}
    assert meter.total_usd == Decimal("0.002")


def test_wrapped_for_replay_a_hit_books_nothing_and_a_miss_books_one_query(
    settings: Settings, tmp_path: Path
) -> None:
    """The wrapper books the hits and the reranker books the misses, exactly once each.

    The paths are disjoint -- a hit never consults the inner reranker, a miss
    always does -- so one meter can be handed to both without double-counting.
    """
    from cascade.llm.cache import CallCache

    cache = CallCache(tmp_path / "cache")
    meter = meter_for(settings)
    client = ScriptedBedrock(page((0, 0.1), (1, 0.2), (2, 0.3)))
    inner = BedrockReranker(configured(settings), meter=meter, client=client)
    wrapper = RecordedReranker(inner=inner, cache=cache, mode="record", meter=meter)

    first = wrapper.score(query="q", documents=DOCS)
    second = wrapper.score(query="q", documents=DOCS)

    assert list(first) == list(second) == [0.1, 0.2, 0.3]
    assert len(client.requests) == 1, "the recording served the second call"
    assert meter.units == {RERANK_BILLING_KIND: 1}
    assert meter.cached_units == {RERANK_BILLING_KIND: 1}
    assert meter.total_usd == Decimal("0.002")


# ---------------------------------------------------------------------------
# Readiness: nothing is spent through a service that cannot be routed or priced
# ---------------------------------------------------------------------------


def test_construction_builds_no_client_and_asserts_nothing(settings: Settings) -> None:
    """Lazy, so a recorded study replays with no AWS account at all.

    A reranker wrapped for replay never reaches the client, which is why
    readiness is asserted at the network door rather than in ``__init__``.
    """
    BedrockReranker(configured(settings, region=None, price_per_1k_queries="0.00"))


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"region": None}, "providers.bedrock.region is not set"),
        ({"price_per_1k_queries": "0.00"}, "price_per_1k_queries"),
        ({"model_id": LexicalReranker.model_id}, "the local reranker's"),
        ({"model_id": "  "}, "retrieval.rerank.model_id is not set"),
    ],
)
def test_a_service_that_cannot_be_routed_or_priced_is_refused(
    settings: Settings, overrides: dict[str, Any], expected: str
) -> None:
    """Each of these would otherwise fail after a round trip, or silently at $0.00.

    A zero rate is the M8 shape: a number that agrees with itself and cannot
    fail, while spending a real phase ceiling.
    """
    reranker = BedrockReranker(configured(settings, **overrides))
    with pytest.raises(ProviderNotReady, match=expected):
        reranker.score(query="q", documents=DOCS)


def test_every_problem_is_reported_at_once(settings: Settings) -> None:
    """So the configuration is fixed in one pass, as `readiness_problems` does."""
    reranker = BedrockReranker(
        configured(settings, region=None, price_per_1k_queries="0.00", model_id="")
    )
    with pytest.raises(ProviderNotReady) as caught:
        reranker.score(query="q", documents=DOCS)
    assert len(caught.value.problems) == 3
    assert caught.value.problems == sorted(caught.value.problems)


def test_a_rate_that_is_not_a_number_fails_at_construction(settings: Settings) -> None:
    """Parsed once, so a malformed rate cannot land between the response and the ledger."""
    with pytest.raises(ValueError, match="not a decimal number"):
        BedrockReranker(configured(settings, price_per_1k_queries="two dollars"))


# ---------------------------------------------------------------------------
# Routing is explicit (ADR-0028), against the real botocore
# ---------------------------------------------------------------------------


@pytest.fixture
def aws_decoys(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Fake credentials, plus a decoy for every variable that could redirect a call.

    The credentials let botocore compute a real SigV4 signature offline. The
    decoys are the point: each is a variable boto3 would consult if a routing
    value were left unset, and a developer's shell must not decide where the
    study's spend lands.
    """
    pytest.importorskip("botocore")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIDEXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY")
    monkeypatch.delenv("AWS_SESSION_TOKEN", raising=False)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "absent-aws-config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "absent-aws-credentials"))
    monkeypatch.setenv("AWS_REGION", "eu-west-1")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-west-1")
    monkeypatch.setenv("AWS_ENDPOINT_URL", "https://decoy.invalid")
    monkeypatch.setenv("AWS_ENDPOINT_URL_BEDROCK_AGENT_RUNTIME", "https://decoy-specific.invalid")


class _RawBody:
    """The streaming shape botocore reads a response body through."""

    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def stream(self, **kwargs: Any) -> Any:
        yield self.payload


@pytest.fixture
def captured_wire(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Let real botocore build and sign the request; replace only the wire.

    The same argument as the SDK providers' mock transport: a hand-built
    double would not catch a change in the service model, and the service
    model is the thing that was verified rather than assumed.
    """
    boto3 = pytest.importorskip("boto3")
    from botocore.awsrequest import AWSResponse

    seen: list[Any] = []

    def before_send(request: Any, **kwargs: Any) -> AWSResponse:
        seen.append(request)
        body = json.dumps(
            {"results": [{"index": index, "relevanceScore": 0.5} for index in range(len(DOCS))]}
        ).encode("utf-8")
        return AWSResponse(request.url, 200, {"Content-Type": "application/json"}, _RawBody(body))

    real: Callable[..., Any] = boto3.session.Session.client

    def client(self: Any, *args: Any, **kwargs: Any) -> Any:
        built = real(self, *args, **kwargs)
        built.meta.events.register("before-send.bedrock-agent-runtime.Rerank", before_send)
        return built

    monkeypatch.setattr(boto3.session.Session, "client", client)
    return seen


@pytest.mark.usefixtures("aws_decoys")
def test_the_call_reaches_the_configured_region_not_the_ambient_one(
    settings: Settings, captured_wire: list[Any]
) -> None:
    """``AWS_REGION`` says eu-west-1 and two endpoint variables say decoy.invalid.

    Measured against the request botocore actually signed: the host is the
    configured region's, and the SigV4 credential scope names it too -- a
    signature for the wrong region would be rejected, so this is not cosmetic.
    """
    reranker = BedrockReranker(configured(settings, region="us-east-1"))
    assert list(reranker.score(query="q", documents=DOCS)) == [0.5, 0.5, 0.5]

    (sent,) = captured_wire
    assert sent.url == "https://bedrock-agent-runtime.us-east-1.amazonaws.com/rerank"
    authorization = sent.headers["Authorization"].decode("utf-8")
    assert authorization.startswith("AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/")
    assert "/us-east-1/bedrock/aws4_request" in authorization


@pytest.mark.usefixtures("aws_decoys")
def test_a_configured_base_url_outranks_the_derived_endpoint(
    settings: Settings, captured_wire: list[Any]
) -> None:
    """An explicit endpoint is a deployment's choice; an ambient one is an accident."""
    base = settings.providers.bedrock.model_copy(
        update={"region": "us-east-1", "base_url": "https://rerank.internal"}
    )
    routed = configured(settings).model_copy(
        update={"providers": settings.providers.model_copy(update={"bedrock": base})}
    )
    BedrockReranker(routed).score(query="q", documents=DOCS)

    (sent,) = captured_wire
    assert sent.url == "https://rerank.internal/rerank"


@pytest.mark.usefixtures("aws_decoys")
def test_the_request_body_is_what_the_service_model_accepts(
    settings: Settings, captured_wire: list[Any]
) -> None:
    """Serialized by botocore against its own shapes, so a wrong field name fails here.

    botocore validates the request parameters before signing; an unknown
    member or a wrong type is a ``ParamValidationError`` rather than a
    plausible call that AWS would reject at the first paid attempt.
    """
    BedrockReranker(configured(settings)).score(query="what happened", documents=DOCS)

    (sent,) = captured_wire
    body = json.loads(sent.body)
    assert body["queries"] == [{"type": "TEXT", "textQuery": {"text": "what happened"}}]
    assert len(body["sources"]) == len(DOCS)
    assert body["rerankingConfiguration"]["bedrockRerankingConfiguration"]["numberOfResults"] == 3


@pytest.mark.usefixtures("aws_decoys")
def test_the_client_is_built_once_and_reused(settings: Settings, captured_wire: list[Any]) -> None:
    """Cached on the instance: a client per query would re-resolve credentials each time."""
    reranker = BedrockReranker(configured(settings))
    reranker.score(query="q", documents=DOCS)
    first = reranker._boto3_client()
    reranker.score(query="q", documents=DOCS)

    assert reranker._boto3_client() is first
    assert len(captured_wire) == 2
