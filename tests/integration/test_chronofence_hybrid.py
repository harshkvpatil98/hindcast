"""`chronofence_search_hybrid` against a live database (M14, migration 018).

Asserted against the catalogue and the planner rather than the migration text,
for the reason `test_chronofence.py` gives: what is deployed is what runs.

**Written while the database was reserved for an ingest and never executed by
its author.** Two of these tests exist specifically to check assumptions the
migration makes about the planner that could not be checked offline -- that
the keyword scan uses the GIN expression index, and that it does *not* use
HNSW. If either fails, the design note at the top of migration 018 is wrong in
a way that matters, and the fix is a new migration.

Requires migration 018 (`cascade db migrate`) and, for the tests that touch
the keyword pool, the full-text indexes (`cascade retrieval index --fts`).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from cascade.config import Settings
from cascade.retrieval.bench import hybrid_readiness
from cascade.retrieval.index import FTS_EXPRESSION, measure, plan_fts
from cascade.retrieval.leakage import violations
from cascade.retrieval.search import Chronofence, vector_literal

pytestmark = pytest.mark.integration

SIGNATURE = "chronofence_search_hybrid(halfvec,text[],timestamptz)"
LATE_CUTOFF = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def live_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    env_file = Path(__file__).resolve().parents[2] / ".env"
    if not env_file.is_file():
        pytest.skip("no .env; run `make env` first")
    monkeypatch.setenv("CASCADE_ENV_FILE", str(env_file))
    settings = Settings()
    try:
        import psycopg

        with (
            psycopg.connect(settings.database_url("admin"), connect_timeout=5) as conn,
            conn.cursor() as cur,
        ):
            cur.execute("SELECT to_regprocedure(%s)", (SIGNATURE,))
            row = cur.fetchone()
            if row is None or row[0] is None:
                pytest.skip("chronofence_search_hybrid is absent; run `cascade db migrate`")
    except pytest.skip.Exception:
        raise
    except Exception as exc:  # noqa: BLE001 -- a skip needs its reason
        pytest.skip(f"postgres not reachable: {type(exc).__name__}")
    return settings


@pytest.fixture
def indexed_settings(live_settings: Settings) -> Settings:
    """Settings for a database whose keyword indexes are built, or skip.

    Skipped rather than failed: the index is an operational step, and these
    tests describe the path once it exists. `retrieval verify` is what fails
    on a missing index, and only when the mode requires it.
    """
    reasons = hybrid_readiness(deployed=True, partitions=measure(live_settings))
    if reasons:
        pytest.skip("; ".join(reasons))
    return live_settings


def query(settings: Settings, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    import psycopg

    with (
        psycopg.connect(settings.database_url("admin"), connect_timeout=10) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(sql, params)
        return list(cur.fetchall())


def a_stored_vector(settings: Settings) -> list[float]:
    """A real corpus embedding, used as the query so no model has to be loaded."""
    rows = query(
        settings,
        "SELECT embedding::text FROM chunks WHERE embedding IS NOT NULL "
        "AND published_at < %s ORDER BY chunk_id LIMIT 1",
        (LATE_CUTOFF,),
    )
    if not rows:
        pytest.skip("corpus is empty; run `cascade corpus build`")
    return [float(value) for value in str(rows[0][0]).strip("[]").split(",")]


def hybrid_rows(
    settings: Settings, vector: list[float], terms: list[str], as_of: datetime
) -> list[tuple[Any, ...]]:
    return query(
        settings,
        "SELECT chunk_id, published_at, vector_rank, keyword_rank, terms_matched, distance "
        "FROM chronofence_search_hybrid(%s::halfvec, %s::text[], %s)",
        (vector_literal(vector), terms, as_of),
    )


# ---------------------------------------------------------------------------
# Function attributes (ADR-0002, invariant 1)
# ---------------------------------------------------------------------------


def test_function_is_security_definer_and_stable(live_settings: Settings) -> None:
    rows = query(
        live_settings,
        "SELECT prosecdef, provolatile FROM pg_proc WHERE oid = to_regprocedure(%s)",
        (SIGNATURE,),
    )
    secdef, volatility = rows[0]
    assert secdef is True, "SECURITY INVOKER: cascade_sim could not use it (ADR-0002)"
    assert volatility == "s"


def test_function_pins_its_search_path_and_ef_search(live_settings: Settings) -> None:
    rows = query(
        live_settings, "SELECT proconfig FROM pg_proc WHERE oid = to_regprocedure(%s)", (SIGNATURE,)
    )
    config = list(rows[0][0] or [])
    search_path = [entry for entry in config if entry.startswith("search_path=")]
    assert search_path and search_path[0].endswith("pg_temp"), config
    with Chronofence(live_settings, role="admin") as fence:
        assert (
            fence.ef_search("chronofence_search_hybrid") == live_settings.retrieval.hnsw_ef_search
        )
        assert fence.ef_search("chronofence_search_hybrid") == fence.ef_search(), (
            "the two modes must search the vector index at the same breadth, or "
            "`bench --relevance` compares them at different recall"
        )


def test_as_of_has_no_default_in_the_signature(live_settings: Settings) -> None:
    rows = query(
        live_settings,
        "SELECT pronargs, pronargdefaults FROM pg_proc WHERE oid = to_regprocedure(%s)",
        (SIGNATURE,),
    )
    assert rows[0] == (3, 0), "three required arguments; as_of must have no DEFAULT"


def test_the_vector_function_was_left_alone(live_settings: Settings) -> None:
    """Every recorded decision depends on it; 018 must not have replaced it."""
    rows = query(
        live_settings,
        "SELECT pronargs FROM pg_proc WHERE oid = "
        "to_regprocedure('chronofence_search(halfvec,timestamptz,int)')",
    )
    assert rows and rows[0][0] == 3


# ---------------------------------------------------------------------------
# The partition view
# ---------------------------------------------------------------------------


def test_the_partition_view_still_returns_one_row_per_partition(live_settings: Settings) -> None:
    """Migration 005's defect, re-checked now that the view has a second LATERAL."""
    rows = query(
        live_settings,
        "SELECT count(*), count(DISTINCT partition), "
        "(SELECT count(*) FROM pg_inherits i JOIN pg_class p ON p.oid = i.inhparent "
        " WHERE p.relname = 'chunks') FROM chronofence_partitions",
    )
    total, distinct, actual = rows[0]
    assert total == distinct == actual


def test_the_view_recognises_the_index_the_command_builds(indexed_settings: Settings) -> None:
    """The view matches on the indexed expression. If `pg_get_indexdef` spells it
    differently from the LIKE pattern in migration 018, every partition reads
    as unindexed and `retrieval index --fts` would try to build each one twice."""
    plans = plan_fts(measure(indexed_settings))
    assert [plan.partition for plan in plans if plan.action == "create"] == []
    definitions = query(
        indexed_settings,
        "SELECT pg_get_indexdef(i.oid) FROM pg_class i "
        "JOIN chronofence_partitions v ON v.fts_index_name = i.relname LIMIT 1",
    )
    assert definitions and "to_tsvector" in definitions[0][0] and "gin" in definitions[0][0].lower()


# ---------------------------------------------------------------------------
# The planner assumptions migration 018 rests on
# ---------------------------------------------------------------------------


def keyword_scan_plan(settings: Settings, term: str) -> str:
    vector = a_stored_vector(settings)
    rows = query(
        settings,
        "EXPLAIN SELECT c.chunk_id FROM chunks c "  # noqa: S608 -- a module constant
        "WHERE c.published_at < %s AND c.embedding IS NOT NULL "
        f"AND {FTS_EXPRESSION.replace('body', 'c.body')} @@ plainto_tsquery('english', %s) "
        "ORDER BY (c.embedding <-> %s::halfvec)::real, c.chunk_id LIMIT 500",
        (LATE_CUTOFF, term, vector_literal(vector)),
    )
    return "\n".join(str(row[0]) for row in rows)


def test_the_keyword_scan_uses_the_expression_index(indexed_settings: Settings) -> None:
    plan = keyword_scan_plan(indexed_settings, "microsoft")
    assert "_body_fts_idx" in plan, f"the GIN expression index is not used:\n{plan}"


def test_the_keyword_scan_is_not_served_by_hnsw(indexed_settings: Settings) -> None:
    """The ORDER BY is on the cast distance so that it cannot be. An HNSW scan
    with the text predicate as a filter re-parses every body it visits and
    returns at most ef_search rows -- silently short for a rare entity."""
    plan = keyword_scan_plan(indexed_settings, "microsoft")
    assert "hnsw" not in plan.lower(), f"HNSW is serving the keyword scan:\n{plan}"


def test_post_cutoff_partitions_are_pruned_from_the_keyword_scan(
    indexed_settings: Settings,
) -> None:
    """Pruning is the performance half of the lock, never the correctness half."""
    vector = a_stored_vector(indexed_settings)
    rows = query(
        indexed_settings,
        "EXPLAIN SELECT c.chunk_id FROM chunks c WHERE c.published_at < %s "  # noqa: S608
        f"AND {FTS_EXPRESSION.replace('body', 'c.body')} @@ plainto_tsquery('english', %s) "
        "ORDER BY (c.embedding <-> %s::halfvec)::real, c.chunk_id LIMIT 500",
        (datetime(2018, 1, 1, tzinfo=UTC), "microsoft", vector_literal(vector)),
    )
    plan = "\n".join(str(row[0]) for row in rows)
    assert "chunks_2025" not in plan and "chunks_2019" not in plan, plan


# ---------------------------------------------------------------------------
# Behaviour
# ---------------------------------------------------------------------------


def test_the_vector_pool_is_the_vector_path(live_settings: Settings) -> None:
    """Same predicate, same ordering, same limit, same ef_search: the rows the
    hybrid function ranks 1..20 by vector must be `chronofence_search`'s top 20.
    With no terms this needs no keyword index."""
    vector = a_stored_vector(live_settings)
    hybrid = hybrid_rows(live_settings, vector, [], LATE_CUTOFF)
    by_rank = sorted((row[2], row[0]) for row in hybrid if row[2] is not None and row[2] <= 20)
    plain = query(
        live_settings,
        "SELECT chunk_id FROM chronofence_search(%s::halfvec, %s, 20)",
        (vector_literal(vector), LATE_CUTOFF),
    )
    assert [chunk_id for _, chunk_id in by_rank] == [row[0] for row in plain]


def test_no_terms_means_no_keyword_pool(live_settings: Settings) -> None:
    rows = hybrid_rows(live_settings, a_stored_vector(live_settings), [], LATE_CUTOFF)
    assert 0 < len(rows) <= live_settings.retrieval.max_k
    assert all(row[3] is None and row[4] is None for row in rows)


def test_the_union_is_bounded_ranked_and_time_locked(indexed_settings: Settings) -> None:
    retrieval = indexed_settings.retrieval
    rows = hybrid_rows(
        indexed_settings, a_stored_vector(indexed_settings), ["united states", "iran"], LATE_CUTOFF
    )
    assert len(rows) <= 2 * retrieval.max_k
    assert len({row[0] for row in rows}) == len(rows), "one row per chunk"
    assert all(row[2] is not None or row[3] is not None for row in rows)
    assert all(row[1] < LATE_CUTOFF for row in rows)
    assert all(row[5] is not None for row in rows), "every candidate has a distance"

    keyword = sorted(row[3] for row in rows if row[3] is not None)
    assert keyword == list(range(1, len(keyword) + 1)), "keyword ranks are 1..n, no gaps"
    assert keyword, "'united states' and 'iran' matched nothing; is the index populated?"

    # Coverage first: no chunk matching one term outranks a chunk matching two.
    ranked = sorted((row[3], row[4]) for row in rows if row[3] is not None)
    coverage = [matched for _, matched in ranked]
    assert coverage == sorted(coverage, reverse=True)


def test_more_terms_than_the_cap_are_bounded_by_the_function(indexed_settings: Settings) -> None:
    """The client refuses to send them; the function must not rely on that."""
    terms = [f"term{i}" for i in range(40)] + ["iran"]
    rows = hybrid_rows(indexed_settings, a_stored_vector(indexed_settings), terms, LATE_CUTOFF)
    assert len(rows) <= 2 * indexed_settings.retrieval.max_k


@pytest.mark.parametrize(
    "hostile",
    ["'; DROP TABLE chunks; --", "a & | ! b:*", "(((", "\\", "", "   ", "<->", "'unterminated"],
)
def test_no_term_can_raise(live_settings: Settings, hostile: str) -> None:
    """`plainto_tsquery` cannot raise on any input; a retrieval must not be
    convertible into an exception by a string."""
    hybrid_rows(live_settings, a_stored_vector(live_settings), [hostile], LATE_CUTOFF)


def test_two_calls_return_identical_rows(indexed_settings: Settings) -> None:
    vector = a_stored_vector(indexed_settings)
    first = hybrid_rows(indexed_settings, vector, ["iran", "israel"], LATE_CUTOFF)
    second = hybrid_rows(indexed_settings, vector, ["israel", "iran"], LATE_CUTOFF)
    assert first == second, "term order must not reach the result either"


def test_the_client_returns_k_fused_chunks_inside_the_lock(indexed_settings: Settings) -> None:
    vector = a_stored_vector(indexed_settings)
    with Chronofence(indexed_settings, role="sim") as fence:
        first = fence.search_hybrid(vector, text="Will Iran strike Israel?", as_of=LATE_CUTOFF, k=6)
        second = fence.search_hybrid(
            vector, text="Will Iran strike Israel?", as_of=LATE_CUTOFF, k=6
        )
    assert first.mode == "hybrid" and first.terms == ("iran", "israel")
    assert len(first.chunks) == 6
    assert violations(first.chunks, as_of=LATE_CUTOFF) == ()
    assert first.chunks == second.chunks
    assert all(chunk.fused_score is not None for chunk in first.chunks)
