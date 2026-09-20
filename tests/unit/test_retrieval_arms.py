"""Which two arms `bench --relevance` compares, and that it compares two.

ADR-0047 leaves whether reranking helps to "an ablation measured on the dev
split", and the instrument for that compared exactly two arms named `vector`
and `hybrid` in its own field names. Generalising it opens failure modes that
the two-arm version could not have, and these are them: a pairing that runs an
arm against itself and reports a precise null with a confidence interval
attached (ADR-0025's failure, one level down), a retrieval mode added to the
literal and silently read as `vector`, two arms measured on two connections so
the latency difference carries the session rather than the stage, and a report
that does not say what it compared.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar, get_args

import pytest
from pydantic import ValidationError

from cascade.config import Settings
from cascade.ledger.schema import Scenario
from cascade.retrieval.bench import (
    PAIRINGS,
    Arm,
    InertArm,
    hybrid_arm,
    mode_arm,
    pairing_arms,
    rerank_enabled,
    reranked_arm,
    vector_arm,
)
from cascade.retrieval.queries import RelevanceQuery, compiler_evidence_query
from cascade.retrieval.schema import RetrievalMode, RetrievedChunk, SearchResult

CUTOFF = datetime(2025, 6, 1, tzinfo=UTC)
VECTOR: tuple[float, ...] = (0.0,)


def query(scenario_id: str = "s00", *, k: int = 6) -> RelevanceQuery:
    return RelevanceQuery(
        scenario_id=scenario_id,
        kind="compiler",
        query=compiler_evidence_query("Will the FTC block Kroger?"),
        as_of=CUTOFF,
        k=k,
    )


def chunk(chunk_id: str, body: str = "The FTC sued.") -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk_id,
        document_id=f"doc-{chunk_id}",
        ordinal=0,
        body=body,
        published_at=CUTOFF - timedelta(days=1),
        source="ccnews",
        url="",
        title="",
        distance=0.5,
    )


@dataclass
class RecordingPort:
    """A Chronofence-shaped stub that records what each arm asked it for.

    The arms take their port as an argument precisely so this is possible
    without a database -- and so one port can serve both arms of a pairing,
    which is the property the latency numbers rest on.
    """

    rerank_model: str | None = "bm25-local-v1"
    calls: list[tuple[str, int, tuple[str, ...]]] = field(default_factory=list)

    def _result(self, k: int, *, reranked: bool, mode: str = "vector") -> SearchResult:
        return SearchResult(
            as_of=CUTOFF,
            k=k,
            chunks=(chunk("c1"), chunk("c2")),
            elapsed_ms=1.0,
            mode=mode,  # type: ignore[arg-type]
            terms=("ftc",) if mode == "hybrid" else (),
            rerank_model=self.rerank_model if reranked else None,
        )

    def search(self, vector: Any, *, as_of: datetime, k: int) -> SearchResult:
        self.calls.append(("search", k, ()))
        return self._result(k, reranked=False)

    def search_hybrid(
        self,
        vector: Any,
        *,
        text: str,
        entities: tuple[str, ...] = (),
        as_of: datetime,
        k: int,
    ) -> SearchResult:
        self.calls.append(("search_hybrid", k, entities))
        return self._result(k, reranked=False, mode="hybrid")

    def retrieve(
        self,
        vector: Any,
        *,
        text: str,
        entities: tuple[str, ...] = (),
        as_of: datetime,
        k: int,
    ) -> SearchResult:
        self.calls.append(("retrieve", k, entities))
        return self._result(k, reranked=True, mode="hybrid")


def with_mode(settings: Settings, mode: RetrievalMode) -> Settings:
    return settings.model_copy(
        update={"retrieval": settings.retrieval.model_copy(update={"mode": mode})}
    )


# ---------------------------------------------------------------------------
# What each pairing compares, by name
# ---------------------------------------------------------------------------


class TestPairings:
    def test_the_hybrid_pairing_is_the_one_that_shipped(self, settings: Settings) -> None:
        """ADR-0040's comparison, unchanged, so its published numbers still mean
        the same thing."""
        baseline, candidate = pairing_arms("hybrid", settings)
        assert (baseline.name, candidate.name) == ("vector", "hybrid")

        port = RecordingPort()
        baseline.retrieve(port, query(k=60), VECTOR)
        candidate.retrieve(port, query(k=60), VECTOR)
        assert [call[0] for call in port.calls] == ["search", "search_hybrid"]

    def test_the_rerank_pairing_names_the_mode_and_the_scorer(self, settings: Settings) -> None:
        """'hybrid+rerank(bm25-local-v1)' rather than 'candidate': a reader of
        the report must be able to tell which pool and which scorer, because
        changing either is a different experiment."""
        baseline, candidate = pairing_arms("rerank", with_mode(settings, "hybrid"))
        assert baseline.name == "hybrid"
        assert candidate.name == "hybrid+rerank(bm25-local-v1)"

    def test_under_vector_mode_the_rerank_baseline_is_the_vector_pool(
        self, settings: Settings
    ) -> None:
        """The baseline is whatever the study runs today, not whatever the
        hybrid pairing happened to call a baseline."""
        baseline, candidate = pairing_arms("rerank", with_mode(settings, "vector"))
        assert baseline.name == "vector"
        assert candidate.name.startswith("vector+rerank(")

        port = RecordingPort()
        baseline.retrieve(port, query(), VECTOR)
        assert port.calls == [("search", 6, ())]

    def test_every_declared_pairing_can_be_built(self, settings: Settings) -> None:
        names = {pairing: pairing_arms(pairing, settings) for pairing in PAIRINGS}
        assert set(names) == {"hybrid", "rerank"}
        for pairing, (baseline, candidate) in sorted(names.items()):
            assert baseline.name != candidate.name, pairing

    def test_an_unknown_pairing_is_refused(self, settings: Settings) -> None:
        with pytest.raises(AssertionError):
            pairing_arms("recency", settings)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# One port for both arms
# ---------------------------------------------------------------------------


class TestOneConnection:
    def test_both_arms_of_every_pairing_run_against_one_port(self, settings: Settings) -> None:
        """The whole reason `Arm.retrieve` takes a port instead of closing over
        a connection. Two fences would be two sessions with two
        prepared-statement caches, and the measured latency difference would
        carry that rather than the stage under test."""
        for pairing in PAIRINGS:
            baseline, candidate = pairing_arms(pairing, settings)
            port = RecordingPort()
            baseline.retrieve(port, query(), VECTOR)
            candidate.retrieve(port, query(), VECTOR)
            assert len(port.calls) == 2, pairing

    def test_the_rerank_candidate_is_the_production_retrieve_call(self, settings: Settings) -> None:
        """Not a reconstruction of it. `Chronofence.retrieve` is where the mode
        and the rerank stage are decided (ADR-0047); an arm that fetched a pool
        and scored it here would measure a stage the study does not run, and
        would drift from it silently."""
        _, candidate = pairing_arms("rerank", with_mode(settings, "hybrid"))
        port = RecordingPort()
        found = candidate.retrieve(port, query(k=6), VECTOR)
        assert [call[0] for call in port.calls] == ["retrieve"]
        assert port.calls[0][1] == 6, "the caller's k, not the rerank pool, crosses the boundary"
        assert found.rerank_model == "bm25-local-v1"

    def test_the_hybrid_arm_passes_the_keyword_query_and_the_entities(self) -> None:
        arm = hybrid_arm()
        port = RecordingPort()
        arm.retrieve(port, query(), VECTOR)
        assert port.calls == [("search_hybrid", 6, ())]


# ---------------------------------------------------------------------------
# An arm that changes nothing is an error, not a null
# ---------------------------------------------------------------------------


class TestInertArm:
    def test_a_candidate_whose_reranker_never_ran_is_refused(self, settings: Settings) -> None:
        """ADR-0025: `causal_decomposition` and `grounding` were configured,
        documented and read nowhere, and the cells that should have differed
        returned small intervals straddling zero -- indistinguishable from an
        honest 'this does not help'. A rerank arm with no reranker fails the
        same way, so it fails loudly on query one instead."""
        _, candidate = pairing_arms("rerank", settings)
        port = RecordingPort(rerank_model=None)
        with pytest.raises(InertArm, match="no reranker ran"):
            candidate.retrieve(port, query(), VECTOR)

    def test_a_reranked_result_passes_through_unchanged(self, settings: Settings) -> None:
        _, candidate = pairing_arms("rerank", settings)
        port = RecordingPort(rerank_model="cohere.rerank-v3-5")
        assert candidate.retrieve(port, query(), VECTOR).rerank_model == "cohere.rerank-v3-5"

    def test_the_baseline_arms_are_not_held_to_it(self, settings: Settings) -> None:
        """`search` and `search_hybrid` never rerank, so a baseline result with
        no `rerank_model` is correct and must not raise."""
        baseline, _ = pairing_arms("rerank", settings)
        assert baseline.retrieve(RecordingPort(rerank_model=None), query(), VECTOR).chunks


# ---------------------------------------------------------------------------
# Mode coverage: a new literal must not fall through to `vector`
# ---------------------------------------------------------------------------


class TestModeCoverage:
    def test_every_retrieval_mode_has_its_own_arm(self) -> None:
        """A third mode added to `RetrievalMode` without a branch in `mode_arm`
        would be read as `vector` and compared against itself."""
        modes = sorted(get_args(RetrievalMode))
        assert modes == ["hybrid", "vector"]
        assert sorted(mode_arm(mode).name for mode in modes) == ["hybrid", "vector"]

    def test_a_mode_with_no_arm_is_refused(self) -> None:
        with pytest.raises(AssertionError):
            mode_arm("semantic")  # type: ignore[arg-type]

    def test_a_reranked_arm_inherits_its_pools_preconditions(self) -> None:
        assert reranked_arm("hybrid", model_id="m").needs_hybrid is True
        assert reranked_arm("vector", model_id="m").needs_hybrid is False
        assert reranked_arm("vector", model_id="m").needs_rerank is True
        assert vector_arm().needs_rerank is False


# ---------------------------------------------------------------------------
# Preconditions are declared, not discovered mid-pass
# ---------------------------------------------------------------------------


class TestPreconditions:
    def test_a_rerank_pairing_under_vector_mode_needs_no_full_text_index(
        self, settings: Settings
    ) -> None:
        """Refusing it for a missing migration-018 index neither arm reads would
        report an unrelated precondition as a result."""
        arms = pairing_arms("rerank", with_mode(settings, "vector"))
        assert not any(arm.needs_hybrid for arm in arms)

    def test_the_hybrid_pairing_always_needs_one(self, settings: Settings) -> None:
        baseline, candidate = pairing_arms("hybrid", settings)
        assert (baseline.needs_hybrid, candidate.needs_hybrid) == (False, True)

    def test_only_the_rerank_pairing_asks_for_the_stage(self, settings: Settings) -> None:
        assert not any(arm.needs_rerank for arm in pairing_arms("hybrid", settings))
        assert any(arm.needs_rerank for arm in pairing_arms("rerank", settings))

    def test_an_arm_declares_nothing_by_default(self) -> None:
        bare = Arm(
            name="x",
            retrieve=lambda port, item, vector: port.search(vector, as_of=item.as_of, k=item.k),
        )
        assert (bare.needs_hybrid, bare.needs_rerank) == (False, False)


# ---------------------------------------------------------------------------
# Turning the stage on for the fence, and nothing else
# ---------------------------------------------------------------------------


class TestRerankEnabled:
    def test_the_bench_can_measure_the_stage_while_it_is_switched_off(
        self, settings: Settings
    ) -> None:
        """A comparison runnable only once reranking was already enabled could
        never be the measurement that decides whether to enable it."""
        assert settings.retrieval.rerank.enabled is False
        assert rerank_enabled(settings).retrieval.rerank.enabled is True

    def test_nothing_else_moves(self, settings: Settings) -> None:
        before = settings.retrieval.model_dump()
        after = rerank_enabled(settings).retrieval.model_dump()
        differing = sorted(key for key in sorted(before) if before[key] != after[key])
        assert differing == ["rerank"]
        rerank_before = dict(before["rerank"])
        rerank_after = dict(after["rerank"])
        assert sorted(
            key for key in sorted(rerank_before) if rerank_before[key] != rerank_after[key]
        ) == ["enabled"]

    def test_the_pool_rules_are_re_checked_when_the_stage_is_turned_on(
        self, settings: Settings
    ) -> None:
        """`RetrievalConfig`'s rules on `rerank.pool` are written `if
        self.rerank.enabled`, so a pool wider than `max_k` loads cleanly while
        the stage is off. `model_copy` does not re-validate; turning the stage
        on with one would surface at the first query as `k exceeds
        retrieval.max_k` -- an error naming neither the pool nor the reason,
        hundreds of queries into a pass."""
        wide = settings.model_copy(
            update={"retrieval": settings.retrieval.model_copy(update={"max_k": 10})}
        )
        assert wide.retrieval.rerank.pool > wide.retrieval.max_k
        with pytest.raises(ValidationError, match=r"exceeds retrieval\.max_k"):
            rerank_enabled(wide)

    def test_a_pool_narrower_than_the_smallest_k_is_refused_too(self, settings: Settings) -> None:
        narrow = settings.model_copy(
            update={
                "retrieval": settings.retrieval.model_copy(
                    update={"rerank": settings.retrieval.rerank.model_copy(update={"pool": 2})}
                )
            }
        )
        with pytest.raises(ValidationError, match="below the smallest k"):
            rerank_enabled(narrow)

    def test_it_does_not_mutate_the_settings_it_was_given(self, settings: Settings) -> None:
        rerank_enabled(settings)
        assert settings.retrieval.rerank.enabled is False


# ---------------------------------------------------------------------------
# The driver: one fence for the pass, and a report that names the arms
# ---------------------------------------------------------------------------


class FakeChronofence:
    """Records every construction, so 'one connection' is measurable offline.

    Counting constructions is the test: a driver that opened a fence per arm
    would put the two arms on two sessions, and the latency column would report
    the session rather than the stage.
    """

    log: ClassVar[list[FakeChronofence]] = []

    def __init__(self, settings: Settings, *, role: str = "sim", reranker: object = None) -> None:
        self.settings = settings
        self.role = role
        self.reranker = reranker
        self.entered = False
        self.port = RecordingPort()
        FakeChronofence.log.append(self)

    def __enter__(self) -> FakeChronofence:
        self.entered = True
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def hybrid_deployed(self) -> bool:
        return True

    def search(self, vector: Any, *, as_of: datetime, k: int) -> SearchResult:
        return self.port.search(vector, as_of=as_of, k=k)

    def search_hybrid(self, vector: Any, **kwargs: Any) -> SearchResult:
        return self.port.search_hybrid(vector, **kwargs)

    def retrieve(self, vector: Any, **kwargs: Any) -> SearchResult:
        return self.port.retrieve(vector, **kwargs)


def scenario(scenario_id: str) -> Scenario:
    return Scenario(
        scenario_id=scenario_id,
        question="Will the FTC block Kroger?",
        resolution_criterion="YES if a court enjoins the deal.",
        cutoff_ts=CUTOFF,
        resolve_ts=CUTOFF + timedelta(days=90),
        domain="regulation",
        source="curated",
        source_ref=scenario_id,
        party_rule="curated",
        party_names=("FTC", "Kroger"),
        event_group=None,
    )


@pytest.fixture
def driver(monkeypatch: pytest.MonkeyPatch) -> type[FakeChronofence]:
    import cascade.retrieval.bench as bench

    FakeChronofence.log = []
    monkeypatch.setattr(bench, "Chronofence", FakeChronofence)
    monkeypatch.setattr(
        "cascade.ledger.store.load_scenarios", lambda *_, **__: [scenario("s1"), scenario("s0")]
    )
    monkeypatch.setattr("cascade.decompose.store.load_graphs", lambda *_, **__: [])
    monkeypatch.setattr("cascade.retrieval.index.measure", lambda *_, **__: ())
    return FakeChronofence


def run(settings: Settings, **kwargs: Any) -> Any:
    from cascade.retrieval.bench import run_relevance

    return run_relevance(settings, encode=lambda texts: [[0.0] for _ in texts], **kwargs)


class TestDriver:
    def test_the_whole_pass_runs_on_one_fence(
        self, settings: Settings, driver: type[FakeChronofence]
    ) -> None:
        """Not one per arm. The rerank pairing under vector mode needs no
        readiness probe either, so one construction is the whole count."""
        report = run(with_mode(settings, "vector"), pairing="rerank")
        assert len(driver.log) == 1
        assert driver.log[0].entered is True
        assert report.scenarios == 2

    def test_that_one_fence_carries_the_stage_the_candidate_needs(
        self, settings: Settings, driver: type[FakeChronofence]
    ) -> None:
        run(with_mode(settings, "vector"), pairing="rerank")
        assert driver.log[0].settings.retrieval.rerank.enabled is True

    def test_the_hybrid_pairing_leaves_the_stage_alone(
        self, settings: Settings, driver: type[FakeChronofence]
    ) -> None:
        """Neither of its arms calls `retrieve`, so enabling a stage they never
        reach would only risk resolving a reranker nothing asked for."""
        run(settings, pairing="hybrid")
        assert [fence.settings.retrieval.rerank.enabled for fence in driver.log] == [False, False]

    def test_the_hybrid_pairing_still_probes_readiness_first(
        self, settings: Settings, driver: type[FakeChronofence]
    ) -> None:
        run(settings, pairing="hybrid")
        assert len(driver.log) == 2, "a readiness probe, then the pass"

    def test_the_report_names_both_arms(
        self, settings: Settings, driver: type[FakeChronofence]
    ) -> None:
        report = run(with_mode(settings, "hybrid"), pairing="rerank")
        assert report.baseline_arm == "hybrid"
        assert report.candidate_arm == "hybrid+rerank(bm25-local-v1)"

    def test_the_default_pairing_is_the_one_that_shipped(
        self, settings: Settings, driver: type[FakeChronofence]
    ) -> None:
        """`cascade retrieval bench --relevance` predates the parameter and must
        keep measuring what it measured."""
        report = run(settings)
        assert (report.baseline_arm, report.candidate_arm) == ("vector", "hybrid")

    def test_the_baseline_runs_first_on_every_query(
        self, settings: Settings, driver: type[FakeChronofence]
    ) -> None:
        """Whichever arm runs second reads a cache the first may have warmed, so
        the order is a property of the measurement, not of the order Python
        happens to evaluate two keyword arguments in."""
        run(settings, pairing="hybrid")
        pass_fence = driver.log[-1]
        methods = [name for name, _, _ in pass_fence.port.calls]
        assert methods == ["search", "search_hybrid"] * 4

    def test_the_reranker_reaches_the_fence(
        self, settings: Settings, driver: type[FakeChronofence]
    ) -> None:
        """Anything that reaches a service is injected, so the one call site
        keeps owning every request that leaves this process."""
        sentinel = object()
        run(with_mode(settings, "vector"), pairing="rerank", reranker=sentinel)
        assert driver.log[0].reranker is sentinel
