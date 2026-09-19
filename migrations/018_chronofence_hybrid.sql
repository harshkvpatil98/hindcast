-- 018: chronofence_search_hybrid -- a keyword candidate pool beside the
-- vector one, both time-locked, fused outside the database (M14).
--
-- `chronofence_search` ranks by one thing: L2 distance from a 384-dimension
-- embedding. For news about named companies and people that is weak in three
-- ways the distance cannot see. A small dense embedder blurs named entities
-- ("Kroger" and "Albertsons" sit about as close to a grocery-merger query as
-- any supermarket does); an article from seventeen months before the cutoff
-- ranks the same as one from the day before; and nothing stops six slots
-- filling with six chunks of one story.
--
-- This migration adds the *candidate generation* half of the fix and nothing
-- else. Ranking -- reciprocal-rank fusion over vector rank, keyword rank and
-- recency rank, then near-duplicate suppression -- is pure Python
-- (`cascade/retrieval/fusion.py`), because it is arithmetic over at most 400
-- rows and a pure function can be property-tested where a SQL function
-- cannot. `chronofence_search` is not touched: every recorded decision and
-- the recall oracle depend on it, and `retrieval.mode` selects between them.
--
-- THE TIME LOCK. A keyword path is a brand-new way for post-cutoff text to
-- reach an agent, and the most attractive one available: a post-resolution
-- article restates the question in the question's own words, so it is the
-- best lexical match in the corpus. `published_at < as_of` therefore appears
-- THREE times below -- once inside each pool, where it also drives partition
-- pruning, and once more over the union. The third is redundant while the
-- first two are intact. That is the point of it: a later edit that loses one
-- pool's predicate still cannot leak. `tests/unit/test_hybrid_sql.py` asserts
-- all three statically and `tests/leakage/test_hybrid_poison_pill.py` asserts
-- the behaviour against the live corpus.
--
-- WHY THE KEYWORD POOL IS BUILT PER TERM, NOT FROM ONE tsquery.
-- The obvious body is `to_tsvector(body) @@ websearch_to_tsquery(query_text)`
-- ordered by `ts_rank`. It fails in both directions on this study's queries,
-- which are "<question> <actor name> <actor objective>" -- thirty-odd words:
--
--   * `plainto_tsquery` / `websearch_to_tsquery` AND every lexeme. No chunk
--     contains all thirty, so the pool is empty for exactly the long queries
--     the agents issue.
--   * OR-ing every lexeme matches most of the corpus ("win", "approve",
--     "2026"), and `ts_rank` has no IDF: a chunk saying "win" ten times
--     outranks one naming the party once.
--
-- So the caller passes `terms`: a short array of *entity phrases* chosen by
-- `cascade/retrieval/keywords.py` (proper-noun runs from the question plus the
-- actor's own name). Each term is its own `plainto_tsquery` -- an AND of that
-- phrase's lexemes, which is what a multi-word name needs. `plainto_tsquery`
-- rather than `to_tsquery` because it cannot raise a syntax error on any
-- input, so no string a caller passes can turn a retrieval into an exception.
--
-- WHY THE KEYWORD POOL IS ORDERED BY COVERAGE THEN DISTANCE, NOT ts_rank.
-- Full-text search here answers one question -- "does this chunk NAME the
-- entity?" -- which is the question the embedder answers badly. It is not
-- asked to rank. `ts_rank` is term density with no IDF, so it prefers the
-- chunk that repeats "Microsoft" nine times to the one about the merger; and
-- because the index is an *expression* index (a stored generated column would
-- rewrite ~2M rows), `ts_rank` has no tsvector to read and must re-parse the
-- body of every row it ranks -- minutes per query for a term like "United
-- States".
--
-- Instead, a chunk is ranked first by HOW MANY DISTINCT TERMS it matches --
-- coverage is the poor man's IDF: a chunk naming both Microsoft and Activision
-- is about the merger -- and then by its embedding distance to the query.
-- "Among the chunks that name this party, the ones nearest the question" is a
-- stronger ordering than term density, costs a distance computation instead of
-- a parse, and fails soft: a junk term ("company") yields chunks that are
-- still topically near the query, where ts_rank would yield arbitrary
-- business news. The list still carries signal the vector pool lacks -- it
-- reaches entity-naming chunks beyond the vector pool's 200, and a chunk in
-- both lists is credited twice by the fusion, which is precisely "this chunk
-- is on topic AND names the party".
--
-- Each term contributes its 500 NEAREST time-locked matches (a constant
-- limit, ADR-0026), per term so that a flood term cannot crowd a rare one
-- out: "Gyokeres" has fewer than 500 matches and keeps every one however
-- common its neighbour is. `terms_matched` is the number of those per-term
-- lists a chunk appears in. For a flood term that undercounts a chunk lying
-- beyond the term's nearest 500 -- deterministically, and only for chunks far
-- from the query, which is the direction in which an undercount costs least.
--
-- The per-term ORDER BY is on `(embedding <-> q)::real`, the CAST, and that
-- is load-bearing: an ORDER BY on the bare operator can be served by the HNSW
-- index with the text predicate demoted to a filter -- re-parsing every body
-- the graph walk visits, and returning fewer rows than asked because an HNSW
-- scan is bounded by ef_search. The cast makes the expression one no index
-- provides, which pins the plan to: GIN bitmap -> heap fetch -> top-N sort.
-- The distances in this pool are therefore exact, not approximate.
--
-- What is NOT bounded is a flood term's match cost: every match is
-- heap-fetched to find the nearest 500. That is one narrow row and one
-- distance per match, not a parse, and `cascade retrieval bench --relevance`
-- reports the hybrid arm's measured latency so the cost is a number rather
-- than this paragraph.
--
-- CONSTANT LIMITS (ADR-0026). The function is SECURITY DEFINER with a pinned
-- search_path, so it cannot be inlined and any parameter in a LIMIT is a
-- runtime value the planner cannot see -- measured at 4x on the vector path.
-- Every LIMIT below is a literal. There is no `k` parameter at all: the
-- function returns the whole candidate union (at most 400 rows) and the
-- caller's k is applied after fusion. `tests/unit/test_hybrid_sql.py` asserts
-- the literals equal `retrieval.max_k`, `retrieval.hybrid_term_candidates` and
-- `retrieval.hybrid_max_terms`, so config and schema cannot drift silently.
--
-- Index creation is deliberately NOT here (ADR-0012): the GIN build is tens of
-- minutes over a corpus loaded by a separate resumable phase, and a migration
-- run against an empty database would index nothing. Without the index this
-- function is correct and unusably slow; `cascade retrieval verify` and
-- `cascade retrieval bench --relevance` both exit 3 rather than let that pass.
--
-- NOT EXECUTED BY ITS AUTHOR. This migration was written while the database
-- was reserved for a production ingest. It is wrapped in one transaction, so
-- a failure applies nothing and records nothing; until it has been applied
-- once it may be corrected in place. After that it is forward-only like every
-- other.

BEGIN;

-- --------------------------------------------------------------------------
-- The partition view also reports the keyword index.
--
-- The HNSW half is unchanged from migration 014. The second LATERAL follows
-- migration 005's rule -- one row per partition by construction, never by a
-- predicate on a fanned-out join.
-- --------------------------------------------------------------------------

DROP VIEW IF EXISTS chronofence_partitions;

CREATE VIEW chronofence_partitions AS
SELECT
    part.relname::text                             AS partition,
    pg_get_expr(part.relpartbound, part.oid)::text AS bounds,
    idx.index_name,
    idx.m,
    idx.ef_construction,
    fts.fts_index_name
FROM pg_class parent
JOIN pg_inherits inh ON inh.inhparent = parent.oid
JOIN pg_class part   ON part.oid = inh.inhrelid
LEFT JOIN LATERAL (
    SELECT
        i.relname::text AS index_name,
        COALESCE(
            substring(pg_get_indexdef(i.oid) from 'm\s*=\s*''?([0-9]+)')::int,
            16  -- pgvector's default when the option is not spelled out
        ) AS m,
        COALESCE(
            substring(pg_get_indexdef(i.oid) from 'ef_construction\s*=\s*''?([0-9]+)')::int,
            64
        ) AS ef_construction
    FROM pg_index pgi
    JOIN pg_class i ON i.oid = pgi.indexrelid
    WHERE pgi.indrelid = part.oid
      AND i.relam = (SELECT oid FROM pg_am WHERE amname = 'hnsw')
    ORDER BY i.relname
    LIMIT 1
) idx ON true
LEFT JOIN LATERAL (
    SELECT i.relname::text AS fts_index_name
    FROM pg_index pgi
    JOIN pg_class i ON i.oid = pgi.indexrelid
    WHERE pgi.indrelid = part.oid
      AND pgi.indisvalid
      AND i.relam = (SELECT oid FROM pg_am WHERE amname = 'gin')
      -- Matched on the expression, not the name: an index that merely carries
      -- the expected name over a different expression cannot serve the
      -- function's predicate, and reporting it as present would let `verify`
      -- pass over a sequential scan.
      AND pg_get_indexdef(i.oid) LIKE '%to_tsvector(%english%body%'
    ORDER BY i.relname
    LIMIT 1
) fts ON true
WHERE parent.relname = 'chunks';

COMMENT ON VIEW chronofence_partitions IS
    'Exactly one row per chunks partition, with its HNSW index and build '
    'parameters and its full-text GIN index, or NULLs where absent. Read by '
    '`cascade retrieval index` and `cascade retrieval verify`.';

GRANT SELECT ON chronofence_partitions TO cascade_eval;

-- --------------------------------------------------------------------------
-- chronofence_search_hybrid
--
-- Returns the candidate UNION, not a ranked top-k: `vector_rank` and
-- `keyword_rank` are each NULL for a row its pool did not supply, and the
-- caller fuses them. `distance` is computed for every row, including
-- keyword-only ones, so a result always states how far it sat from the query.
--
-- `simhash` rides along from `documents` on the provenance join the vector
-- path already pays for. It is what the diversity pass keys on; returning the
-- 384-dimension vectors instead would be ~300 kB a query to re-derive a
-- near-duplicate signal the corpus computed at ingest.
--
-- `as_of` has no default, by design (invariant 1).
-- --------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION chronofence_search_hybrid(
    q     halfvec(384),
    terms text[],
    as_of timestamptz
)
RETURNS TABLE (
    chunk_id      text,
    document_id   text,
    ordinal       integer,
    body          text,
    published_at  timestamptz,
    source        text,
    url           text,
    title         text,
    distance      real,
    vector_rank   integer,
    keyword_rank  integer,
    terms_matched integer,
    simhash       bigint
)
LANGUAGE sql
STABLE
PARALLEL SAFE
SECURITY DEFINER
SET search_path = public, pg_temp
SET hnsw.ef_search = 400
-- A bitmap that outgrows work_mem goes lossy, and a lossy page is rechecked
-- row by row -- which for an expression index means re-parsing every body on
-- the page. A flood term's bitmap does not fit the 4MB default.
SET work_mem = '64MB'
AS $$
    WITH vector_pool AS MATERIALIZED (
        -- Identical to migration 014's pool, on purpose: same predicate, same
        -- ordering, same literal limit, so the measured plan carries over.
        SELECT
            c.chunk_id,
            c.document_id,
            c.ordinal,
            c.body,
            c.published_at,
            (c.embedding <-> q)::real AS distance
        FROM chunks c
        WHERE c.published_at < as_of
        ORDER BY c.embedding <-> q
        LIMIT 200
    ),
    vector_ranked AS (
        SELECT
            v.*,
            (row_number() OVER (ORDER BY v.distance, v.chunk_id))::integer AS vector_rank
        FROM vector_pool v
    ),
    wanted AS MATERIALIZED (
        -- Normalised and capped here as well as in the client, so the work
        -- this function does is bounded whatever a caller passes.
        SELECT DISTINCT lower(btrim(u.term)) AS term
        FROM unnest(terms) AS u(term)
        WHERE btrim(u.term) <> ''
        ORDER BY 1
        LIMIT 8
    ),
    keyword_hits AS MATERIALIZED (
        SELECT
            w.term,
            m.chunk_id,
            m.document_id,
            m.ordinal,
            m.body,
            m.published_at,
            m.distance
        FROM wanted w
        CROSS JOIN LATERAL (
            SELECT
                c.chunk_id,
                c.document_id,
                c.ordinal,
                c.body,
                c.published_at,
                (c.embedding <-> q)::real AS distance
            FROM chunks c
            WHERE c.published_at < as_of
              -- A chunk with no vector is invisible to the vector path; keeping
              -- it out here too means every candidate has a distance.
              AND c.embedding IS NOT NULL
              AND to_tsvector('english', c.body) @@ plainto_tsquery('english', w.term)
            -- The CAST keeps HNSW out of this scan; see the header.
            ORDER BY (c.embedding <-> q)::real, c.chunk_id
            LIMIT 500
        ) m
    ),
    keyword_counted AS (
        SELECT
            h.*,
            (count(*) OVER (PARTITION BY h.chunk_id, h.published_at))::integer
                AS terms_matched,
            row_number() OVER (PARTITION BY h.chunk_id, h.published_at ORDER BY h.term)
                AS copy
        FROM keyword_hits h
    ),
    keyword_pool AS MATERIALIZED (
        SELECT
            k.chunk_id,
            k.document_id,
            k.ordinal,
            k.body,
            k.published_at,
            k.distance,
            k.terms_matched
        FROM keyword_counted k
        WHERE k.copy = 1
        ORDER BY k.terms_matched DESC, k.distance, k.chunk_id
        LIMIT 200
    ),
    keyword_ranked AS (
        SELECT
            k.*,
            (row_number() OVER (
                ORDER BY k.terms_matched DESC, k.distance, k.chunk_id
            ))::integer AS keyword_rank
        FROM keyword_pool k
    ),
    candidates AS (
        SELECT
            COALESCE(v.chunk_id, k.chunk_id)         AS chunk_id,
            COALESCE(v.document_id, k.document_id)   AS document_id,
            COALESCE(v.ordinal, k.ordinal)           AS ordinal,
            COALESCE(v.body, k.body)                 AS body,
            COALESCE(v.published_at, k.published_at) AS published_at,
            COALESCE(v.distance, k.distance)         AS distance,
            v.vector_rank,
            k.keyword_rank,
            k.terms_matched
        FROM vector_ranked v
        FULL OUTER JOIN keyword_ranked k
          ON  k.chunk_id     = v.chunk_id
          AND k.published_at = v.published_at
    )
    SELECT
        f.chunk_id,
        f.document_id,
        f.ordinal,
        f.body,
        f.published_at,
        d.source,
        d.url,
        d.title,
        f.distance,
        f.vector_rank,
        f.keyword_rank,
        f.terms_matched,
        d.simhash
    FROM candidates f
    CROSS JOIN LATERAL (
        SELECT doc.source, doc.url, doc.title, doc.simhash
        FROM documents doc
        WHERE doc.document_id  = f.document_id
          AND doc.published_at = f.published_at
        LIMIT 1
    ) d
    -- The third statement of the lock, over the union. Redundant while both
    -- pools carry theirs; it exists for the day one of them does not.
    WHERE f.published_at < as_of
    ORDER BY f.chunk_id;
$$;

COMMENT ON FUNCTION chronofence_search_hybrid(halfvec, text[], timestamptz) IS
    'Time-locked candidate union for hybrid retrieval: the HNSW vector pool '
    'and a per-term full-text keyword pool, each restricted to chunks '
    'published strictly before as_of, with the rank each pool gave a row. '
    'Fusion is the caller''s. as_of has no default by design (invariant 1).';

-- --------------------------------------------------------------------------
-- Grants mirror migrations 004 and 014: revoke from PUBLIC first, because
-- CREATE FUNCTION grants EXECUTE to everyone and a grant made without the
-- revoke is decorative (ADR-0005). `cascade_sim` gains EXECUTE on this
-- function and nothing on any table or view.
-- --------------------------------------------------------------------------

REVOKE ALL ON FUNCTION chronofence_search_hybrid(halfvec, text[], timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION chronofence_search_hybrid(halfvec, text[], timestamptz)
    TO cascade_sim, cascade_eval;

COMMIT;
