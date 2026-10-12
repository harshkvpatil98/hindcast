"""Static checks on migration 018, the hybrid candidate function (M14).

The keyword pool is a brand-new way for post-cutoff text to reach an agent,
and the most attractive one there is: an article written after resolution
restates the question in the question's own words, so it is the best lexical
match in the corpus. The behavioural proof is `tests/leakage/
test_hybrid_poison_pill.py`, which needs a live database. These checks need
nothing, so they run on every commit and over a migration nobody has applied
yet -- the same reasoning as `test_invariants.py`, which this file follows.

Each check is paired with a "guard the guard" case: the same assertion run
over a deliberately broken copy of the SQL, which must fail. A static check
that cannot fail is not a check.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from cascade.config import Settings
from cascade.retrieval.index import FTS_EXPRESSION

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATION = REPO_ROOT / "migrations" / "018_chronofence_hybrid.sql"

TIME_LOCK = re.compile(r"\bpublished_at\s*<\s*as_of\b")
# The pools that read `chunks`. Each must carry the lock itself, not inherit it.
POOLS_READING_THE_CORPUS = ("vector_pool", "keyword_hits")


def code_only(sql: str) -> str:
    """The SQL with `--` comments and single-quoted literals removed.

    Same crude stripping as `test_invariants._sql_code_only`, and crude in the
    same safe direction: removing text can only remove matches, so it cannot
    make a missing predicate look present.
    """
    lines = []
    for line in sql.splitlines():
        without_comment = line.split("--", 1)[0]
        lines.append(re.sub(r"'[^']*'", "''", without_comment))
    return "\n".join(lines)


def function_body(sql: str) -> str:
    """The text between the function's `$$` delimiters."""
    match = re.search(
        r"CREATE OR REPLACE FUNCTION chronofence_search_hybrid\b.*?AS \$\$(.*?)\$\$;",
        sql,
        re.DOTALL,
    )
    assert match is not None, "chronofence_search_hybrid is not defined in migration 018"
    return match.group(1)


def function_header(sql: str) -> str:
    """From `CREATE OR REPLACE FUNCTION` up to the opening `$$`."""
    match = re.search(
        r"(CREATE OR REPLACE FUNCTION chronofence_search_hybrid\b.*?)AS \$\$", sql, re.DOTALL
    )
    assert match is not None
    return match.group(1)


def cte_body(body: str, name: str) -> str:
    """The parenthesised body of one CTE, found by balancing parentheses.

    Balanced rather than regex-matched because the pools contain nested
    sub-selects, and a lazy regex would stop at the first inner `)` -- which
    would make "the predicate is inside this pool" pass on a fragment.
    """
    opening = re.search(rf"\b{re.escape(name)}\s+AS\s+(?:MATERIALIZED\s+)?\(", body)
    assert opening is not None, f"CTE {name!r} is not defined"
    depth = 1
    index = opening.end()
    while depth and index < len(body):
        depth += {"(": 1, ")": -1}.get(body[index], 0)
        index += 1
    assert depth == 0, f"CTE {name!r} has unbalanced parentheses"
    return body[opening.end() : index - 1]


@pytest.fixture(scope="module")
def sql() -> str:
    return code_only(MIGRATION.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def body(sql: str) -> str:
    return function_body(sql)


# ---------------------------------------------------------------------------
# The time lock
# ---------------------------------------------------------------------------


def lock_failures(body: str) -> list[str]:
    """Every way the function body fails to state the time lock. Empty is good."""
    failures = []
    for pool in POOLS_READING_THE_CORPUS:
        text = cte_body(body, pool)
        if not re.search(r"\bFROM\s+chunks\b", text):
            failures.append(f"{pool} no longer reads chunks; this check is pointed at nothing")
        if not TIME_LOCK.search(text):
            failures.append(f"{pool} reads chunks without `published_at < as_of`")
    # Every read of `chunks` must be inside a pool this check knows about. A
    # third, unlocked read added later would otherwise pass unnoticed.
    reads = len(re.findall(r"\bFROM\s+chunks\b", body))
    if reads != len(POOLS_READING_THE_CORPUS):
        failures.append(
            f"{reads} reads of chunks but {len(POOLS_READING_THE_CORPUS)} known pools; "
            "a new read path needs its own lock and its own entry here"
        )
    final = body[body.rindex("FROM candidates") :]
    if not TIME_LOCK.search(final):
        failures.append("the final SELECT over the union does not restate the lock")
    return failures


def test_both_pools_and_the_union_carry_the_time_lock(body: str) -> None:
    """`published_at < as_of` inside the vector pool, the keyword pool, and the union.

    The keyword pool's is the one that matters most: it is new, and nothing
    else in the suite would notice its absence without a database.
    """
    assert lock_failures(body) == []


def test_the_lock_is_strict(body: str) -> None:
    """`<`, never `<=`: a chunk published at the cutoff instant existed when the
    forecast was made, and `chronofence_search` promises strictly before."""
    assert not re.search(r"published_at\s*<=\s*as_of", body)
    assert not re.search(r"as_of\s*>=\s*\S*published_at", body)


@pytest.mark.parametrize("pool", POOLS_READING_THE_CORPUS)
def test_the_lock_check_catches_a_pool_that_lost_its_predicate(body: str, pool: str) -> None:
    """Guard the guard: strip one pool's predicate and the check must object."""
    text = cte_body(body, pool)
    broken = body.replace(text, TIME_LOCK.sub("TRUE", text))
    assert broken != body
    assert any(pool in failure for failure in lock_failures(broken))


def test_the_lock_check_catches_a_lost_union_guard(body: str) -> None:
    head, tail = body[: body.rindex("FROM candidates")], body[body.rindex("FROM candidates") :]
    assert any("union" in failure for failure in lock_failures(head + TIME_LOCK.sub("TRUE", tail)))


def test_the_lock_check_catches_a_new_unlocked_read(body: str) -> None:
    sneaky = body.replace(
        "candidates AS (", "extra AS (SELECT * FROM chunks c),\n    candidates AS (", 1
    )
    assert sneaky != body
    assert any("new read path" in failure for failure in lock_failures(sneaky))


# ---------------------------------------------------------------------------
# Invariant 1 -- as_of has no default
# ---------------------------------------------------------------------------

AS_OF_DEFAULT = re.compile(r"as_of\s+[a-z_ ]*(\bDEFAULT\b|=)", re.IGNORECASE)


def signature(sql: str) -> str:
    match = re.search(r"FUNCTION chronofence_search_hybrid\s*\((.*?)\)\s*RETURNS", sql, re.DOTALL)
    assert match is not None
    return match.group(1)


def test_as_of_is_a_required_parameter(sql: str) -> None:
    """Three parameters, `as_of` among them, and no DEFAULT anywhere in the list.

    `=` is checked as well as `DEFAULT`: Postgres accepts `as_of timestamptz =
    now()` as a parameter default, and `test_invariants` looks only for the
    keyword.
    """
    parameters = signature(sql)
    assert re.search(r"\bas_of\s+timestamptz\b", parameters)
    assert not AS_OF_DEFAULT.search(parameters)
    assert "DEFAULT" not in parameters.upper()
    assert len([part for part in parameters.split(",") if part.strip()]) == 3


@pytest.mark.parametrize(
    "broken",
    ["as_of timestamptz DEFAULT now()", "as_of timestamptz = now()"],
)
def test_the_default_check_catches_both_spellings(sql: str, broken: str) -> None:
    parameters = re.sub(r"as_of\s+timestamptz", broken, signature(sql))
    assert AS_OF_DEFAULT.search(parameters)


def test_there_is_no_k_parameter(sql: str) -> None:
    """The caller's k is applied after fusion, so it cannot enter the plan at all."""
    assert not re.search(r"\bk\s+int", signature(sql))


# ---------------------------------------------------------------------------
# ADR-0002 -- SECURITY DEFINER with a pinned search_path
# ---------------------------------------------------------------------------


def test_the_function_is_security_definer_with_a_pinned_search_path(sql: str) -> None:
    header = function_header(sql)
    assert re.search(r"\bSECURITY\s+DEFINER\b", header), (
        "SECURITY INVOKER would run as cascade_sim, which has no SELECT on chunks: the "
        "function would be unusable by exactly the role it exists for (ADR-0002)"
    )
    path = re.search(r"SET\s+search_path\s*=\s*([^\n]+)", header)
    assert path is not None, "a SECURITY DEFINER function without a pinned search_path"
    schemas = [part.strip() for part in path.group(1).split(",")]
    assert schemas == ["public", "pg_temp"], f"pg_temp must be listed, and last: {schemas}"
    assert re.search(r"\bSTABLE\b", header)


def test_the_pinned_ef_search_matches_config(sql: str) -> None:
    """The vector pool must search as widely here as in `chronofence_search`,
    or the two modes are compared at different recall."""
    match = re.search(r"SET\s+hnsw\.ef_search\s*=\s*(\d+)", function_header(sql))
    assert match is not None
    assert int(match.group(1)) == Settings().retrieval.hnsw_ef_search


# ---------------------------------------------------------------------------
# ADR-0026 -- constant LIMITs
# ---------------------------------------------------------------------------


def limits(text: str) -> list[str]:
    return re.findall(r"\bLIMIT\s+(\S+)", text)


def test_every_limit_is_an_integer_literal(sql: str) -> None:
    """A parameterised LIMIT in a non-inlinable function measured 4x (ADR-0026)."""
    found = limits(sql)
    assert found, "no LIMIT found; the check is pointed at nothing"
    assert [value for value in found if not value.rstrip(";").isdigit()] == []


def test_the_limit_check_catches_a_parameterised_limit(body: str) -> None:
    broken = body.replace("LIMIT 200", "LIMIT k", 1)
    assert [value for value in limits(broken) if not value.isdigit()] == ["k"]


def test_the_literals_match_config(body: str) -> None:
    """Config mirrors the SQL literals because it cannot supply them.

    `max_k` bounds both pools -- it is also the pool `FusionParams` derives the
    recency bound from, so a pool enlarged here alone would silently loosen
    that bound.
    """
    retrieval = Settings().retrieval
    assert limits(cte_body(body, "vector_pool")) == [str(retrieval.max_k)]
    assert limits(cte_body(body, "keyword_pool")) == [str(retrieval.max_k)]
    assert limits(cte_body(body, "keyword_hits")) == [str(retrieval.hybrid_term_candidates)]
    assert limits(cte_body(body, "wanted")) == [str(retrieval.hybrid_max_terms)]


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def order_bys(text: str) -> list[str]:
    """Each ORDER BY clause, up to its LIMIT or the line that closes its window.

    Not "up to the next `)`": the keyword scan orders by `(... <-> q)::real`,
    and stopping at that parenthesis would read the clause as ending before
    the tie-break this check exists to find.
    """
    return [
        " ".join(clause.split())
        for clause in re.findall(r"ORDER BY\s+(.*?)(?=\bLIMIT\b|\n\s*\)|\Z)", text, re.DOTALL)
    ]


@pytest.mark.parametrize("pool", ["keyword_hits", "keyword_pool", "keyword_ranked"])
def test_every_truncating_order_ends_in_chunk_id(body: str, pool: str) -> None:
    """A LIMIT over a tie is a coin toss unless the order is total.

    `chunk_id` is unique within a partition's primary key, so ending on it
    makes which rows survive a function of the data. (The vector pool's
    ORDER BY is the bare distance operator on purpose -- that is what lets
    HNSW serve it -- and its ties are broken by `vector_ranked`.)
    """
    clauses = order_bys(cte_body(body, pool))
    assert clauses, f"{pool} has no ORDER BY"
    assert all(clause.rstrip().endswith("chunk_id") for clause in clauses), clauses


def test_vector_ranks_break_ties_on_chunk_id(body: str) -> None:
    assert re.search(
        r"row_number\(\)\s+OVER\s+\(ORDER BY v\.distance, v\.chunk_id\)",
        cte_body(body, "vector_ranked"),
    )


def test_the_keyword_scan_cannot_be_served_by_hnsw(body: str) -> None:
    """The per-term ORDER BY must be on the CAST distance.

    On the bare operator the planner may walk the HNSW graph and demote the
    text predicate to a filter: slow, and *short* -- an HNSW scan returns at
    most ef_search rows, so a rare entity's matches would silently go missing.
    """
    hits = cte_body(body, "keyword_hits")
    assert re.search(r"ORDER BY \(c\.embedding <-> q\)::real, c\.chunk_id", hits)
    assert not re.search(r"ORDER BY c\.embedding <-> q", hits)


def test_the_keyword_predicate_matches_the_indexed_expression(body: str) -> None:
    """Postgres uses an expression index only for the same expression.

    `cascade retrieval index --fts` builds the index from `FTS_EXPRESSION`; if
    the function filtered on anything else -- a different configuration, the
    one-argument `to_tsvector` -- every keyword scan would be a sequential
    parse of the corpus and nothing would say why.
    """
    assert FTS_EXPRESSION == "to_tsvector('english', body)"
    raw = function_body(MIGRATION.read_text(encoding="utf-8"))
    assert FTS_EXPRESSION.replace("body", "c.body") + " @@ plainto_tsquery('english'," in raw


def test_the_query_is_built_by_a_parser_that_cannot_raise(body: str) -> None:
    """`to_tsquery` raises on malformed input; `plainto_tsquery` cannot."""
    assert "plainto_tsquery(" in body
    assert not re.search(r"(?<![a-z_])to_tsquery\(", body)
    assert "websearch_to_tsquery(" not in body


# ---------------------------------------------------------------------------
# ADR-0005 -- grants
# ---------------------------------------------------------------------------


def grants(sql: str) -> list[tuple[str, str]]:
    """`(object clause, grantees)` for every GRANT statement."""
    return [
        (" ".join(target.split()), " ".join(grantees.split()))
        for target, grantees in re.findall(r"\bGRANT\s+(.*?)\s+TO\s+(.*?);", sql, re.DOTALL)
    ]


def test_cascade_sim_is_granted_execute_on_the_function_and_nothing_else(sql: str) -> None:
    """The simulation's corpus surface is two functions, not a table or a view.

    A SELECT grant on `chunks` would make the whole boundary decorative: the
    role could read past the cutoff without calling anything.
    """
    to_sim = [target for target, grantees in grants(sql) if "cascade_sim" in grantees]
    assert to_sim == [
        "EXECUTE ON FUNCTION chronofence_search_hybrid(halfvec, text[], timestamptz)"
    ], to_sim


def test_execute_is_revoked_from_public_before_it_is_granted(sql: str) -> None:
    """CREATE FUNCTION grants EXECUTE to PUBLIC; a grant without the revoke is
    decorative, because every role inherits PUBLIC (ADR-0005)."""
    revoke = sql.find("REVOKE ALL ON FUNCTION chronofence_search_hybrid")
    grant = sql.find("GRANT EXECUTE ON FUNCTION chronofence_search_hybrid")
    assert 0 <= revoke < grant
    assert re.search(
        r"REVOKE ALL ON FUNCTION chronofence_search_hybrid\([^)]*\)\s+FROM PUBLIC", sql
    )


def test_the_grant_check_catches_a_table_grant(sql: str) -> None:
    broken = sql + "\nGRANT SELECT ON chunks TO cascade_sim;"
    to_sim = [target for target, grantees in grants(broken) if "cascade_sim" in grantees]
    assert "SELECT ON chunks" in to_sim


# ---------------------------------------------------------------------------
# What the migration must leave alone
# ---------------------------------------------------------------------------


def test_the_vector_function_is_not_redefined(sql: str) -> None:
    """Every recorded decision and the recall oracle depend on `chronofence_search`."""
    assert not re.search(r"FUNCTION\s+chronofence_search\s*\(", sql)
    assert not re.search(r"FUNCTION\s+chronofence_search_exact\s*\(", sql)


def test_no_index_is_created_by_the_migration(sql: str) -> None:
    """ADR-0012: an index built by a migration is built on an empty table."""
    assert not re.search(r"\bCREATE\s+(UNIQUE\s+)?INDEX\b", sql, re.IGNORECASE)


def test_no_table_is_altered(sql: str) -> None:
    """A stored tsvector column would rewrite ~2M rows; the index is an expression."""
    assert not re.search(r"\bALTER\s+TABLE\b", sql, re.IGNORECASE)
    assert "GENERATED ALWAYS" not in sql.upper()


def test_the_migration_is_one_transaction(sql: str) -> None:
    """Unapplied and unexecuted by its author: a failure must apply nothing."""
    statements = [line.strip() for line in sql.splitlines() if line.strip()]
    assert statements[0] == "BEGIN;"
    assert statements[-1] == "COMMIT;"


def test_the_partition_view_keeps_its_hnsw_columns_and_gains_the_fts_one(sql: str) -> None:
    view = sql[sql.index("CREATE VIEW chronofence_partitions") : sql.index("COMMENT ON VIEW")]
    for column in ("partition", "index_name", "idx.m", "ef_construction", "fts_index_name"):
        assert column in view
    # One row per partition by construction (migration 005's defect): both
    # index lookups are LATERAL ... LIMIT 1, never a fanned-out join.
    assert view.count("LEFT JOIN LATERAL") == 2
    assert view.count("LIMIT 1") == 2
