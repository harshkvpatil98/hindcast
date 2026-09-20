"""Running the fan-out unbatched where the batch argument does not apply (ADR-0052).

ADR-0020 made the 50% batch discount a functional requirement: the simulate
phase is $126 batched and $252 not, against a $240 ceiling, so a provider
without a Message Batches API is refused at the batch door before any spend.
That argument is about *money*. A provider billing a flat-rate subscription
charges nothing per call, so the same phase costs the same either way and there
is no discount for an unbatched run to lose.

Three claims are pinned here, and the third is what makes the first two worth
having:

1. **The exemption is measured, not asserted.** The cost meter prices the same
   call batched and unbatched, on the exempt provider and on a paid one, and
   the numbers are compared. The flag is a consequence of the price table, not
   a substitute for it.
2. **A paid provider cannot reach the serial path**, by name, by configuration,
   or by adding a provider to the registry.
3. **The unbatched wavefront reproduces the batched wavefront's event-log
   hash, byte for byte** -- and a preparation that merely *declines* to batch
   does not. ``cache_hit`` is one of the fields ``DecisionEvent.canonical``
   hashes, so a wave that skipped preparation would write ``cache_hit=False``
   into every event and fail M8's replay criterion against its own recording.
   The failing arm is exercised, because a hash test that cannot fail proves
   nothing about the one that passes.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from cascade.aperture.policy import derive_policies
from cascade.config import Settings
from cascade.ensemble.runner import EnsembleRunner, RunTask
from cascade.llm.cache import CallCache
from cascade.llm.client import LLMClient
from cascade.llm.meter import CostMeter
from cascade.llm.providers import PROVIDERS, ProviderSpec, charges_per_call
from cascade.llm.types import LLMError, ProviderNotReady, Usage
from cascade.sim.agent import LLMAgents, prepare_actor
from cascade.sim.kernel import Loom, RunResult
from cascade.sim.prompts import ACTION_TOOL_NAME, brief_from
from tests.conftest import make_graph

AGENT_MODEL = "claude-haiku-4-5-20251001"


# ---------------------------------------------------------------------------
# A transport that serves both doors, so one client can take either shape
# ---------------------------------------------------------------------------


def _tool_message(model: str) -> dict[str, Any]:
    """One canned tool call. Identical for every turn, so the arms differ only
    in how the answer was fetched -- never in what it said."""
    return {
        "id": "msg_01TEST",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [
            {
                "type": "tool_use",
                "id": "toolu_01",
                "name": ACTION_TOOL_NAME,
                "input": {
                    "action": {
                        "type": "COMMIT",
                        "target_factor": "factor_0",
                        "magnitude": 0.4,
                        "resource_spend": 0.2,
                        "rationale": "hold the line",
                    }
                },
            }
        ],
        "stop_reason": "tool_use",
        "stop_sequence": None,
        "usage": {
            "input_tokens": 4200,
            "output_tokens": 45,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
    }


class BothDoors:
    """Serves ``/v1/messages`` and the batch endpoints, counting each."""

    def __init__(self) -> None:
        self.single_calls = 0
        self.submissions = 0
        self._custom_ids: list[str] = []

    def client(self) -> httpx.Client:
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if request.method == "POST" and url.endswith("/v1/messages/batches"):
                self.submissions += 1
                body = json.loads(request.content)
                self._custom_ids = [item["custom_id"] for item in body["requests"]]
                return httpx.Response(
                    200,
                    json={
                        "id": "msgbatch_01",
                        "type": "message_batch",
                        "processing_status": "in_progress",
                    },
                )
            if request.method == "GET" and url.endswith("/results"):
                lines = [
                    json.dumps(
                        {
                            "custom_id": custom_id,
                            "result": {"type": "succeeded", "message": _tool_message(AGENT_MODEL)},
                        }
                    )
                    # Reversed on purpose: the API promises no ordering, and a
                    # harness matching positionally would pass by accident.
                    for custom_id in reversed(self._custom_ids)
                ]
                return httpx.Response(
                    200,
                    text="\n".join(lines),
                    headers={"content-type": "application/x-jsonl"},
                )
            if request.method == "GET" and "/v1/messages/batches/" in url:
                return httpx.Response(
                    200,
                    json={
                        "id": "msgbatch_01",
                        "type": "message_batch",
                        "processing_status": "ended",
                        "results_url": (
                            "https://api.anthropic.com/v1/messages/batches/msgbatch_01/results"
                        ),
                    },
                )
            if request.method == "POST" and url.endswith("/v1/messages"):
                self.single_calls += 1
                body = json.loads(request.content)
                return httpx.Response(200, json=_tool_message(body["model"]))
            raise AssertionError(f"unexpected request: {request.method} {url}")

        return httpx.Client(transport=httpx.MockTransport(handler))


# ---------------------------------------------------------------------------
# The two arms, and the arm that is supposed to fail
# ---------------------------------------------------------------------------


class SerialAgents(LLMAgents):
    """The unbatched shape, forced. What a subscription provider selects."""

    @property
    def submits_batches(self) -> bool:
        return False


class UnpreparedAgents(LLMAgents):
    """The tempting wrong answer: decline to batch and decide turn by turn.

    Kept as a test subject rather than shipped. It resolves nothing up front,
    so every ``decide`` reaches the model and writes ``cache_hit=False``.
    """

    @property
    def submits_batches(self) -> bool:
        return False

    def prepare(self, turns: Any) -> int:
        return 0


def _client(
    settings: Settings,
    tmp_path: Path,
    transport: BothDoors | None,
    *,
    mode: str = "record",
    **kwargs: Any,
) -> LLMClient:
    client = LLMClient(
        settings.model_copy(update={"llm": settings.llm.model_copy(update={"mode": mode})}),
        phase="simulate",
        cache=CallCache(tmp_path / "llm"),
        http_client=transport.client() if transport is not None else None,
        **kwargs,
    )
    client._sleep = lambda _seconds: None  # type: ignore[method-assign]
    return client


def _agents(
    settings: Settings,
    graph: Any,
    client: LLMClient,
    *,
    cls: type[LLMAgents] = LLMAgents,
) -> LLMAgents:
    prepared = {
        (graph.scenario_id, actor.id): prepare_actor(
            brief_from(
                actor,
                levers={"factor_0": 0.4},
                counterparties=tuple(sorted(a.id for a in graph.actors if a.id != actor.id))[:2],
                horizon=settings.kernel.steps,
                question_context="Will the coalition hold through the vote?",
                evidence=tuple(
                    (f"2019-0{i + 1}-01T00:00:00+00:00", "govpr", "e" * 1200) for i in range(6)
                ),
            ),
            settings,
        )
        for actor in sorted(graph.actors, key=lambda a: a.id)
    }
    return cls(settings=settings, client=client, prepared=prepared)


def _hash_of_one_run(
    settings: Settings,
    tmp_path: Path,
    *,
    cls: type[LLMAgents] = LLMAgents,
) -> tuple[str, BothDoors, EnsembleRunner, list[RunResult]]:
    """Run one replicate through the wavefront and return its event-log hash."""
    transport = BothDoors()
    graph = make_graph(n_actors=8, n_factors=4, scenario_id="s1")
    decider = _agents(settings, graph, _client(settings, tmp_path, transport), cls=cls)
    loom = Loom(
        settings=settings,
        graph=graph,
        policies=derive_policies(graph, settings.aperture, asymmetry=True),
        decider=decider,
    )
    results: list[RunResult] = []
    runner = EnsembleRunner(
        settings=settings,
        loom_for=lambda _scenario_id: loom,
        on_complete=results.append,
        policy="agent",
    )
    runner.execute([RunTask(scenario_id="s1", config_id="base", replicate=0)], wave=1)
    assert len(results) == 1
    return results[0].event_log_hash, transport, runner, results


# ---------------------------------------------------------------------------
# 1. The exemption is measured, not asserted
# ---------------------------------------------------------------------------


def test_a_subscription_is_priced_identically_batched_and_not(settings: Settings) -> None:
    """The whole argument, in the units it is made in.

    ADR-0020's ceiling is a dollar figure, so the exemption has to be a dollar
    figure too. Under ``claude_code`` the price table is zero by construction
    (``Settings.pricing_table``), so the batch multiplier multiplies nothing.
    """
    usage = Usage(input_tokens=4200, output_tokens=45)
    subscription = CostMeter(
        settings.model_copy(
            update={"llm": settings.llm.model_copy(update={"provider": "claude_code"})}
        ),
        "simulate",
    )

    batched = subscription.price_of(settings.models.agent, usage, batch=True)
    unbatched = subscription.price_of(settings.models.agent, usage, batch=False)

    assert batched == Decimal(0)
    assert unbatched == Decimal(0)
    assert batched == unbatched


def test_a_paid_provider_pays_strictly_more_unbatched(settings: Settings) -> None:
    """The control. Without it the test above would pass for a broken meter."""
    usage = Usage(input_tokens=4200, output_tokens=45)
    paid = CostMeter(settings, "simulate")

    batched = paid.price_of(settings.models.agent, usage, batch=True)
    unbatched = paid.price_of(settings.models.agent, usage, batch=False)

    assert batched > Decimal(0)
    assert unbatched > batched
    # §12.1's multiplier, read off the meter rather than restated.
    assert batched * 2 == unbatched


def test_an_unbatched_subscription_run_books_nothing(settings: Settings, tmp_path: Path) -> None:
    """A run that costs nothing must record nothing, or `trace cost` lies.

    M8 found a reconciliation that compared $0.00 against $0.00 and passed, and
    the fix was to keep *nothing to reconcile* apart from *reconciled*. An
    unbatched subscription phase has to stay on the first side of that line:
    booking a notional list price for calls a subscription served would invent
    a spend no provider record could ever match.
    """
    _, _transport, _runner, results = _hash_of_one_run(settings, tmp_path, cls=SerialAgents)
    subscription = CostMeter(
        settings.model_copy(
            update={"llm": settings.llm.model_copy(update={"provider": "claude_code"})}
        ),
        "simulate",
    )
    for _ in range(results[0].llm_calls):
        subscription.record(
            model=settings.models.agent,
            usage=Usage(input_tokens=4200, output_tokens=45),
            batch=False,
        )

    assert results[0].llm_calls > 0
    assert subscription.calls == results[0].llm_calls
    assert subscription.total_usd == Decimal(0)


# ---------------------------------------------------------------------------
# 2. A paid provider cannot reach the serial path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("provider", "charged"),
    [("anthropic", True), ("aws", True), ("bedrock", True), ("claude_code", False)],
)
def test_every_provider_that_charges_is_bound_by_the_ceiling(provider: str, charged: bool) -> None:
    """Bedrock is the case that matters: no batches *and* per-call billing.

    If the exemption keyed off ``supports_batches`` instead of billing, Bedrock
    would take it and the simulate phase would run at roughly twice the rate
    its ceiling was set against.
    """
    assert charges_per_call(provider) is charged
    assert PROVIDERS["bedrock"].supports_batches is False
    assert PROVIDERS["claude_code"].supports_batches is False


def test_an_unknown_provider_is_charged() -> None:
    """A typo must not buy an exemption from the ceiling."""
    assert charges_per_call("clause_code") is True
    assert charges_per_call("") is True


def test_the_exemption_follows_billing_and_not_the_name(monkeypatch: Any) -> None:
    """A synthetic paid provider is refused; a synthetic free one is not.

    The registry is patched rather than the predicate, so this measures what a
    fifth provider added later would actually get.
    """
    paid = ProviderSpec(
        name="anthropic",  # a real Literal member; only `billing` is under test
        operated_by="a synthetic partner that charges per token",
        supports_batches=False,
        client_class=None,
        pricing_source="its own invoice",
        billing="per_token",
    )
    free = ProviderSpec(
        name="anthropic",
        operated_by="a synthetic partner on a flat rate",
        supports_batches=False,
        client_class=None,
        pricing_source="a subscription",
        billing="subscription",
    )
    registry = dict(PROVIDERS)

    monkeypatch.setitem(registry, "anthropic", paid)
    monkeypatch.setattr("cascade.llm.providers.PROVIDERS", registry)
    assert charges_per_call("anthropic") is True

    registry["anthropic"] = free
    assert charges_per_call("anthropic") is False


def test_zeroing_a_price_table_does_not_exempt_a_paid_provider(settings: Settings) -> None:
    """The guard reads the registry, never configuration.

    ``providers.bedrock.pricing`` is a YAML field. If the exemption were
    derived from the price table, an operator who zeroed it -- by accident or
    to silence a readiness error -- would also switch off the ceiling that
    guards the study's cost claim, and nothing would say so.
    """
    zeroed = settings.model_copy(
        update={
            "llm": settings.llm.model_copy(update={"provider": "bedrock"}),
            "providers": settings.providers.model_copy(
                update={
                    "bedrock": settings.providers.bedrock.model_copy(update={"pricing": {}}),
                }
            ),
        }
    )

    assert zeroed.llm.provider == "bedrock"
    assert charges_per_call(zeroed.llm.provider) is True


def test_the_agents_ask_the_client_that_will_serve_the_call(
    settings: Settings, tmp_path: Path
) -> None:
    """Not ``self.settings``: two copies of one fact can disagree.

    An agent built against a subscription configuration and handed a client
    routed at a paid provider must batch, because the client is what spends.
    """
    transport = BothDoors()
    graph = make_graph(n_actors=8, n_factors=4, scenario_id="s1")
    subscription_settings = settings.model_copy(
        update={"llm": settings.llm.model_copy(update={"provider": "claude_code"})}
    )
    paid_client = _client(settings, tmp_path, transport)

    agents = _agents(subscription_settings, graph, paid_client)

    assert agents.settings.llm.provider == "claude_code"
    assert paid_client.provider == "anthropic"
    assert agents.submits_batches is True


def test_a_client_that_names_no_provider_is_charged(settings: Settings) -> None:
    """Fail closed. A double that forgot to say who it is must not go serial."""

    class Nameless:
        pass

    graph = make_graph(n_actors=8, n_factors=4, scenario_id="s1")
    assert _agents(settings, graph, Nameless()).submits_batches is True  # type: ignore[arg-type]


def test_the_batch_door_still_refuses_a_subscription(settings: Settings, tmp_path: Path) -> None:
    """The exemption lives in the decider, so the door is unchanged.

    ``complete_batch`` has exactly one contract -- these go out as one
    submission at the batch rate -- and it is shared with the baseline phase.
    A door that sometimes looped instead would change that phase's cost model
    without a word.
    """
    from cascade.llm.types import BatchItem, LLMRequest

    transport = BothDoors()
    client = _client(
        settings.model_copy(
            update={"llm": settings.llm.model_copy(update={"provider": "claude_code"})}
        ),
        tmp_path,
        transport,
        cli_runner=lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("no CLI call expected")),
    )
    item = BatchItem(
        custom_id="t0",
        request=LLMRequest(
            model=AGENT_MODEL,
            messages=[{"role": "user", "content": "turn 0"}],
            temperature=0.7,
            max_tokens=512,
            prompt_rev="r2",
        ),
    )

    with pytest.raises(ProviderNotReady) as caught:
        client.complete_batch([item])

    # The refusal names the remedy that applies to *this* provider.
    assert "subscription" in str(caught.value)
    assert "prepares serially" in str(caught.value)
    assert transport.submissions == 0


# ---------------------------------------------------------------------------
# 3. The unbatched wavefront reproduces the event-log hash
# ---------------------------------------------------------------------------


def test_the_unbatched_wavefront_reproduces_the_batched_hash(
    settings: Settings, tmp_path: Path
) -> None:
    """ADR-0020's strongest claim, extended to the shape it did not have.

    Batching is a cost change and must not be a behaviour change. Resolving the
    same wave one call at a time is the same statement one step further: the
    answers, their order and every field the hash covers are identical, and
    only the number of HTTP requests differs.
    """
    batched, batch_transport, _r, _res = _hash_of_one_run(settings, tmp_path / "batched")
    serial, serial_transport, _r2, _res2 = _hash_of_one_run(
        settings, tmp_path / "serial", cls=SerialAgents
    )

    assert batched == serial
    # And the two really did take different doors.
    assert batch_transport.submissions == settings.kernel.steps
    assert batch_transport.single_calls == 0
    assert serial_transport.submissions == 0
    assert serial_transport.single_calls > 0


def test_declining_to_prepare_changes_the_hash(settings: Settings, tmp_path: Path) -> None:
    """The failing arm, so the test above is a measurement and not a tautology.

    ``cache_hit`` is inside ``DecisionEvent.canonical``. A decider that skipped
    preparation would let every ``decide`` reach the model, write
    ``cache_hit=False`` into every event, and produce runs that fail M8's
    byte-identical criterion against their own recordings -- while looking,
    from the outside, exactly like a correct unbatched run.
    """
    batched, _t, _r, _res = _hash_of_one_run(settings, tmp_path / "batched")
    unprepared, transport, _r2, _res2 = _hash_of_one_run(
        settings, tmp_path / "unprepared", cls=UnpreparedAgents
    )

    assert unprepared != batched
    assert transport.submissions == 0
    assert transport.single_calls > 0


def test_all_three_drivers_agree_on_replay(settings: Settings, tmp_path: Path) -> None:
    """Replay is where the M8 criterion is read, and all three shapes agree there.

    §2.3's compiled graph is a third driver, and `cascade trace replay` uses it
    for every run whatever recorded it. So the statement that matters is that
    the batched wavefront, the unbatched wavefront and the single-run driver
    all reproduce one hash from one set of recordings -- and reach it without
    constructing an SDK client at all, which is M0's criterion 2.
    """
    graph = make_graph(n_actors=8, n_factors=4, scenario_id="s1")
    policies = derive_policies(graph, settings.aperture, asymmetry=True)
    spec = RunTask(scenario_id="s1", config_id="base", replicate=0).spec(policy="agent")
    shared = tmp_path / "shared"

    # One recording pass, through the batched wavefront the study uses.
    recorder = _client(settings, shared, BothDoors())
    loom = Loom(
        settings=settings,
        graph=graph,
        policies=policies,
        decider=_agents(settings, graph, recorder),
    )
    recorded: list[RunResult] = []
    EnsembleRunner(
        settings=settings,
        loom_for=lambda _scenario_id: loom,
        on_complete=recorded.append,
        policy="agent",
    ).execute([RunTask(scenario_id="s1", config_id="base", replicate=0)], wave=1)

    hashes: dict[str, str] = {"recorded": recorded[0].event_log_hash}
    for name, cls in (("batched", LLMAgents), ("serial", SerialAgents)):
        client = _client(settings, shared, None, mode="replay")
        replayed: list[RunResult] = []
        replay_loom = Loom(
            settings=settings,
            graph=graph,
            policies=policies,
            decider=_agents(settings, graph, client, cls=cls),
        )
        EnsembleRunner(
            settings=settings,
            loom_for=lambda _scenario_id, _loom=replay_loom: _loom,  # type: ignore[misc]
            on_complete=replayed.append,
            policy="agent",
        ).execute([RunTask(scenario_id="s1", config_id="base", replicate=0)], wave=1)
        hashes[name] = replayed[0].event_log_hash
        assert client.constructed_sdk_client is False

    single = _client(settings, shared, None, mode="replay")
    hashes["single_run"] = (
        Loom(
            settings=settings,
            policies=policies,
            graph=graph,
            decider=_agents(settings, graph, single, cls=SerialAgents),
        )
        .run(spec)
        .event_log_hash
    )
    assert single.constructed_sdk_client is False

    assert len(set(hashes.values())) == 1, hashes


def test_duplicate_turns_in_a_wave_still_cost_one_call(settings: Settings, tmp_path: Path) -> None:
    """The batch path deduplicates explicitly; this one arrives second.

    Replicates that have not yet diverged produce byte-identical requests. The
    batch submits them once; the serial path hits the content-addressed cache
    on the second. Same saving, slower route -- and a serial path that did not
    get this would spend the study's allowance on exactly the duplication the
    cache exists to exploit.
    """
    transport = BothDoors()
    graph = make_graph(n_actors=8, n_factors=4, scenario_id="s1")
    decider = _agents(settings, graph, _client(settings, tmp_path, transport), cls=SerialAgents)
    loom = Loom(
        settings=settings,
        graph=graph,
        policies=derive_policies(graph, settings.aperture, asymmetry=True),
        decider=decider,
    )
    handle = loom.start(
        RunTask(scenario_id="s1", config_id="base", replicate=0).spec(policy="agent")
    )
    turns = loom.observe_step(handle)
    assert turns

    decider.prepare([turns[0], turns[0], turns[0]])

    assert transport.single_calls == 1


def test_the_serial_path_refuses_live_mode(settings: Settings, tmp_path: Path) -> None:
    """The same refusal the batch door makes, for the same reason.

    Live bypasses the cache, so a prepared turn would be answered here and
    asked again at decide time. Under a subscription that is two calls against
    a usage limit rather than two charges, which is still twice the work and
    two different answers to one turn.
    """
    transport = BothDoors()
    graph = make_graph(n_actors=8, n_factors=4, scenario_id="s1")
    live = LLMClient(
        settings.model_copy(update={"llm": settings.llm.model_copy(update={"mode": "live"})}),
        phase="simulate",
        cache=CallCache(tmp_path / "llm"),
        http_client=transport.client(),
    )
    decider = _agents(settings, graph, live, cls=SerialAgents)
    loom = Loom(
        settings=settings,
        graph=graph,
        policies=derive_policies(graph, settings.aperture, asymmetry=True),
        decider=decider,
    )
    handle = loom.start(
        RunTask(scenario_id="s1", config_id="base", replicate=0).spec(policy="agent")
    )
    turns = loom.observe_step(handle)

    with pytest.raises(LLMError, match="live mode"):
        decider.prepare(turns)

    assert transport.single_calls == 0


# ---------------------------------------------------------------------------
# The report, and invariant 8
# ---------------------------------------------------------------------------


def test_a_subscription_fan_out_reports_its_turns_as_unbatched(
    settings: Settings, tmp_path: Path
) -> None:
    transport = BothDoors()
    graph = make_graph(n_actors=8, n_factors=4, scenario_id="s1")
    decider = _agents(settings, graph, _client(settings, tmp_path, transport), cls=SerialAgents)
    loom = Loom(
        settings=settings,
        graph=graph,
        policies=derive_policies(graph, settings.aperture, asymmetry=True),
        decider=decider,
    )
    results: list[RunResult] = []
    runner = EnsembleRunner(
        settings=settings,
        loom_for=lambda _scenario_id: loom,
        on_complete=results.append,
        policy="agent",
    )

    report = runner.execute([RunTask(scenario_id="s1", config_id="base", replicate=0)], wave=1)

    assert report.batches == 0
    assert report.batched_turns == 0
    assert report.unbatched_turns == results[0].decisions
    assert transport.submissions == 0


def test_a_batched_fan_out_reports_no_unbatched_turns(settings: Settings, tmp_path: Path) -> None:
    """The control: the new column must stay empty on the path it does not describe."""
    transport = BothDoors()
    graph = make_graph(n_actors=8, n_factors=4, scenario_id="s1")
    decider = _agents(settings, graph, _client(settings, tmp_path, transport))
    loom = Loom(
        settings=settings,
        graph=graph,
        policies=derive_policies(graph, settings.aperture, asymmetry=True),
        decider=decider,
    )
    results: list[RunResult] = []
    runner = EnsembleRunner(
        settings=settings,
        loom_for=lambda _scenario_id: loom,
        on_complete=results.append,
        policy="agent",
    )

    report = runner.execute([RunTask(scenario_id="s1", config_id="base", replicate=0)], wave=1)

    assert report.unbatched_turns == 0
    assert report.batches == settings.kernel.steps
    assert report.batched_turns == results[0].decisions


def test_an_unbatched_phase_resumes_by_set_difference(settings: Settings, tmp_path: Path) -> None:
    """Invariant 8 is unchanged: the plan never consults the decider.

    Resumability is a set difference against the runs table, and preparation
    shape is downstream of it. A run row exists only for a run that finished,
    so an interrupted unbatched wave leaves exactly the unfinished replicates
    to do.
    """
    transport = BothDoors()
    graph = make_graph(n_actors=8, n_factors=4, scenario_id="s1")
    decider = _agents(settings, graph, _client(settings, tmp_path, transport), cls=SerialAgents)
    loom = Loom(
        settings=settings,
        graph=graph,
        policies=derive_policies(graph, settings.aperture, asymmetry=True),
        decider=decider,
    )
    stored: set[int] = set()
    results: list[RunResult] = []
    runner = EnsembleRunner(
        settings=settings,
        loom_for=lambda _scenario_id: loom,
        on_complete=results.append,
        policy="agent",
        completed=lambda _scenario_id, _config_id: set(stored),
    )

    tasks, skipped = runner.plan(scenario_ids=["s1"], config_id="base", replicates=3)
    assert skipped == 0 and len(tasks) == 3

    runner.execute(tasks[:1], wave=1)
    stored.add(0)

    remaining, skipped_now = runner.plan(scenario_ids=["s1"], config_id="base", replicates=3)
    assert skipped_now == 1
    assert [task.replicate for task in remaining] == [1, 2]
