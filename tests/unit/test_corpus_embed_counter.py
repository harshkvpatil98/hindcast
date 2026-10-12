"""Token counting must not share a tokenizer with the embedding call.

The ingest counts tokens on worker threads while the main thread embeds. The
transformers wrapper reconfigures the Rust tokenizer it wraps on *every* call
-- truncation and padding on for an embedding batch, off for a count -- so a
count taken through the model's own tokenizer during an `encode` is a data
race. Measured against the pinned tokenizer it raised `RuntimeError: Already
borrowed` on every call; the quiet version of the same race is a count capped
at the truncation length, which the chunker would believe.

`Embedder` therefore counts on a private copy that nothing reconfigures. None
of this needs the model: the first tests use a stand-in that models exactly
the state at issue, and the last uses the real `tokenizers` library with a
vocabulary built in memory.
"""

from __future__ import annotations

import sys
import threading
import time
import types
from typing import Any

import pytest

from cascade.corpus.embed import EMBEDDING_DIM, Embedder, EmbeddingUnavailable, _private_counter


class Encoding:
    def __init__(self, ids: list[int]) -> None:
        self.ids = ids


class Backend:
    """The part of `tokenizers.Tokenizer` that matters here: truncation and
    padding are *state on the object*, and `encode` obeys whatever is set."""

    def __init__(self) -> None:
        self.truncation: dict[str, int] | None = None
        self.padding: dict[str, int] | None = None
        self.encode_special_tokens = False

    def enable_truncation(self, max_length: int) -> None:
        self.truncation = {"max_length": max_length}

    def no_truncation(self) -> None:
        self.truncation = None

    def enable_padding(self, length: int) -> None:
        self.padding = {"length": length}

    def no_padding(self) -> None:
        self.padding = None

    def encode(self, text: str, add_special_tokens: bool = True) -> Encoding:
        ids = [1, *range(10, 10 + len(text.split())), 2] if add_special_tokens else []
        if self.truncation is not None:
            ids = ids[: self.truncation["max_length"]]
        if self.padding is not None:
            ids = ids + [0] * (self.padding["length"] - len(ids))
        return Encoding(ids)

    def encode_batch(self, texts: list[str], add_special_tokens: bool = True) -> list[Encoding]:
        return [self.encode(text, add_special_tokens) for text in texts]


class Wrapper:
    def __init__(self, backend: Any) -> None:
        self.backend_tokenizer = backend


def configured_for_embedding() -> Backend:
    """How `SentenceTransformer.encode` leaves the shared tokenizer."""
    backend = Backend()
    backend.enable_truncation(8)
    backend.enable_padding(16)
    backend.encode_special_tokens = True
    return backend


def loaded(counter: Any) -> Embedder:
    embedder = Embedder(model_name="m", device="cpu")
    embedder._counter = counter
    embedder._model = object()  # what `load` tests; nothing here embeds
    return embedder


LONG = " ".join(f"w{i}" for i in range(40))  # 42 tokens with [CLS] and [SEP]


def test_the_counter_is_a_copy_with_truncation_and_padding_off() -> None:
    shared = configured_for_embedding()
    counter = _private_counter(Wrapper(shared))

    assert counter is not shared
    assert counter.truncation is None and counter.padding is None
    assert counter.encode_special_tokens is True
    # Making the copy must not reconfigure the original: the embedding call
    # depends on its truncation, and an over-long input is an indexing error
    # inside the model rather than a wrong count.
    assert shared.truncation == {"max_length": 8} and shared.padding == {"length": 16}


def test_counts_ignore_whatever_embedding_does_to_the_shared_tokenizer() -> None:
    shared = Backend()
    embedder = loaded(_private_counter(Wrapper(shared)))
    assert embedder.count_tokens(LONG) == 42

    shared.enable_truncation(8)
    shared.enable_padding(16)
    assert len(shared.encode(LONG).ids) == 16  # what a count through it would now say
    assert embedder.count_tokens(LONG) == 42
    assert embedder.count_tokens_batch([LONG, "two words", ""]) == [42, 4, 2]
    assert embedder.count_tokens_batch([]) == []


def test_a_tokenizer_with_no_backend_to_copy_is_refused() -> None:
    with pytest.raises(EmbeddingUnavailable, match="no Rust backend to copy"):
        _private_counter(object())


def test_a_racing_first_load_constructs_the_model_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """The counters call `load()` on every use, from every chunking thread."""
    constructed: list[str] = []

    class Model:
        tokenizer = Wrapper(Backend())

        def get_sentence_embedding_dimension(self) -> int:
            return EMBEDDING_DIM

    def factory(name: str, *, device: str, local_files_only: bool = False) -> Model:
        constructed.append(name)
        time.sleep(0.05)  # long enough for every other thread to reach `load`
        return Model()

    stub = types.ModuleType("sentence_transformers")
    stub.SentenceTransformer = factory  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sentence_transformers", stub)

    embedder = Embedder(model_name="m", device="cpu")
    together = threading.Barrier(8)
    counts: list[int] = []

    def count() -> None:
        together.wait()
        counts.append(embedder.count_tokens("three word text"))

    threads = [threading.Thread(target=count) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert constructed == ["m"]
    assert counts == [5] * 8


def test_the_real_tokenizer_counts_correctly_while_the_shared_one_is_reconfigured() -> None:
    tokenizers = pytest.importorskip("tokenizers")
    from tokenizers.models import WordPiece
    from tokenizers.pre_tokenizers import Whitespace
    from tokenizers.processors import TemplateProcessing

    vocab = {"[UNK]": 0, "[CLS]": 1, "[SEP]": 2, "[PAD]": 3, "tariff": 4, "harbour": 5, "##s": 6}
    shared = tokenizers.Tokenizer(WordPiece(vocab, unk_token="[UNK]"))
    shared.pre_tokenizer = Whitespace()
    shared.post_processor = TemplateProcessing(
        single="[CLS] $A [SEP]", special_tokens=[("[CLS]", 1), ("[SEP]", 2)]
    )
    shared.enable_truncation(max_length=8)
    shared.enable_padding(pad_id=3, pad_token="[PAD]")

    embedder = loaded(_private_counter(Wrapper(shared)))
    assert shared.truncation is not None and shared.padding is not None
    text = "tariffs harbour " * 30  # 30 x (tariff ##s harbour) + [CLS] [SEP]
    assert embedder.count_tokens(text) == 92
    assert len(shared.encode(text).ids) == 8

    stop = threading.Event()
    failures: list[str] = []

    def embed_like() -> None:
        # What every `SentenceTransformer.encode` batch does to the shared
        # tokenizer -- reconfigure, then encode -- as fast as it can.
        while not stop.is_set():
            shared.no_truncation()
            shared.enable_truncation(max_length=8)
            shared.encode_batch([text, "tariff"])

    def count() -> None:
        try:
            for _ in range(200):
                if embedder.count_tokens(text) != 92:
                    failures.append("a single count moved")
                if embedder.count_tokens_batch([text, "tariff", ""]) != [92, 3, 2]:
                    failures.append("a batched count moved")
        except Exception as exc:  # noqa: BLE001 -- the failure this test exists to see
            failures.append(f"{type(exc).__name__}: {exc}")

    reconfigurer = threading.Thread(target=embed_like)
    counters = [threading.Thread(target=count) for _ in range(4)]
    reconfigurer.start()
    for thread in counters:
        thread.start()
    for thread in counters:
        thread.join()
    stop.set()
    reconfigurer.join()
    assert failures == []
