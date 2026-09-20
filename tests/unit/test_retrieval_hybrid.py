"""The hybrid client path, the mode switch and the full-text index plan (M14).

No database. The client is driven through a fake connection that records the
SQL it is handed and replies with rows shaped like `chronofence_search_hybrid`
returns. That is enough to pin what must hold *before* and *after* the
database is involved -- the time lock's Python half, the mode dispatch, and the
determinism of what is sent -- and none of what only Postgres can show, which
lives in `tests/integration/test_chronofence_hybrid.py` and
`tests/leakage/test_hybrid_poison_pill.py`.
"""

from __future__ import annotations

import ast
import inspect
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from cascade.config import RetrievalConfig, Settings
from cascade.corpus.embed import EMBEDDING_DIM
from cascade.retrieval.index import (
    FTS_EXPRESSION,
    fts_index_name_for,
    plan_fts,
    plan_fts_partition,
)
from cascade.retrieval.schema import PartitionIndex
from cascade.retrieval.search import Chronofence, TimeLockViolation

REPO_ROOT = Path(__file__).resolve().parents[2]
CUTOFF = datetime(2025, 6, 1, tzinfo=UTC)
VECTOR = [0.0] * EMBEDDING_DIM


def row(
    chunk_id: str,
    *,
    vector_rank: int | None = None,
    keyword_rank: int | None = None,
    published_at: datetime | None = None,
    document_id: str | None = None,
    simhash: int = 0,
) -> tuple[Any, ...]:
    """One row in `chronofence_search_hybrid`'s column order."""
    return (
        chunk_id,
        document_id or f"doc-{chunk_id}",
        0,
        f"body of {chunk_id}",
        published_at or CUTOFF - timedelta(days=1),
        "ccnews",
        "",
        "",
        0.5,
        vector_rank,
        keyword_rank,
        None if keyword_rank is None else 1,
        simhash,
    )


class FakeCursor:
    def __init__(self, connection: FakeConnection) -> None:
        self._connection = connection

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        self._connection.executed.append((sql, params))

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._connection.rows)


class FakeConnection:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.rows = rows
        self.executed: list[tuple[str, tuple[Any, ...]]] = []

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def close(self) -> None:
        return None


def fence_over(
    settings: Settings, rows: list[tuple[Any, ...]]
) -> tuple[Chronofence, FakeConnection]:
    fence = Chronofence(settings, role="sim")
    connection = FakeConnection(rows)
    fence._conn = connection
    return fence, connection


def in_mode(settings: Settings, mode: str) -> Settings:
    return settings.model_copy(
        update={"retrieval": settings.retrieval.model_copy(update={"mode": mode})}
    )


# ---------------------------------------------------------------------------
# Invariant 1 at the new boundary
# ---------------------------------------------------------------------------


class TestAsOf:
    @pytest.mark.parametrize("method", ["search_hybrid", "retrieve"])
    def test_as_of_is_required_and_keyword_only(self, method: str) -> None:
        """No default, in the signature itself -- so this holds whatever the body does."""
        parameter = inspect.signature(getattr(Chronofence, method)).parameters["as_of"]
        assert parameter.default is inspect.Parameter.empty
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY

    @pytest.mark.parametrize("method", ["search_hybrid", "retrieve"])
    def test_omitting_as_of_is_a_type_error(self, settings: Settings, method: str) -> None:
        fence, _ = fence_over(in_mode(settings, "hybrid"), [])
        with pytest.raises(TypeError):
            getattr(fence, method)(VECTOR, text="Will Kroger win?", k=6)

    @pytest.mark.parametrize("method", ["search_hybrid", "retrieve"])
    def test_a_naive_cutoff_is_refused_before_any_sql(
        self, settings: Settings, method: str
    ) -> None:
        """A naive timestamp is read in the server's zone and silently shifts the
        lock. The hybrid path must not be the way around that refusal."""
        fence, connection = fence_over(in_mode(settings, "hybrid"), [])
        with pytest.raises(ValueError, match="timezone-aware"):
            getattr(fence, method)(VECTOR, text="q", as_of=datetime(2025, 6, 1), k=6)
        assert connection.executed == []

    def test_the_other_request_checks_apply_too(self, settings: Settings) -> None:
        fence, connection = fence_over(settings, [])
        with pytest.raises(ValueError, match="dimensions"):
            fence.search_hybrid([0.0] * 10, text="q", as_of=CUTOFF, k=6)
        with pytest.raises(ValueError, match="max_k"):
            fence.search_hybrid(VECTOR, text="q", as_of=CUTOFF, k=settings.retrieval.max_k + 1)
        with pytest.raises(ValueError, match="positive"):
            fence.search_hybrid(VECTOR, text="q", as_of=CUTOFF, k=0)
        assert connection.executed == []

    def test_outside_a_context_manager_it_raises(self, settings: Settings) -> None:
        with pytest.raises(RuntimeError, match="context manager"):
            Chronofence(settings).search_hybrid(VECTOR, text="q", as_of=CUTOFF, k=6)


# ---------------------------------------------------------------------------
# The tripwire
# ---------------------------------------------------------------------------


class TestTimeLockTripwire:
    @pytest.mark.parametrize("offset", [timedelta(0), timedelta(seconds=1), timedelta(days=400)])
    def test_a_row_at_or_after_the_cutoff_fails_the_retrieval(
        self, settings: Settings, offset: timedelta
    ) -> None:
        """Raised, never filtered. A Python filter would hide a broken lock by
        quietly repairing its output; this turns a leak into a failed run.
        Equality counts: the promise is *strictly* before."""
        rows = [
            row("fine", vector_rank=1),
            row("leaked", keyword_rank=1, published_at=CUTOFF + offset),
        ]
        fence, _ = fence_over(settings, rows)
        with pytest.raises(TimeLockViolation, match="leaked"):
            fence.search_hybrid(VECTOR, text="Will Kroger win?", as_of=CUTOFF, k=6)

    def test_a_leak_is_not_survivable_by_asking_for_fewer_chunks(self, settings: Settings) -> None:
        """The check is over the whole union, not the k that would have been kept:
        a leaked row ranked seventh still means the lock is broken."""
        rows = [row(f"ok-{i}", vector_rank=i + 1) for i in range(8)]
        rows.append(row("leaked", vector_rank=200, published_at=CUTOFF + timedelta(days=1)))
        fence, _ = fence_over(settings, rows)
        with pytest.raises(TimeLockViolation):
            fence.search_hybrid(VECTOR, text="q", as_of=CUTOFF, k=1)

    def test_rows_strictly_before_the_cutoff_pass(self, settings: Settings) -> None:
        rows = [row("a", vector_rank=1, published_at=CUTOFF - timedelta(microseconds=1))]
        fence, _ = fence_over(settings, rows)
        assert fence.search_hybrid(VECTOR, text="q", as_of=CUTOFF, k=6).chunk_ids == ("a",)


# ---------------------------------------------------------------------------
# What is sent, and what comes back
# ---------------------------------------------------------------------------


class TestHybridCall:
    def test_the_sql_names_the_hybrid_function_and_carries_no_k(self, settings: Settings) -> None:
        fence, connection = fence_over(settings, [row("a", vector_rank=1)])
        fence.search_hybrid(VECTOR, text="Will Kroger close?", entities=("FTC",), as_of=CUTOFF, k=6)
        ((sql, params),) = connection.executed
        assert "FROM chronofence_search_hybrid(%s::halfvec, %s::text[], %s)" in sql
        assert params[1] == ["ftc", "kroger"], "entities first, then terms mined from the text"
        assert params[2] == CUTOFF
        assert len(params) == 3, "k is applied after fusion and must not reach the plan"

    def test_no_terms_is_sent_as_an_empty_array_not_skipped(self, settings: Settings) -> None:
        """The vector pool, recency and diversity still run."""
        fence, connection = fence_over(settings, [row("a", vector_rank=1)])
        result = fence.search_hybrid(VECTOR, text="Will it happen?", as_of=CUTOFF, k=6)
        assert connection.executed[0][1][1] == []
        assert result.terms == ()
        assert result.chunk_ids == ("a",)

    def test_the_result_is_fused_truncated_and_explains_itself(self, settings: Settings) -> None:
        rows = [
            row("v-only", vector_rank=1),
            row("both", vector_rank=2, keyword_rank=1),
            row("k-only", keyword_rank=2),
        ]
        fence, _ = fence_over(settings, rows)
        result = fence.search_hybrid(VECTOR, text="Will Kroger close?", as_of=CUTOFF, k=2)
        assert result.mode == "hybrid"
        assert result.candidates == 3
        # both: 1/62 + 1/61; v-only: 1/61; k-only: 1/62 -- all one age, so
        # recency adds the same to each.
        assert result.chunk_ids == ("both", "v-only")
        first = result.chunks[0]
        assert (first.vector_rank, first.keyword_rank, first.recency_rank) == (2, 1, 1)
        assert first.fused_score == pytest.approx(1 / 62 + 1 / 61 + 0.5 / 61)

    def test_the_result_does_not_depend_on_row_order(self, settings: Settings) -> None:
        rows = [
            row("a", vector_rank=3, published_at=CUTOFF - timedelta(days=90)),
            row("b", vector_rank=1, keyword_rank=4, published_at=CUTOFF - timedelta(days=2)),
            row("c", keyword_rank=1, published_at=CUTOFF - timedelta(days=2)),
            row("d", vector_rank=2, published_at=CUTOFF - timedelta(days=700)),
        ]
        forwards, _ = fence_over(settings, rows)
        backwards, _ = fence_over(settings, list(reversed(rows)))
        left = forwards.search_hybrid(VECTOR, text="q", as_of=CUTOFF, k=3)
        right = backwards.search_hybrid(VECTOR, text="q", as_of=CUTOFF, k=3)
        assert left.chunks == right.chunks

    def test_the_vector_path_is_untouched(self, settings: Settings) -> None:
        """`search` still sends what it always sent and returns no hybrid fields."""
        vector_row = row("a")[:9]
        fence, connection = fence_over(settings, [vector_row])
        result = fence.search(VECTOR, as_of=CUTOFF, k=6)
        ((sql, params),) = connection.executed
        assert "FROM chronofence_search(%s::halfvec, %s, %s)" in sql
        assert params[1:] == (CUTOFF, 6)
        assert result.mode == "vector"
        assert result.chunks[0].fused_score is None and result.chunks[0].vector_rank is None


# ---------------------------------------------------------------------------
# `retrieval.mode` is a mechanism, not a configuration field (ADR-0025)
# ---------------------------------------------------------------------------


class TestModeIsAMechanism:
    """ADR-0025's defect was two switches that were configured, documented and
    read nowhere. This one is checked the way that ADR prescribes: flipping it
    changes what the system actually does."""

    @pytest.mark.parametrize(
        ("mode", "function"),
        [("vector", "chronofence_search("), ("hybrid", "chronofence_search_hybrid(")],
    )
    def test_flipping_the_mode_changes_the_function_that_is_called(
        self, settings: Settings, mode: str, function: str
    ) -> None:
        rows = [row("a", vector_rank=1)] if mode == "hybrid" else [row("a")[:9]]
        fence, connection = fence_over(in_mode(settings, mode), rows)
        result = fence.retrieve(VECTOR, text="Will Kroger close?", as_of=CUTOFF, k=6)
        assert function in connection.executed[0][0]
        assert result.mode == mode

    def test_every_evidence_site_in_the_cli_goes_through_the_switch(self) -> None:
        """A site left calling `fence.search` would stay on vector retrieval for
        ever, silently -- and a baseline left behind while the agents moved
        would turn the headline comparison into a retrieval comparison."""
        tree = ast.parse((REPO_ROOT / "cascade" / "cli.py").read_text(encoding="utf-8"))
        calls = [
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "fence"
        ]
        # The compiler, the agents' prefixes, the single-model baselines (one
        # helper shared by both baseline commands and the injection probe), and
        # the dossier writer's pool (ADR-0037).
        assert calls.count("retrieve") == 4, calls
        assert "search" not in calls and "search_hybrid" not in calls, calls

    def test_the_shipped_mode_is_the_one_that_was_measured(self) -> None:
        """`hybrid` since 2026-09-19, set on `bench --relevance` over the
        rebuilt corpus and before anything was compiled (ADR-0040). The value
        decides what every prompt in the study contains, so it is asserted
        here rather than left to whoever last edited the config."""
        assert Settings().retrieval.mode == "hybrid"


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


class TestConfig:
    def override(self, **changes: Any) -> RetrievalConfig:
        values = Settings().retrieval.model_dump()
        values.update(changes)
        return RetrievalConfig(**values)

    def test_the_shipped_values(self) -> None:
        retrieval = Settings().retrieval
        assert (retrieval.rrf_k, retrieval.rrf_recency_weight) == (60, 0.5)
        assert (retrieval.rrf_vector_weight, retrieval.rrf_keyword_weight) == (1.0, 1.0)
        assert (retrieval.hybrid_term_candidates, retrieval.hybrid_max_terms) == (500, 8)
        assert (retrieval.diversity_max_per_story, retrieval.diversity_simhash_bits) == (2, 8)

    def test_an_unknown_mode_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            self.override(mode="keyword")

    @pytest.mark.parametrize(
        "changes",
        [
            {"rrf_k": 0},
            {"rrf_vector_weight": 0.0},
            {"rrf_keyword_weight": -1.0},
            {"rrf_recency_weight": -0.5},
            {"hybrid_term_candidates": 0},
            {"hybrid_max_terms": 0},
            {"diversity_max_per_story": 0},
            {"diversity_simhash_bits": 65},
            {"relevance_generic_name_rate": 0.0},
            {"relevance_generic_name_rate": 1.5},
        ],
    )
    def test_out_of_range_values_are_refused(self, changes: dict[str, Any]) -> None:
        with pytest.raises(ValidationError):
            self.override(**changes)

    def test_an_unknown_key_is_refused(self) -> None:
        """`extra=forbid`: a typo'd setting must not be silently ignored."""
        with pytest.raises(ValidationError):
            self.override(rrf_recency_wieght=0.5)

    def test_the_mode_can_be_set_from_the_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CASCADE_RETRIEVAL__MODE", "hybrid")
        assert Settings().retrieval.mode == "hybrid"


# ---------------------------------------------------------------------------
# The full-text index plan (ADR-0012's lifecycle)
# ---------------------------------------------------------------------------


def partition(name: str, rows: int, *, fts: str | None = None) -> PartitionIndex:
    return PartitionIndex(
        partition=name,
        rows=rows,
        index_name=f"{name}_embedding_hnsw_idx",
        m=16,
        ef_construction=64,
        fts_index_name=fts,
    )


class TestFtsIndexPlan:
    def test_an_empty_partition_is_skipped(self) -> None:
        assert plan_fts_partition(partition("chunks_2027q4", 0)).action == "skip-empty"

    def test_an_unindexed_partition_is_created_under_the_derived_name(self) -> None:
        plan = plan_fts_partition(partition("chunks_2024q1", 50_000))
        assert plan.action == "create"
        assert (
            plan.index_name == fts_index_name_for("chunks_2024q1") == "chunks_2024q1_body_fts_idx"
        )

    def test_an_indexed_partition_is_kept_however_it_grew(self) -> None:
        """GIN indexes rows as they arrive; there is no drift to rebuild for."""
        for rows in (10, 600_000, 5_000_000):
            plan = plan_fts_partition(
                partition("chunks_2017q4", rows, fts="chunks_2017q4_body_fts_idx")
            )
            assert plan.action == "keep"

    def test_an_index_under_another_name_is_kept_not_duplicated(self) -> None:
        """The view recognises the index by its expression, so a hand-built one
        is seen -- and must not be built a second time beside itself."""
        plan = plan_fts_partition(partition("chunks_2024q1", 10, fts="built_by_hand_idx"))
        assert plan.action == "keep"
        assert plan.index_name == "built_by_hand_idx"

    def test_there_is_no_rebuild_action(self) -> None:
        actions = {
            plan_fts_partition(item).action
            for item in (
                partition("a", 0),
                partition("b", 5),
                partition("c", 5, fts="c_body_fts_idx"),
            )
        }
        assert actions == {"skip-empty", "create", "keep"}

    def test_partitions_are_planned_largest_first_ties_by_name(self) -> None:
        plans = plan_fts(
            [partition("chunks_b", 10), partition("chunks_a", 10), partition("chunks_c", 900)]
        )
        assert [plan.partition for plan in plans] == ["chunks_c", "chunks_a", "chunks_b"]

    def test_the_hnsw_plan_does_not_read_the_fts_column(self) -> None:
        """The two lifecycles are independent: a missing keyword index must not
        make the vector index plan as stale."""
        from cascade.retrieval.index import plan_partition

        with_fts = plan_partition(
            partition("p", 10, fts="p_body_fts_idx"), m=16, ef_construction=64
        )
        without = plan_partition(partition("p", 10), m=16, ef_construction=64)
        assert with_fts.action == without.action == "keep"

    def test_the_indexed_expression_is_immutable_by_construction(self) -> None:
        """The one-argument `to_tsvector(text)` reads a session setting, is only
        STABLE, and Postgres refuses to index it."""
        assert FTS_EXPRESSION.startswith("to_tsvector('english',")


# ---------------------------------------------------------------------------
# The CLI surface
# ---------------------------------------------------------------------------


class TestCli:
    def test_relevance_exits_3_when_the_hybrid_path_is_not_built(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Exit 3, precondition failed -- not a traceback, and not a bench that
        starts and then spends days parsing every body in the corpus."""
        from typer.testing import CliRunner

        import cascade.retrieval.bench as bench
        from cascade.cli import app
        from cascade.version import EXIT_PRECONDITION

        def not_ready(*_: object, **__: object) -> None:
            raise bench.HybridNotReady(
                [
                    "3 non-empty partition(s) have no full-text index -- run `cascade retrieval index --fts`"
                ]
            )

        monkeypatch.setattr(bench, "run_relevance", not_ready)
        outcome = CliRunner().invoke(app, ["retrieval", "bench", "--relevance"])
        assert outcome.exit_code == EXIT_PRECONDITION
        assert "cascade retrieval index --fts" in " ".join(outcome.output.split())

    def test_a_report_prints_both_arms_by_name_the_interval_and_the_caveat(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The arm names must reach the output, not only the two columns.

        `hybrid - vector` used to be implicit in the field names, so a report
        printed from `RelevanceReport` said what it compared only by accident
        of vocabulary. With a second pairing in the tool that is a report
        nobody can interpret, so the names are data and the printer has to
        show them.

        This is red until `cascade/cli.py::_print_relevance_report` follows the
        rename, and that is the point: the printer reads these fields off an
        `Any`, so nothing else would catch it before `cascade retrieval bench
        --relevance` raised `AttributeError` at the end of a database pass.
        The mapping is `MetricComparison.vector_mean -> baseline_mean`,
        `.hybrid_mean -> candidate_mean`, `KindReport.vector_latency ->
        baseline_latency`, `.hybrid_latency -> candidate_latency`; the two
        column headers and the `hybrid - vector` delta header come from
        `report.baseline_arm` and `report.candidate_arm`.
        """
        from typer.testing import CliRunner

        import cascade.retrieval.bench as bench
        from cascade.cli import app
        from cascade.eval.schema import BootstrapInterval
        from cascade.retrieval.metrics import summarise_latency

        comparison = bench.MetricComparison(
            metric="party_mention_rate",
            label="chunks naming a party",
            names="informative names",
            n_paired=170,
            unmeasurable=10,
            baseline_mean=0.4123,
            candidate_mean=0.6789,
            interval=BootstrapInterval(point=0.2666, lo=0.2101, hi=0.3212, b=10000, p_value=0.0001),
        )
        report = bench.RelevanceReport(
            baseline_arm="hybrid",
            candidate_arm="hybrid+rerank(bm25-local-v1)",
            kinds=(
                bench.KindReport(
                    kind="baseline",
                    k=6,
                    scenarios=180,
                    queries=180,
                    comparisons=(comparison,),
                    mean_overlap=0.31,
                    queries_without_terms=5,
                    baseline_latency=summarise_latency([50.0, 60.0]),
                    candidate_latency=summarise_latency([300.0, 900.0]),
                ),
            ),
            scenarios=180,
            graphs=0,
            distinct_names=466,
            generic=("other", "pm et"),
            scenarios_without_informative_names=10,
            bootstrap_b=10000,
            elapsed_s=12.0,
        )
        monkeypatch.setattr(bench, "run_relevance", lambda *_, **__: report)
        outcome = CliRunner().invoke(app, ["retrieval", "bench", "--relevance"])
        text = " ".join(outcome.output.split())
        assert outcome.exit_code == 0, outcome.output
        for expected in (
            "0.4123",
            "0.6789",
            "+0.2666",
            "pm et",
            "not evidence about forecast",
            "bm25-local-v1",
        ):
            assert expected in text, expected

    def test_the_index_command_offers_the_full_text_build(self) -> None:
        from typer.testing import CliRunner

        from cascade.cli import app

        outcome = CliRunner().invoke(app, ["retrieval", "index", "--help"])
        assert outcome.exit_code == 0
        # Colour codes split option names on a colour terminal (CI is one).
        text = re.sub(r"\x1b\[[0-9;]*m", "", outcome.output)
        assert "--fts" in text and "--no-fts" in text
