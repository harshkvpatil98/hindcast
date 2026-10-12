"""Date monotonicity over the HYBRID retrieval trace (M14, spec §4.2 criterion 4).

The same property `test_date_monotonicity.py` asserts for the vector path, over
the path that has two ways in. It reads the function's rows directly rather
than going through the client, on purpose: the client raises on a late row, so
a test through it could only ever see the tripwire -- this one sees the SQL.

Never executed by its author; needs migration 018 and the full-text indexes.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import psycopg
import pytest
from hypothesis import HealthCheck, given
from hypothesis import settings as hyp_settings
from hypothesis import strategies as st

from cascade.config import Settings
from cascade.retrieval.bench import hybrid_readiness
from cascade.retrieval.index import measure
from cascade.retrieval.keywords import entity_terms
from cascade.retrieval.search import Chronofence, vector_literal

pytestmark = pytest.mark.leakage

HYBRID = (
    "SELECT chunk_id, published_at FROM " "chronofence_search_hybrid(%s::halfvec, %s::text[], %s)"
)


@pytest.fixture(scope="module")
def conn(live_settings: Settings) -> Any:
    with Chronofence(live_settings, role="admin") as fence:
        deployed = fence.hybrid_deployed()
    reasons = hybrid_readiness(
        deployed=deployed, partitions=measure(live_settings) if deployed else []
    )
    if reasons:
        pytest.skip("; ".join(reasons))
    with psycopg.connect(live_settings.database_url("sim"), connect_timeout=30) as bound:
        yield bound


@pytest.fixture(scope="module")
def cutoffs(records: tuple[Any, ...]) -> list[datetime]:
    return sorted({record.scenario.cutoff_ts for record in records})


@pytest.fixture(scope="module")
def queries(records: tuple[Any, ...], embedder: Any) -> list[tuple[Any, list[str]]]:
    """Real questions with their real entity terms, embedded once."""
    chosen = [record.scenario.question for record in records[:24]]
    vectors = embedder.encode(chosen)
    return [
        (vector, list(entity_terms(question, max_terms=8)))
        for question, vector in zip(chosen, vectors, strict=True)
    ]


@given(
    cutoff_index=st.integers(min_value=0, max_value=179),
    query_index=st.integers(min_value=0, max_value=23),
)
@hyp_settings(
    max_examples=120,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_no_candidate_from_either_pool_is_dated_at_or_after_its_cutoff(
    conn: Any,
    cutoffs: list[datetime],
    queries: list[tuple[Any, list[str]]],
    cutoff_index: int,
    query_index: int,
) -> None:
    """Over the whole candidate union -- up to 400 rows a call, not the six an
    agent keeps -- because a leak ranked 300th is still a broken lock."""
    as_of = cutoffs[cutoff_index % len(cutoffs)]
    vector, terms = queries[query_index % len(queries)]
    with conn.cursor() as cur:
        cur.execute(HYBRID, (vector_literal(vector), terms, as_of))
        late = [(chunk_id, stamp) for chunk_id, stamp in cur.fetchall() if stamp >= as_of]
    assert late == [], f"{len(late)} candidates at or after {as_of.isoformat()}: {late[:3]}"


@given(cutoff_index=st.integers(min_value=0, max_value=179))
@hyp_settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_an_earlier_cutoff_sees_nothing_newer(
    conn: Any, cutoffs: list[datetime], queries: list[tuple[Any, list[str]]], cutoff_index: int
) -> None:
    """Catches a predicate on the wrong side of the comparison, which a fixed
    late cutoff would never expose."""
    as_of = cutoffs[cutoff_index % len(cutoffs)] - timedelta(days=365)
    vector, terms = next((pair for pair in queries if pair[1]), queries[0])
    with conn.cursor() as cur:
        cur.execute(HYBRID, (vector_literal(vector), terms, as_of))
        stamps = [stamp for _, stamp in cur.fetchall()]
    assert all(stamp < as_of for stamp in stamps)
