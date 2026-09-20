"""Record and replay around a reranker (ADR-0047, M15).

A study that can replay its decisions but not the ordering of the evidence
those decisions were made on cannot replay at all: the prompt carries the
evidence, so a reranker that reorders between two runs changes the prompt and
therefore the decision. These tests hold `RecordedReranker` to exactly the
contract `LLMClient.complete` keeps -- including that `replay` never reaches
the inner reranker, which is asserted with one that raises if called.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path

import pytest

from cascade.config import Settings
from cascade.llm.cache import CallCache
from cascade.llm.client import RecordedReranker
from cascade.llm.meter import CostMeter
from cascade.llm.types import CachedCall, CacheMiss, Usage
from cascade.retrieval.rerank import RerankError, rerank_cache_key


class CountingReranker:
    """A deterministic stand-in that reports how often it was consulted."""

    model_id = "counting-v1"

    def __init__(self, scores: Sequence[float] | None = None) -> None:
        self.scores = list(scores) if scores is not None else []
        self.calls = 0

    def score(self, *, query: str, documents: Sequence[str]) -> Sequence[float]:
        self.calls += 1
        if self.scores:
            return list(self.scores)
        return [float(len(body)) for body in documents]


class ExplodingReranker:
    """Proves a code path never reaches the inner reranker."""

    model_id = "counting-v1"

    def score(self, *, query: str, documents: Sequence[str]) -> Sequence[float]:
        raise AssertionError("the inner reranker must not be reached")


@pytest.fixture
def cache(tmp_path: Path) -> CallCache:
    return CallCache(tmp_path / "cache")


DOCS = ["alpha", "beta beta", "gamma gamma gamma"]


class TestRecordMode:
    def test_a_miss_scores_and_stores(self, cache: CallCache) -> None:
        inner = CountingReranker()
        wrapper = RecordedReranker(inner=inner, cache=cache, mode="record")
        scores = wrapper.score(query="q", documents=DOCS)
        assert list(scores) == [5.0, 9.0, 17.0]
        assert inner.calls == 1
        assert rerank_cache_key(model_id="counting-v1", query="q", documents=DOCS) in cache

    def test_a_second_identical_call_is_served_from_disk(self, cache: CallCache) -> None:
        first = RecordedReranker(inner=CountingReranker(), cache=cache, mode="record")
        first.score(query="q", documents=DOCS)

        inner = CountingReranker()
        second = RecordedReranker(inner=inner, cache=cache, mode="record")
        assert list(second.score(query="q", documents=DOCS)) == [5.0, 9.0, 17.0]
        assert inner.calls == 0, "a recorded pool must not be scored again"
        assert second.hits == 1

    def test_a_different_pool_is_a_different_call(self, cache: CallCache) -> None:
        inner = CountingReranker()
        wrapper = RecordedReranker(inner=inner, cache=cache, mode="record")
        wrapper.score(query="q", documents=DOCS)
        wrapper.score(query="q", documents=list(reversed(DOCS)))
        assert inner.calls == 2, "pool order is part of the key"


class TestReplayMode:
    def test_a_recording_is_returned_without_consulting_the_reranker(
        self, cache: CallCache
    ) -> None:
        RecordedReranker(inner=CountingReranker(), cache=cache, mode="record").score(
            query="q", documents=DOCS
        )
        replay = RecordedReranker(inner=ExplodingReranker(), cache=cache, mode="replay")
        assert list(replay.score(query="q", documents=DOCS)) == [5.0, 9.0, 17.0]

    def test_a_miss_raises_rather_than_scoring(self, cache: CallCache) -> None:
        replay = RecordedReranker(inner=ExplodingReranker(), cache=cache, mode="replay")
        with pytest.raises(CacheMiss, match="no recorded rerank"):
            replay.score(query="never recorded", documents=DOCS)

    def test_the_miss_message_names_the_variable_that_fixes_it(self, cache: CallCache) -> None:
        replay = RecordedReranker(inner=ExplodingReranker(), cache=cache, mode="replay")
        with pytest.raises(CacheMiss, match="CASCADE_LLM__MODE=record"):
            replay.score(query="never recorded", documents=DOCS)


class TestLiveMode:
    def test_live_scores_every_time_and_records_nothing(self, cache: CallCache) -> None:
        inner = CountingReranker()
        wrapper = RecordedReranker(inner=inner, cache=cache, mode="live")
        wrapper.score(query="q", documents=DOCS)
        wrapper.score(query="q", documents=DOCS)
        assert inner.calls == 2
        assert cache.count() == 0


class TestACorruptedRecordingIsRefused:
    def _store(self, cache: CallCache, *, scores: object) -> None:
        cache.put(
            CachedCall(
                key=rerank_cache_key(model_id="counting-v1", query="q", documents=DOCS),
                request_digest={"kind": "rerank"},
                raw_response={"scores": scores},
                usage=Usage(input_tokens=0, output_tokens=0),
                latency_ms=1.0,
                recorded_at="2026-01-01T00:00:00+00:00",
            )
        )

    def test_a_recording_of_the_wrong_length_is_an_error(self, cache: CallCache) -> None:
        # Caught here, where the key is still in hand, rather than downstream
        # in `apply_scores` where the cause is no longer visible.
        self._store(cache, scores=[1.0, 2.0])
        replay = RecordedReranker(inner=ExplodingReranker(), cache=cache, mode="replay")
        with pytest.raises(RerankError, match="does not describe this call"):
            replay.score(query="q", documents=DOCS)

    def test_a_recording_with_no_scores_is_an_error(self, cache: CallCache) -> None:
        self._store(cache, scores=None)
        replay = RecordedReranker(inner=ExplodingReranker(), cache=cache, mode="replay")
        with pytest.raises(RerankError, match="holds no score"):
            replay.score(query="q", documents=DOCS)


class TestTheMeterSeesTheRerankCacheButNotAsModelCalls:
    """Per-query accounting, which the wrapper is the only place that can do.

    A hit is booked here because this is the only place that knows one
    happened; a miss is booked by the inner reranker, because that is where a
    provider is reached and a local one costs nothing to reach. The two paths
    are disjoint, so one meter serves both without double-counting.
    """

    def _meter(self, settings: Settings) -> CostMeter:
        return CostMeter(settings, "bench", ceiling_usd=Decimal("1000"))

    def test_a_miss_on_a_local_reranker_books_nothing_at_all(
        self, cache: CallCache, settings: Settings
    ) -> None:
        meter = self._meter(settings)
        wrapper = RecordedReranker(
            inner=CountingReranker(), cache=cache, mode="record", meter=meter
        )
        wrapper.score(query="q", documents=DOCS)

        assert meter.total_usd == Decimal(0)
        assert meter.units == {}
        assert meter.cached_units == {}

    def test_a_hit_is_counted_at_zero(self, cache: CallCache, settings: Settings) -> None:
        # A study replayed end to end would otherwise report no rerank activity
        # at all, and the count is what says whether the recorded corpus covers
        # the pools this run asked for.
        RecordedReranker(inner=CountingReranker(), cache=cache, mode="record").score(
            query="q", documents=DOCS
        )
        meter = self._meter(settings)
        replay = RecordedReranker(
            inner=ExplodingReranker(), cache=cache, mode="replay", meter=meter
        )
        replay.score(query="q", documents=DOCS)

        assert meter.cached_units == {"rerank": 1}
        assert meter.total_usd == Decimal(0)

    def test_a_rerank_hit_is_not_a_model_cache_hit(
        self, cache: CallCache, settings: Settings
    ) -> None:
        # `hit_rate` is the M6 acceptance criterion's ratio, measured over
        # decisions. A second kind of call entering its denominator would move
        # the criterion without changing anything it measures.
        RecordedReranker(inner=CountingReranker(), cache=cache, mode="record").score(
            query="q", documents=DOCS
        )
        meter = self._meter(settings)
        RecordedReranker(inner=ExplodingReranker(), cache=cache, mode="replay", meter=meter).score(
            query="q", documents=DOCS
        )

        assert meter.cached_calls == 0
        assert meter.hit_rate == 0.0

    def test_a_wrapper_without_a_meter_still_scores(self, cache: CallCache) -> None:
        # The leakage probes and the property tests wrap rerankers without
        # measuring spend; needing a meter to do so would make them carry one.
        wrapper = RecordedReranker(inner=CountingReranker(), cache=cache, mode="record")
        assert list(wrapper.score(query="q", documents=DOCS)) == [5.0, 9.0, 17.0]


class TestTheWrapperIsNotAModel:
    def test_it_reports_the_inner_reranker_s_identity(self, cache: CallCache) -> None:
        # The key must name the scorer, not the wrapper: wrapping must not
        # fork the recorded corpus.
        wrapper = RecordedReranker(inner=CountingReranker(), cache=cache, mode="record")
        assert wrapper.model_id == "counting-v1"

    def test_switching_provider_invalidates_recordings(self, cache: CallCache) -> None:
        RecordedReranker(inner=CountingReranker(), cache=cache, mode="record").score(
            query="q", documents=DOCS
        )

        class OtherReranker(CountingReranker):
            model_id = "other-v1"

        inner = OtherReranker()
        wrapper = RecordedReranker(inner=inner, cache=cache, mode="record")
        wrapper.score(query="q", documents=DOCS)
        assert inner.calls == 1, "a different reranker must not read the old recording"
