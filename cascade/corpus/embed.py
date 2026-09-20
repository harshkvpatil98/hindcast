"""Local embedding with ``BAAI/bge-small-en-v1.5`` (spec §2.3, §3.2).

384 dimensions, normalised, stored as ``halfvec(384)``. The model is pinned:
the vectors in the corpus and the vectors a query is embedded with at M3 must
come from the same model, or every cosine distance in the study is meaningless.
``cascade doctor`` asserts the pin.

Normalisation is load-bearing rather than cosmetic. pgvector's ``<->`` is L2
distance; on unit-norm vectors L2 and cosine rank identically, so normalising
here is what lets the M3 index use the operator it is built for while the
study reasons in cosine terms.

This module also owns the **tokenizer**, because the chunker's 512-token cap
only means what the spec intends if it is counted with the same tokenizer the
model uses. Counting whitespace words instead would let a chunk overflow the
context window and be silently truncated at embed time -- the tail of the
chunk would be in the database as text but absent from its own vector.

Counting runs on a **private copy** of that tokenizer, never on the model's own
object. The ingest counts tokens from worker threads while the main thread is
embedding, and the transformers wrapper reconfigures the shared Rust tokenizer
on every call -- truncation and padding *on* for an embedding batch, *off* for
a count. Measured against the pinned tokenizer: counting through the shared
object while an embedding-style call ran on another thread raised
``RuntimeError: Already borrowed`` on every call, and the alternative to that
error is worse, a count silently capped at the truncation length.
"""

from __future__ import annotations

import copy
import threading
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

__all__ = ["EMBEDDING_DIM", "Embedder", "EmbeddingUnavailable"]

EMBEDDING_DIM = 384


class EmbeddingUnavailable(RuntimeError):
    """The embedding stack is pinned but not installed."""


def _private_counter(tokenizer: Any) -> Any:
    """Copy the tokenizer's Rust backend and switch truncation and padding off.

    Preserves the chunker's contract that a count is a function of the text
    alone. The copy is configured once, here, and nothing reconfigures it
    afterwards, so its ``encode`` calls only ever read it: safe from any
    number of threads, and blind to whatever ``SentenceTransformer.encode`` is
    doing to the original at that moment. The counts are the wrapper's own --
    with truncation and padding off, the wrapper's ``input_ids`` *are* this
    backend's ``ids``.

    Raises rather than counting through the shared object: that path is
    correct single-threaded and a data race otherwise, and nothing at the call
    site could tell which one it had been given.
    """
    backend = getattr(tokenizer, "backend_tokenizer", None)
    if backend is None:
        raise EmbeddingUnavailable(
            f"{type(tokenizer).__name__} has no Rust backend to copy. Token counting "
            "needs a private tokenizer that embedding cannot reconfigure mid-count, "
            "and only a fast tokenizer can provide one."
        )
    counter = copy.deepcopy(backend)
    counter.no_truncation()
    counter.no_padding()
    # The wrapper re-asserts this on the original before every call; a copy
    # that went through serialisation must not be left to its default.
    counter.encode_special_tokens = backend.encode_special_tokens
    return counter


@dataclass
class Embedder:
    """Loads the pinned model once and encodes batches with it."""

    model_name: str
    batch_size: int = 512
    device: str | None = None
    use_fp16: bool = True
    _model: Any = field(default=None, init=False, repr=False)
    _counter: Any = field(default=None, init=False, repr=False)
    _load_lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False, compare=False
    )

    def _resolve_device(self) -> str:
        if self.device is not None:
            return self.device
        try:
            import torch
        except ImportError:  # pragma: no cover - guarded by load()
            return "cpu"
        if torch.backends.mps.is_available():
            return "mps"
        if torch.cuda.is_available():
            return "cuda"
        return "cpu"

    def load(self) -> None:
        """Load the model. Idempotent, and the only place it is constructed.

        Raises rather than falling back to a different model: a silent
        substitution would produce a corpus whose vectors cannot be compared
        with anything a later phase embeds.
        """
        if self._model is not None:
            return
        # Taken only on the cold path, so the counters' per-call `load()` stays
        # one attribute test. Two threads racing a first load would otherwise
        # each construct the model -- twice the memory on a machine where the
        # model shares it with the GPU.
        with self._load_lock:
            if self._model is None:
                self._load()

    def _load(self) -> None:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise EmbeddingUnavailable(
                "sentence-transformers is pinned but not installed. "
                "Run `uv sync --extra embed` -- the corpus cannot be built without it."
            ) from exc

        device = self._resolve_device()
        model = self._construct(SentenceTransformer, device=device)
        if self.use_fp16 and device != "cpu":
            # fp16 halves memory and roughly doubles throughput on MPS/CUDA.
            # Left off on CPU, where half precision is emulated and slower.
            model = model.half()
        self._counter = _private_counter(model.tokenizer)
        self.device = device
        # Last, because it is what `load` tests: a thread that sees the model
        # must also see the counter it is about to use.
        self._model = model

        # sentence-transformers 5.x renamed this; support both so the pin can
        # move within the major line without an import-time failure.
        dimension_of = (
            getattr(model, "get_embedding_dimension", None)
            or model.get_sentence_embedding_dimension
        )
        dim = dimension_of()
        if dim != EMBEDDING_DIM:
            raise EmbeddingUnavailable(
                f"{self.model_name} produces {dim}-d vectors but the schema stores "
                f"halfvec({EMBEDDING_DIM}); the pin and the DDL disagree"
            )

    def _construct(self, factory: Any, *, device: str) -> Any:
        """Build the model, falling back to the local cache on a network blip.

        The weights are cached on disk after the first load, but the loader
        still calls out to the hub to check for a newer revision -- so a
        transient HTTP failure takes down every test that needs an embedder,
        including the leakage suite and the date-monotonicity properties.
        Measured here: a mid-stream `RemoteProtocolError` against
        huggingface.co failed 1 test and errored 12 more on a machine that
        already had the model.

        The retry passes ``local_files_only=True`` rather than setting an
        environment variable, because only ``cascade/config.py`` may touch the
        process environment and a test enforces it.

        A model that is genuinely absent still fails: the fallback can only
        succeed from a populated cache, and the original error is re-raised
        with the offline attempt's own failure attached when it is not.
        """
        try:
            return factory(self.model_name, device=device)
        except Exception as first:  # noqa: BLE001 -- re-raised unless the cache saves us
            try:
                return factory(self.model_name, device=device, local_files_only=True)
            except Exception as offline:  # noqa: BLE001 -- folded into the raise below
                raise EmbeddingUnavailable(
                    f"could not load {self.model_name!r}: {type(first).__name__}: {first}. "
                    f"The local cache did not satisfy it either "
                    f"({type(offline).__name__}: {offline}). The model is pinned, so "
                    "there is no substitution to fall back to."
                ) from first

    def count_tokens(self, text: str) -> int:
        """Count tokens the way the model does.

        This is the counter the chunker must use. ``add_special_tokens`` is
        left on because the model prepends [CLS] and appends [SEP] at encode
        time, and those occupy the same 512-token window the chunk has to fit
        inside.

        Safe to call from several threads, and while ``encode`` is running:
        it reads the private counter, which nothing mutates after ``load``.
        """
        self.load()
        return len(self._counter.encode(text, add_special_tokens=True).ids)

    def count_tokens_batch(self, texts: Sequence[str]) -> list[int]:
        """Count tokens for many strings in one call.

        The chunker measures every sentence of every document. Called one at a
        time, that crosses the Python/Rust boundary once per sentence and
        dominates ingest CPU; the fast tokenizer batches the same work into a
        single call. Same counts, materially less overhead.

        Thread-safe on the same terms as ``count_tokens``, which is what lets
        the ingest chunk one batch while it embeds another.
        """
        self.load()
        if not texts:
            return []
        encoded = self._counter.encode_batch(list(texts), add_special_tokens=True)
        return [len(encoding.ids) for encoding in encoded]

    def encode(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed ``texts`` into unit-norm 384-d vectors, in input order."""
        self.load()
        if not texts:
            return []
        vectors = self._model.encode(
            list(texts),
            batch_size=self.batch_size,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        return [[float(value) for value in row] for row in vectors]
