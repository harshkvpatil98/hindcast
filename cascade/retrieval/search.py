"""The Python side of the Chronofence boundary (spec §4.2, ADR-0002).

``as_of`` is keyword-only and has no default in every function here, mirroring
the SQL signature. That is invariant 1 and it is enforced twice on purpose:
Python raises ``TypeError`` before a connection is opened, and Postgres raises
a function-resolution error if the Python layer is ever bypassed. A retrieval
helper with a default ``as_of`` is how the entire study gets silently
invalidated, so there is nowhere in this package to add one.

The connection is held open across queries because the bench measures query
latency against a 15 ms budget, and a fresh TCP connection and authentication
handshake per query would be most of that budget measuring the wrong thing.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from datetime import datetime
from types import TracebackType
from typing import Any, Literal

from cascade.config import Settings
from cascade.corpus.embed import EMBEDDING_DIM
from cascade.retrieval.fusion import FusionParams, select
from cascade.retrieval.keywords import entity_terms
from cascade.retrieval.rerank import LexicalReranker, Reranker, apply_scores
from cascade.retrieval.schema import HybridCandidate, RetrievedChunk, SearchResult

__all__ = ["Chronofence", "Role", "TimeLockViolation", "fusion_params", "vector_literal"]

Role = Literal["sim", "eval", "admin"]

# The cast is `::halfvec`, deliberately without a `(384)` typmod.
#
# A typmod cast is executed by pgvector's `halfvec(halfvec, integer, boolean)`
# coercion function, and migration 001 revokes EXECUTE on every function in
# `public` from PUBLIC (ADR-0005) -- which catches pgvector's own functions,
# because the extension installs into `public`. Measured: `cascade_eval` gets
# "permission denied for function halfvec" on `'[...]'::halfvec(384)` while
# `'[...]'::halfvec` succeeds, since a bare cast uses the type's input
# function, which is not privilege-checked.
#
# Granting EXECUTE on the coercion function would work and would also punch a
# hole in deny-by-default for every role, to buy a dimension check that
# `_call` performs in Python with a better error message. Function-parameter
# typmods are not enforced by Postgres anyway, so the cast was never the thing
# validating the width.
_QUERY_TEMPLATE = (
    "SELECT chunk_id, document_id, ordinal, body, published_at, "
    "source, url, title, distance "
    "FROM {function}(%s::halfvec, %s, %s)"
)


# The hybrid function takes no `k`: it returns the candidate union and the
# caller's k is applied after fusion, so no caller-supplied number can enter
# the plan at all (ADR-0026). `::text[]` is explicit because an empty Python
# list reaches Postgres with no element type to infer one from.
_HYBRID_QUERY = (
    "SELECT chunk_id, document_id, ordinal, body, published_at, source, url, title, "
    "distance, vector_rank, keyword_rank, terms_matched, simhash "
    "FROM chronofence_search_hybrid(%s::halfvec, %s::text[], %s)"
)


class TimeLockViolation(RuntimeError):
    """A retrieval function returned a chunk dated at or after ``as_of``.

    The lock is enforced in SQL, three times over, and nothing in Python
    filters on a date -- a Python filter would *hide* a broken lock by quietly
    repairing its output. This is the opposite: a tripwire that turns a leak
    into a failed run. It should be unreachable, and it is raised rather than
    asserted so that ``python -O`` cannot remove it.
    """


def fusion_params(settings: Settings) -> FusionParams:
    """The configured fusion, validated -- including the recency bound.

    Built from settings in one place so the client and the bench cannot fuse
    under different parameters and then be compared as though they had not.
    """
    retrieval = settings.retrieval
    return FusionParams(
        rrf_k=retrieval.rrf_k,
        vector_weight=retrieval.rrf_vector_weight,
        keyword_weight=retrieval.rrf_keyword_weight,
        recency_weight=retrieval.rrf_recency_weight,
        pool=retrieval.max_k,
        max_per_story=retrieval.diversity_max_per_story,
        simhash_bits=retrieval.diversity_simhash_bits,
    )


def vector_literal(values: Sequence[float]) -> str:
    """Render a query vector in pgvector's text input format.

    ``repr`` rather than ``str`` so a float round-trips exactly; a truncated
    query vector would perturb distances and make a recall measurement
    irreproducible for reasons invisible in the output.
    """
    return "[" + ",".join(repr(float(value)) for value in values) + "]"


class Chronofence:
    """A time-locked view of the corpus, bound to one database role.

    Use as a context manager. Opened as ``sim`` -- the default and the role
    the simulation actually runs as -- only ``search`` is available, because
    ``chronofence_search_exact`` is not granted to ``cascade_sim``. That is
    the point: the simulation gets exactly one corpus read path, and this
    class cannot quietly give it a second.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        role: Role = "sim",
        reranker: Reranker | None = None,
    ) -> None:
        self._settings = settings
        self._role = role
        self._conn: Any = None
        # Injected rather than constructed, like the compiler's `embed` and
        # `retrieve` (M4): a reranker that reaches a network is a shell
        # concern, and injecting it keeps this class testable against a
        # deterministic one. None means "resolve from config", which for the
        # local provider is a pure object this module may build itself.
        self._reranker = reranker

    def __enter__(self) -> Chronofence:
        import psycopg

        self._conn = psycopg.connect(self._settings.database_url(self._role), connect_timeout=30)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    @property
    def role(self) -> Role:
        return self._role

    def _require_connection(self) -> Any:
        if self._conn is None:
            raise RuntimeError("Chronofence must be used as a context manager")
        return self._conn

    def _call(
        self,
        function: str,
        vector: Sequence[float],
        *,
        as_of: datetime,
        k: int,
    ) -> SearchResult:
        """Invoke one of the two chronofence functions and time it.

        ``as_of`` is passed positionally into a signature with no DEFAULT, so
        omitting it upstream is a Python ``TypeError`` and could not reach here
        as a NULL. A NULL that did reach here would return zero rows rather
        than every row -- ``published_at < NULL`` is NULL, never true -- so the
        failure mode of this boundary is empty, not leaky.
        """
        self._validate(vector, as_of=as_of, k=k)

        conn = self._require_connection()
        started = time.perf_counter()
        with conn.cursor() as cur:
            cur.execute(
                _QUERY_TEMPLATE.format(function=function),
                (vector_literal(vector), as_of, k),
            )
            rows = cur.fetchall()
        elapsed_ms = (time.perf_counter() - started) * 1000.0

        chunks = tuple(
            RetrievedChunk(
                chunk_id=str(row[0]),
                document_id=str(row[1]),
                ordinal=int(row[2]),
                body=str(row[3]),
                published_at=row[4],
                source=str(row[5]),
                url=str(row[6]),
                title=str(row[7]),
                distance=float(row[8]),
            )
            for row in rows
        )
        return SearchResult(as_of=as_of, k=k, chunks=chunks, elapsed_ms=elapsed_ms)

    def _validate(self, vector: Sequence[float], *, as_of: datetime, k: int) -> None:
        """Refuse a malformed request before a connection is touched.

        Shared by both search paths so the hybrid one cannot be the way around
        a check the vector one makes -- above all the naive-``as_of`` refusal,
        which is part of the time lock.
        """
        if k <= 0:
            raise ValueError(f"k must be positive, got {k}")
        max_k = self._settings.retrieval.max_k
        if k > max_k:
            # `chronofence_search` draws a pool of `max_k` candidates with a
            # constant limit so the caller's k stays out of the plan (migration
            # 014). Top-k of top-N equals top-k only while k <= N, so beyond it
            # the function would quietly return the pool's first k rather than
            # the corpus's -- a wrong answer that looks like a right one.
            raise ValueError(
                f"k={k} exceeds retrieval.max_k={max_k}, which is the candidate pool "
                "`chronofence_search` draws. Raise max_k and the pool limit together "
                "in a new migration, or ask for fewer chunks."
            )
        if len(vector) != EMBEDDING_DIM:
            # Checked here because the SQL cast below carries no typmod (see
            # `_QUERY_TEMPLATE`), so Postgres would not catch a wrong-sized
            # query vector until the distance operator did -- by which point
            # the error names an operator rather than the caller's mistake.
            raise ValueError(
                f"query vector has {len(vector)} dimensions, expected {EMBEDDING_DIM}; "
                "the query and the corpus must come from the same pinned embedding model"
            )
        if as_of.tzinfo is None:
            raise ValueError(
                f"as_of must be timezone-aware, got naive {as_of!r}; a naive cutoff is "
                "interpreted in the server's timezone and silently shifts the time lock"
            )

    def search(self, vector: Sequence[float], *, as_of: datetime, k: int) -> SearchResult:
        """The k nearest chunks published strictly before ``as_of``.

        Approximate: IVFFlat with the probes pinned into the function by
        migration 004. Recall against exact search is the M3 acceptance
        criterion measured by :mod:`cascade.retrieval.bench`.
        """
        return self._call("chronofence_search", vector, as_of=as_of, k=k)

    def search_exact(self, vector: Sequence[float], *, as_of: datetime, k: int) -> SearchResult:
        """Exhaustive ground truth for recall. ``cascade_eval`` only.

        Called as ``cascade_sim`` this raises ``InsufficientPrivilege`` -- the
        grant is the mechanism, so the error is left to propagate rather than
        pre-empted with a friendlier Python check that could drift from it.
        """
        return self._call("chronofence_search_exact", vector, as_of=as_of, k=k)

    def search_hybrid(
        self,
        vector: Sequence[float],
        *,
        text: str,
        entities: Sequence[str] = (),
        as_of: datetime,
        k: int,
    ) -> SearchResult:
        """The k best chunks published strictly before ``as_of``, by fusion.

        Preserves the time lock exactly as :meth:`search` does -- ``as_of`` is
        required, keyword-only and refused when naive -- and adds nothing that
        could widen it: both candidate pools are filtered inside
        ``chronofence_search_hybrid``, and what happens here only reorders and
        truncates rows that function returned. No date is compared in Python
        except to *raise*: a row at or after ``as_of`` is a
        :class:`TimeLockViolation`, never a row to drop quietly.

        ``text`` and ``entities`` choose the keyword terms
        (:func:`~cascade.retrieval.keywords.entity_terms`); ``vector`` is the
        embedding of whatever the caller embedded, exactly as for
        :meth:`search`. They are separate because they need not be the same
        string: an actor's objective belongs in the embedding and its name
        belongs in the keyword terms.

        The result is a function of the arguments and the corpus: the terms
        are derived deterministically, the SQL breaks every tie on
        ``chunk_id``, and fusion is order-invariant over its candidates.
        """
        self._validate(vector, as_of=as_of, k=k)
        retrieval = self._settings.retrieval
        params = fusion_params(self._settings)
        terms = entity_terms(text, entities=entities, max_terms=retrieval.hybrid_max_terms)

        conn = self._require_connection()
        started = time.perf_counter()
        with conn.cursor() as cur:
            cur.execute(_HYBRID_QUERY, (vector_literal(vector), list(terms), as_of))
            rows = cur.fetchall()
        elapsed_ms = (time.perf_counter() - started) * 1000.0

        candidates = tuple(
            HybridCandidate(
                chunk_id=str(row[0]),
                document_id=str(row[1]),
                ordinal=int(row[2]),
                body=str(row[3]),
                published_at=row[4],
                source=str(row[5]),
                url=str(row[6]),
                title=str(row[7]),
                distance=float(row[8]),
                vector_rank=None if row[9] is None else int(row[9]),
                keyword_rank=None if row[10] is None else int(row[10]),
                terms_matched=None if row[11] is None else int(row[11]),
                simhash=int(row[12]),
            )
            for row in rows
        )
        late = sorted(item.chunk_id for item in candidates if item.published_at >= as_of)
        if late:
            raise TimeLockViolation(
                f"chronofence_search_hybrid returned {len(late)} chunk(s) dated at or after "
                f"as_of={as_of.isoformat()}: {late[:5]}. The time lock in migration 018 is "
                "broken; nothing retrieved through it can be trusted."
            )

        chosen = select(candidates, k=k, params=params)
        chunks = tuple(
            RetrievedChunk(
                chunk_id=entry.candidate.chunk_id,
                document_id=entry.candidate.document_id,
                ordinal=entry.candidate.ordinal,
                body=entry.candidate.body,
                published_at=entry.candidate.published_at,
                source=entry.candidate.source,
                url=entry.candidate.url,
                title=entry.candidate.title,
                distance=entry.candidate.distance,
                vector_rank=entry.candidate.vector_rank,
                keyword_rank=entry.candidate.keyword_rank,
                recency_rank=entry.recency_rank,
                fused_score=entry.score,
            )
            for entry in chosen
        )
        return SearchResult(
            as_of=as_of,
            k=k,
            chunks=chunks,
            elapsed_ms=elapsed_ms,
            mode="hybrid",
            terms=terms,
            candidates=len(candidates),
        )

    def retrieve(
        self,
        vector: Sequence[float],
        *,
        text: str,
        entities: Sequence[str] = (),
        as_of: datetime,
        k: int,
    ) -> SearchResult:
        """Evidence under the configured ``retrieval.mode``.

        The one call every evidence-fetching site makes, so the mode is decided
        in one place and the compiler, the agents and the single-model
        baselines cannot end up reading the corpus three different ways -- a
        baseline handed differently retrieved evidence would measure retrieval
        rather than architecture (§10.2). Under ``vector`` this *is*
        :meth:`search`; ``text`` and ``entities`` are not read.

        Reranking, when enabled, happens **here** rather than inside either
        search method (ADR-0047). A reranker permutes whatever pool the
        configured mode produced, so it composes with both and there is one
        place where a second ranking stage can enter the system.
        """
        rerank = self._settings.retrieval.rerank
        if not rerank.enabled:
            return self._retrieve_pool(vector, text=text, entities=entities, as_of=as_of, k=k)

        reranker = self._resolve_reranker()
        # Ask for the wider pool, then let the reranker choose the caller's k
        # out of it. `max` rather than the configured pool alone: a caller
        # asking for more than the pool must not be silently truncated by a
        # ranking stage it did not ask about.
        base = self._retrieve_pool(
            vector, text=text, entities=entities, as_of=as_of, k=max(k, rerank.pool)
        )
        scores = reranker.score(query=text, documents=[chunk.body for chunk in base.chunks])
        chosen = apply_scores(base.chunks, scores, top_k=k)
        return base.model_copy(
            update={
                "chunks": chosen,
                "k": k,
                "rerank_model": reranker.model_id,
                "reranked_pool": len(base.chunks),
            }
        )

    def _retrieve_pool(
        self,
        vector: Sequence[float],
        *,
        text: str,
        entities: Sequence[str],
        as_of: datetime,
        k: int,
    ) -> SearchResult:
        """The configured mode's own result, before any reranking."""
        if self._settings.retrieval.mode == "hybrid":
            return self.search_hybrid(vector, text=text, entities=entities, as_of=as_of, k=k)
        return self.search(vector, as_of=as_of, k=k)

    def _resolve_reranker(self) -> Reranker:
        """The configured reranker, building the pure default where it applies.

        A ``local`` reranker is arithmetic and this module may construct one;
        anything that reaches a network must be injected, so that a
        misconfiguration fails loudly here rather than opening a second egress
        path out of a module whose job is to read the corpus.
        """
        if self._reranker is not None:
            return self._reranker
        rerank = self._settings.retrieval.rerank
        if rerank.provider == "local":
            return LexicalReranker(model_id=rerank.model_id)
        raise RuntimeError(
            f"retrieval.rerank.provider is {rerank.provider!r}, which reaches a service, but no "
            "reranker was injected into Chronofence; construct it at the call site so the one "
            "call site owns every request that leaves this process"
        )

    def ef_search(self, function: str = "chronofence_search") -> int:
        """The ``hnsw.ef_search`` actually pinned into a deployed function.

        Read from ``pg_proc.proconfig`` rather than from config, so
        `cascade retrieval verify` compares the *deployed* value against the
        configured one instead of comparing config with itself.
        """
        conn = self._require_connection()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT proconfig FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                "WHERE n.nspname = 'public' AND p.proname = %s",
                (function,),
            )
            row = cur.fetchone()
        if row is None or not row[0]:
            raise RuntimeError(
                f"{function} has no pinned configuration; the migration that defines it "
                "has not been applied to this database"
            )
        for entry in row[0]:
            key, _, value = str(entry).partition("=")
            if key == "hnsw.ef_search":
                return int(value)
        raise RuntimeError(
            f"{function} does not pin hnsw.ef_search; callers would silently "
            "get pgvector's default of 40 and a recall profile nothing asserts"
        )

    def hybrid_deployed(self) -> bool:
        """Whether migration 018's function exists in this database."""
        conn = self._require_connection()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT to_regprocedure("
                "'public.chronofence_search_hybrid(halfvec,text[],timestamptz)') IS NOT NULL"
            )
            row = cur.fetchone()
        return bool(row and row[0])
