"""Poison pill through the KEYWORD pool (M14, migration 018).

The hybrid path adds a second way into the corpus, and it is the more
dangerous of the two. The vector probe (`test_poison_pill.py`) plants documents
that sit *near* a query. This one plants documents that **match it exactly**:
every poison carries a nonce word that occurs nowhere else in the corpus, and
the keyword pool is then asked for that word. With no time filter the poison is
not merely a good match, it is the *only* match -- so if the lock in the
keyword pool were missing, mis-typed, or on the wrong side of the comparison,
this is the query that would show it.

Three controls keep the probe from passing because it tested nothing:

* the nonce is findable by a raw full-text query with no time filter;
* the nonce is returned **by the function itself**, through the keyword pool,
  when `as_of` is moved past the poison's date -- so its absence at the real
  cutoff is the lock and not a broken keyword pool;
* a document dated one microsecond *before* a cutoff is returned, and one dated
  exactly *at* it is not -- the lock is strict and is on the right side.

Everything runs in a transaction that is rolled back, as in the vector probe.

**Written while the database was reserved for an ingest; never executed by its
author.** It needs migration 018 and `cascade retrieval index --fts`.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import timedelta
from typing import Any

import psycopg
import pytest

from cascade.config import Settings
from cascade.retrieval.bench import hybrid_readiness
from cascade.retrieval.index import FTS_EXPRESSION, measure
from cascade.retrieval.keywords import entity_terms
from cascade.retrieval.leakage import (
    POISON_MARKER,
    PoisonDocument,
    build_poison_documents,
    insert_poison,
    scenario_of,
    violations,
)
from cascade.retrieval.search import Chronofence, vector_literal

pytestmark = pytest.mark.leakage

HYBRID = (
    "SELECT chunk_id, document_id, published_at, vector_rank, keyword_rank "
    "FROM chronofence_search_hybrid(%s::halfvec, %s::text[], %s)"
)


def nonce_for(scenario_id: str) -> str:
    """A word no news article contains, stable per scenario, safe as a term."""
    digest = hashlib.blake2b(scenario_id.encode("utf-8"), digest_size=6).hexdigest()
    return f"zqpoison{digest}"


@pytest.fixture(scope="module")
def hybrid_settings(live_settings: Settings) -> Settings:
    with Chronofence(live_settings, role="admin") as fence:
        deployed = fence.hybrid_deployed()
    reasons = hybrid_readiness(
        deployed=deployed, partitions=measure(live_settings) if deployed else []
    )
    if reasons:
        pytest.skip("; ".join(reasons))
    return live_settings


@pytest.fixture
def poisoned(hybrid_settings: Settings, records: tuple[Any, ...], embedder: Any) -> Any:
    """One nonce-bearing post-resolution document per scenario, uncommitted."""
    base = build_poison_documents(records, count=len(records))
    documents = tuple(
        replace(document, body=f"{document.body} {nonce_for(document.scenario_id)}")
        for document in base
    )
    vectors = embedder.encode([document.body for document in documents])

    conn = psycopg.connect(hybrid_settings.database_url("admin"), connect_timeout=30)
    conn.autocommit = False
    try:
        assert insert_poison(conn, documents, vectors) == len(documents)
        yield conn, documents, vectors
    finally:
        # Never commit. This is the line that keeps the probe from becoming
        # the leak it is testing for.
        conn.rollback()
        conn.close()


def hybrid(conn: Any, vector: Any, terms: list[str], as_of: Any) -> list[tuple[Any, ...]]:
    with conn.cursor() as cur:
        cur.execute(HYBRID, (vector_literal(vector), terms, as_of))
        return list(cur.fetchall())


# ---------------------------------------------------------------------------
# Controls
# ---------------------------------------------------------------------------


def test_the_nonce_is_findable_when_there_is_no_time_filter(poisoned: Any) -> None:
    """Control 1: the keyword index sees the poison. Without this, a keyword
    pool that matched nothing at all would pass every assertion below."""
    conn, documents, _ = poisoned
    document = documents[0]
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT document_id FROM chunks WHERE {FTS_EXPRESSION} "  # noqa: S608
            "@@ plainto_tsquery('english', %s)",
            (nonce_for(document.scenario_id),),
        )
        found = [row[0] for row in cur.fetchall()]
    assert found == [document.document_id], (
        "the nonce must match the poison and nothing else in the corpus; " f"matched {found[:5]}"
    )


def test_the_function_returns_the_poison_once_as_of_is_moved_past_it(poisoned: Any) -> None:
    """Control 2: through the function, through the keyword pool, at rank 1.

    So the keyword pool works, reaches the poison, and ranks it first -- and the
    only thing that changes between this call and the real one is `as_of`.
    """
    conn, documents, vectors = poisoned
    document, vector = documents[0], vectors[0]
    rows = hybrid(
        conn, vector, [nonce_for(document.scenario_id)], document.published_at + timedelta(days=1)
    )
    mine = [row for row in rows if row[1] == document.document_id]
    assert mine, "the poison was not retrievable even with as_of moved past its date"
    assert mine[0][4] == 1, f"expected keyword rank 1, got {mine[0][4]}"


# ---------------------------------------------------------------------------
# The criterion
# ---------------------------------------------------------------------------


def test_no_scenario_retrieves_its_own_poison_through_the_keyword_pool(
    poisoned: Any, records: tuple[Any, ...]
) -> None:
    """0 of 180. Each scenario asks the keyword pool for its own nonce -- a term
    only its own post-resolution document contains -- at its own cutoff, with
    the poison's own vector. Nothing may come back that is dated at or after
    the cutoff, from either pool, poison or real."""
    conn, documents, vectors = poisoned
    own: list[str] = []
    late: list[str] = []

    for document, vector in zip(documents, vectors, strict=True):
        rows = hybrid(conn, vector, [nonce_for(document.scenario_id)], document.cutoff_ts)
        for chunk_id, document_id, published_at, _vector_rank, keyword_rank in rows:
            if scenario_of(str(document_id)) == document.scenario_id:
                own.append(f"{chunk_id} (keyword_rank={keyword_rank})")
            if published_at >= document.cutoff_ts:
                late.append(f"{document.scenario_id}:{chunk_id}@{published_at}")

    assert len(documents) == len(records)
    assert own == [], f"{len(own)} scenarios retrieved their own post-resolution text: {own[:5]}"
    assert late == [], f"{len(late)} rows dated at or after their query's cutoff: {late[:5]}"


def test_the_study_s_real_terms_do_not_reach_it_either(
    poisoned: Any, records: tuple[Any, ...]
) -> None:
    """The same, asked the way the study asks: entity terms mined from the
    question. The poison restates the question verbatim, so it matches every
    one of them -- it is the best lexical match for the scenario's real query
    that the corpus could contain."""
    conn, documents, vectors = poisoned
    questions = {record.scenario.scenario_id: record.scenario.question for record in records}
    leaked: list[str] = []
    asked = 0
    for document, vector in zip(documents, vectors, strict=True):
        terms = list(entity_terms(questions[document.scenario_id], max_terms=8))
        if not terms:
            continue
        asked += 1
        for chunk_id, document_id, published_at, _, _ in hybrid(
            conn, vector, terms, document.cutoff_ts
        ):
            if published_at >= document.cutoff_ts or (
                scenario_of(str(document_id)) == document.scenario_id
            ):
                leaked.append(f"{document.scenario_id}:{chunk_id}@{published_at}")
    assert asked > 100, f"only {asked} scenarios produced any term; the probe barely ran"
    assert leaked == [], leaked[:5]


def test_the_lock_is_strict_and_on_the_right_side(
    hybrid_settings: Settings, records: tuple[Any, ...], embedder: Any
) -> None:
    """Control 3. One microsecond before the cutoff is evidence; the cutoff
    instant itself is not. A `<=`, or a comparison the wrong way round, passes
    a test that only ever plants poison months late."""
    scenario = records[0].scenario
    nonce_before, nonce_at = "zqbeforecutoff", "zqexactlyatcutoff"

    def planted(name: str, nonce: str, published_at: Any) -> PoisonDocument:
        return PoisonDocument(
            document_id=f"{POISON_MARKER}|{scenario.scenario_id}|{name}",
            scenario_id=scenario.scenario_id,
            body=f"{scenario.question} {nonce}",
            published_at=published_at,
            cutoff_ts=scenario.cutoff_ts,
        )

    documents = (
        planted("before", nonce_before, scenario.cutoff_ts - timedelta(microseconds=1)),
        planted("at", nonce_at, scenario.cutoff_ts),
    )
    vectors = embedder.encode([document.body for document in documents])

    conn = psycopg.connect(hybrid_settings.database_url("admin"), connect_timeout=30)
    conn.autocommit = False
    try:
        insert_poison(conn, documents, vectors)
        rows = hybrid(conn, vectors[0], [nonce_before, nonce_at], scenario.cutoff_ts)
    finally:
        conn.rollback()
        conn.close()

    returned = {row[1] for row in rows}
    assert documents[0].document_id in returned, "a pre-cutoff document was withheld"
    assert documents[1].document_id not in returned, "a document dated AT the cutoff came back"


def test_a_null_as_of_returns_nothing_rather_than_everything(hybrid_settings: Settings) -> None:
    """`published_at < NULL` is NULL, never true: the failure mode is empty."""
    with (
        psycopg.connect(hybrid_settings.database_url("eval"), connect_timeout=10) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(
            "SELECT count(*) FROM chronofence_search_hybrid(%s::halfvec, %s::text[], NULL)",
            (vector_literal([0.0] * 384), ["iran"]),
        )
        row = cur.fetchone()
    assert row is not None and row[0] == 0


def test_as_of_cannot_be_omitted_at_the_sql_boundary(hybrid_settings: Settings) -> None:
    """Two arguments must fail to *resolve* -- not read the present."""
    with (
        psycopg.connect(hybrid_settings.database_url("eval"), connect_timeout=10) as conn,
        conn.cursor() as cur,
        pytest.raises(psycopg.errors.UndefinedFunction),
    ):
        cur.execute(
            "SELECT * FROM chronofence_search_hybrid(%s::halfvec, %s::text[])",
            (vector_literal([0.0] * 384), ["iran"]),
        )


# ---------------------------------------------------------------------------
# The grant boundary
# ---------------------------------------------------------------------------


def test_sim_can_execute_the_hybrid_function_and_still_cannot_read_the_corpus(
    hybrid_settings: Settings,
) -> None:
    """SECURITY DEFINER is what makes the first half work; migration 018 granting
    nothing on any table is what keeps the second half true."""
    from datetime import UTC, datetime

    with Chronofence(hybrid_settings, role="sim") as fence:
        result = fence.search_hybrid(
            [0.0] * 384, text="Will Iran act?", as_of=datetime(2019, 6, 1, tzinfo=UTC), k=5
        )
    assert len(result.chunks) <= 5

    for table in ("chunks", "documents", "chronofence_partitions", "scenario_labels"):
        with (
            psycopg.connect(hybrid_settings.database_url("sim"), connect_timeout=10) as conn,
            conn.cursor() as cur,
            pytest.raises(psycopg.errors.InsufficientPrivilege),
        ):
            cur.execute(f"SELECT 1 FROM {table} LIMIT 1")  # noqa: S608


def test_the_hybrid_function_is_not_executable_by_public(hybrid_settings: Settings) -> None:
    with (
        psycopg.connect(hybrid_settings.database_url("admin"), connect_timeout=10) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(
            "SELECT proacl::text FROM pg_proc WHERE oid = "
            "to_regprocedure('chronofence_search_hybrid(halfvec,text[],timestamptz)')"
        )
        row = cur.fetchone()
    assert row is not None and row[0] is not None, "default ACL: PUBLIC still holds EXECUTE"
    remainder = str(row[0])
    for role in ("cascade_admin", "cascade_sim", "cascade_eval"):
        remainder = remainder.replace(f"{role}=X/", "")
    assert "=X/" not in remainder, f"EXECUTE granted beyond the three roles: {row[0]}"


# ---------------------------------------------------------------------------
# The committed corpus, through the client
# ---------------------------------------------------------------------------


def test_nothing_in_the_committed_corpus_postdates_its_cutoff(
    hybrid_settings: Settings, records: tuple[Any, ...], embedder: Any
) -> None:
    """The real population, the real queries, the real client -- whose tripwire
    would raise rather than return a late row, so this also shows it is quiet."""
    scenarios = [record.scenario for record in records]
    vectors = embedder.encode([scenario.question for scenario in scenarios])
    offenders: list[str] = []
    with Chronofence(hybrid_settings, role="sim") as fence:
        for scenario, vector in zip(scenarios, vectors, strict=True):
            result = fence.search_hybrid(
                vector, text=scenario.question, as_of=scenario.cutoff_ts, k=60
            )
            offenders.extend(
                f"{scenario.scenario_id}:{bad.chunk_id}"
                for bad in violations(result.chunks, as_of=scenario.cutoff_ts)
            )
    assert offenders == []


def test_the_corpus_is_unchanged_after_the_probe(hybrid_settings: Settings) -> None:
    with (
        psycopg.connect(hybrid_settings.database_url("admin"), connect_timeout=30) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(
            "SELECT (SELECT count(*) FROM chunks WHERE document_id LIKE %s), "
            "(SELECT count(*) FROM documents WHERE document_id LIKE %s)",
            (f"{POISON_MARKER}|%", f"{POISON_MARKER}|%"),
        )
        row = cur.fetchone()
    assert row == (0, 0), "poison survived the probe"
