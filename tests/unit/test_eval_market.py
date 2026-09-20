"""The market at the cutoff (M14): the time lock, the exclusions, the grant.

Three things would each quietly invalidate this benchmark, and each has a test
that fails when it is broken on purpose:

* using an observation *at or after* the cutoff -- for a resolved market the
  later prices converge on the outcome, so this is the leak;
* imputing a price the market never quoted, which scores silence as
  calibration;
* letting ``cascade_sim`` read the table, which turns a benchmark into an
  input nobody decided to give the agents.

The fixtures under ``tests/fixtures/market/`` are real responses from the three
endpoints, trimmed. They deliberately carry **no resolution field**: a fixture
with ``outcomePrices`` in it would put labels in the repository. Where a test
needs such a field to prove it is ignored, it injects a synthetic one.
"""

from __future__ import annotations

import ast
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from cascade.config import MarketBaselineConfig, Settings
from cascade.eval.baselines import BASELINES
from cascade.eval.market import (
    MARKET_CONFIG_ID,
    MarketPayloadError,
    MarketPrice,
    PriceObservation,
    coverage_summary,
    is_stale,
    last_before,
    manifold_bets_url,
    market_created_at,
    parse_manifold_bets,
    parse_polymarket_history,
    parse_source_ref,
    polymarket_history_url,
    polymarket_market_url,
    polymarket_yes_token,
    scored_market_forecasts,
    unobtainable,
)
from cascade.eval.market_fetch import MarketFetcher, fetch_market_prices
from cascade.ledger.schema import Scenario

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "market"
MIGRATIONS = REPO_ROOT / "migrations"

# The real scenario the Polymarket fixtures were recorded for.
PM_REF = "polymarket:2026-fifa-world-cup-which-countries-qualify:550700"
PM_CUTOFF = datetime(2025, 8, 12, 7, 3, 9, 216275, tzinfo=UTC)
# A real market whose Gamma `createdAt` falls after the cutoff the registry
# derived from its `startDate`.
LATE_REF = "polymarket:ethereum-spot-etf-approved-by:253452"
LATE_CUTOFF = datetime(2023, 8, 18, 12, 8, 40, 397000, tzinfo=UTC)
MF_REF = "manifold:6LpNssU8Cd"
MF_CUTOFF = datetime(2025, 11, 3, 5, 5, 16, 534500, tzinfo=UTC)

NOW = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)
DAY = timedelta(hours=24)


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def scenario(scenario_id: str, *, source: str, source_ref: str, cutoff: datetime) -> Scenario:
    return Scenario(
        scenario_id=scenario_id,
        question="Will it happen?",
        resolution_criterion="Resolves YES if it happens.",
        cutoff_ts=cutoff,
        resolve_ts=cutoff + timedelta(days=60),
        domain="other",
        source=source,  # type: ignore[arg-type]
        source_ref=source_ref,
        party_rule="event_siblings",
        party_names=("A", "B", "C"),
        event_group=None,
    )


def obs(at: datetime, p: float) -> PriceObservation:
    return PriceObservation(observed_at=at, probability=p)


def priced(
    scenario_id: str, *, age: timedelta, p: float = 0.6, source: str = "polymarket"
) -> MarketPrice:
    return MarketPrice(
        scenario_id=scenario_id,
        source=source,
        source_ref=f"{source}:ref",
        cutoff_ts=PM_CUTOFF,
        probability=p,
        observed_at=PM_CUTOFF - age,
        fetched_at=NOW,
    )


def missing(scenario_id: str, reason: str, *, source: str = "polymarket") -> MarketPrice:
    return unobtainable(
        scenario_id=scenario_id,
        source=source,
        source_ref=f"{source}:ref",
        cutoff=PM_CUTOFF,
        reason=reason,  # type: ignore[arg-type]
        fetched_at=NOW,
    )


# ---------------------------------------------------------------------------
# The time lock
# ---------------------------------------------------------------------------


class TestLastBefore:
    def test_it_takes_the_last_observation_strictly_before_the_cutoff(self) -> None:
        cutoff = PM_CUTOFF
        chosen = last_before(
            [
                obs(cutoff - timedelta(hours=3), 0.40),
                obs(cutoff - timedelta(minutes=1), 0.55),
                obs(cutoff + timedelta(minutes=1), 0.99),
            ],
            cutoff=cutoff,
        )
        assert chosen is not None
        assert chosen.probability == 0.55

    def test_an_observation_exactly_at_the_cutoff_is_not_used(self) -> None:
        """`published_at >= as_of` is a violation everywhere in this project;
        a price stamped at the cutoff is not *before* it."""
        cutoff = PM_CUTOFF
        chosen = last_before(
            [obs(cutoff - timedelta(minutes=5), 0.30), obs(cutoff, 0.97)], cutoff=cutoff
        )
        assert chosen is not None
        assert chosen.probability == 0.30

    def test_only_an_observation_at_the_cutoff_means_no_price(self) -> None:
        assert last_before([obs(PM_CUTOFF, 0.97)], cutoff=PM_CUTOFF) is None

    def test_one_microsecond_before_the_cutoff_is_admissible(self) -> None:
        """The lock is strict, not padded: refusing this would be a different
        rule from the one the rest of the project applies."""
        chosen = last_before([obs(PM_CUTOFF - timedelta(microseconds=1), 0.2)], cutoff=PM_CUTOFF)
        assert chosen is not None

    def test_nothing_before_the_cutoff_is_none_never_a_number(self) -> None:
        assert last_before([], cutoff=PM_CUTOFF) is None
        assert last_before([obs(PM_CUTOFF + timedelta(days=1), 1.0)], cutoff=PM_CUTOFF) is None

    def test_input_order_does_not_matter_for_distinct_timestamps(self) -> None:
        points = [obs(PM_CUTOFF - timedelta(minutes=m), m / 100) for m in (50, 2, 30, 7)]
        assert last_before(points, cutoff=PM_CUTOFF) == last_before(
            list(reversed(points)), cutoff=PM_CUTOFF
        )
        chosen = last_before(points, cutoff=PM_CUTOFF)
        assert chosen is not None and chosen.probability == 0.02

    def test_a_shared_timestamp_resolves_to_the_later_entry(self) -> None:
        at = PM_CUTOFF - timedelta(seconds=1)
        chosen = last_before([obs(at, 0.1), obs(at, 0.9)], cutoff=PM_CUTOFF)
        assert chosen is not None and chosen.probability == 0.9

    def test_the_cutoff_has_no_default(self) -> None:
        """Invariant 1: a forgotten cutoff is a TypeError, not the newest price
        the source holds -- which for a resolved market is the outcome."""
        with pytest.raises(TypeError):
            last_before([obs(PM_CUTOFF, 0.5)])  # type: ignore[call-arg]

    def test_a_naive_cutoff_is_refused(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            last_before([], cutoff=datetime(2025, 1, 1))

    @given(
        offsets=st.lists(st.integers(min_value=-10_000, max_value=10_000), max_size=40),
    )
    def test_the_lock_holds_for_any_history(self, offsets: list[int]) -> None:
        points = [obs(PM_CUTOFF + timedelta(seconds=s), 0.5) for s in offsets]
        chosen = last_before(points, cutoff=PM_CUTOFF)
        admissible = [p for p in points if p.observed_at < PM_CUTOFF]
        if not admissible:
            assert chosen is None
            return
        assert chosen is not None
        assert chosen.observed_at < PM_CUTOFF
        assert chosen.observed_at == max(p.observed_at for p in admissible)


class TestMarketPriceIsLockedByConstruction:
    def test_an_observation_at_the_cutoff_cannot_be_constructed(self) -> None:
        with pytest.raises(ValidationError, match="time lock"):
            MarketPrice(
                scenario_id="s",
                source="polymarket",
                source_ref="r",
                cutoff_ts=PM_CUTOFF,
                probability=0.5,
                observed_at=PM_CUTOFF,
                fetched_at=NOW,
            )

    def test_an_observation_after_the_cutoff_cannot_be_constructed(self) -> None:
        with pytest.raises(ValidationError, match="time lock"):
            priced("s", age=timedelta(seconds=-1))

    def test_a_price_is_a_probability_or_a_reason_never_both(self) -> None:
        with pytest.raises(ValidationError, match="never both"):
            MarketPrice(
                scenario_id="s",
                source="polymarket",
                source_ref="r",
                cutoff_ts=PM_CUTOFF,
                probability=0.5,
                observed_at=PM_CUTOFF - DAY,
                unobtainable_reason="no_price_history",
                fetched_at=NOW,
            )

    def test_a_price_with_neither_is_refused(self) -> None:
        """There is no default probability for "unknown" to fall back on."""
        with pytest.raises(ValidationError, match="never neither"):
            MarketPrice(
                scenario_id="s", source="polymarket", source_ref="r",
                cutoff_ts=PM_CUTOFF, fetched_at=NOW,
            )  # fmt: skip

    def test_an_unobtainable_price_carries_no_number(self) -> None:
        price = missing("s", "no_price_history")
        assert price.probability is None
        assert price.staleness is None
        assert not price.priced

    def test_a_naive_timestamp_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="timezone-aware"):
            PriceObservation(observed_at=datetime(2025, 1, 1), probability=0.5)


class TestStaleness:
    def test_staleness_is_cutoff_minus_observation(self) -> None:
        assert priced("s", age=timedelta(minutes=7)).staleness == timedelta(minutes=7)

    def test_exactly_at_the_bound_is_still_usable(self) -> None:
        assert not is_stale(priced("s", age=DAY), max_staleness=DAY)

    def test_one_tick_past_the_bound_is_stale(self) -> None:
        assert is_stale(priced("s", age=DAY + timedelta(microseconds=1)), max_staleness=DAY)

    def test_an_unpriced_scenario_is_never_called_stale(self) -> None:
        """It has its own reason; folding the two hides how many markets had
        nothing to say."""
        assert not is_stale(missing("s", "no_price_history"), max_staleness=DAY)


# ---------------------------------------------------------------------------
# Parsers, against real payloads
# ---------------------------------------------------------------------------


class TestPolymarketPayloads:
    def test_the_real_market_document_yields_its_yes_token(self) -> None:
        document = fixture("gamma_market_550700.json")
        token = polymarket_yes_token(document)
        assert token is not None
        assert token == json.loads(document["clobTokenIds"])[0]
        assert token.isdigit() and len(token) > 60

    def test_the_fixture_carries_no_resolution_field(self) -> None:
        """A fixture with `outcomePrices` in it would commit labels."""
        for name in sorted(path.name for path in FIXTURES.glob("gamma_market_*.json")):
            document = fixture(name)
            assert "outcomePrices" not in document, name
            assert "umaResolutionStatus" not in document, name

    @pytest.mark.parametrize("resolution", ['["1", "0"]', '["0", "1"]'])
    def test_the_token_does_not_depend_on_how_the_market_resolved(self, resolution: str) -> None:
        document = dict(fixture("gamma_market_550700.json"))
        clean = polymarket_yes_token(document)
        document["outcomePrices"] = resolution  # synthetic, injected for this test
        assert polymarket_yes_token(document) == clean

    def test_a_reversed_outcome_pair_is_refused_not_guessed(self) -> None:
        document = dict(fixture("gamma_market_550700.json"))
        document["outcomes"] = '["No", "Yes"]'
        assert polymarket_yes_token(document) is None

    def test_a_market_without_tokens_has_no_yes_token(self) -> None:
        document = dict(fixture("gamma_market_550700.json"))
        document.pop("clobTokenIds")
        assert polymarket_yes_token(document) is None

    def test_created_at_is_read_as_aware_utc(self) -> None:
        created = market_created_at(fixture("gamma_market_253452.json"))
        assert created is not None and created.tzinfo is not None
        assert created > LATE_CUTOFF, "the fixture is the market that postdates its cutoff"

    def test_a_missing_created_at_is_none(self) -> None:
        assert market_created_at({}) is None
        assert market_created_at({"createdAt": "yesterday"}) is None
        assert market_created_at({"createdAt": "2025-01-01T00:00:00"}) is None  # naive

    def test_the_real_history_parses_in_chronological_order(self) -> None:
        points = parse_polymarket_history(fixture("clob_history_550700_window.json"))
        assert len(points) >= 5
        assert [p.observed_at for p in points] == sorted(p.observed_at for p in points)
        assert all(0.0 <= p.probability <= 1.0 for p in points)
        assert all(p.observed_at < PM_CUTOFF for p in points), "the request ended at the cutoff"

    def test_an_empty_history_is_no_observations(self) -> None:
        assert parse_polymarket_history(fixture("clob_history_empty.json")) == ()

    def test_a_body_without_history_is_an_error_not_an_empty_market(self) -> None:
        """A changed API must not become a coverage statistic."""
        with pytest.raises(MarketPayloadError):
            parse_polymarket_history({"error": "invalid filters"})
        with pytest.raises(MarketPayloadError):
            parse_polymarket_history([])

    def test_unreadable_points_are_skipped_never_repaired(self) -> None:
        points = parse_polymarket_history(
            {
                "history": [
                    {"t": 1754982000, "p": 0.6},
                    {"t": 1754982060, "p": 1.4},  # not a probability
                    {"t": "soon", "p": 0.5},
                    {"t": 1754982120},
                    {"t": 1754982180, "p": True},
                    "junk",
                ]
            }
        )
        assert [p.probability for p in points] == [0.6]


class TestManifoldPayloads:
    def test_the_real_bets_parse_in_chronological_order(self) -> None:
        points = parse_manifold_bets(fixture("manifold_bets_6LpNssU8Cd.json"))
        assert len(points) >= 3
        assert [p.observed_at for p in points] == sorted(p.observed_at for p in points)

    def test_redemptions_are_not_observations(self) -> None:
        payload = fixture("manifold_bets_6LpNssU8Cd.json")
        redemptions = [bet for bet in payload if bet.get("isRedemption")]
        assert redemptions, "the fixture must contain a real redemption"
        assert len(parse_manifold_bets(payload)) == len(payload) - len(redemptions)

    def test_fields_that_change_after_the_cutoff_are_not_read(self) -> None:
        """A limit order keeps filling after the cutoff; `createdTime` and
        `probAfter` were written once."""
        payload = fixture("manifold_bets_6LpNssU8Cd.json")
        mutated = [
            {**bet, "isFilled": not bet.get("isFilled"), "isCancelled": True, "amount": 0,
             "fills": [], "updatedTime": 1}
            for bet in payload
        ]  # fmt: skip
        assert parse_manifold_bets(mutated) == parse_manifold_bets(payload)

    def test_the_price_is_prob_after_of_the_last_bet_before_the_cutoff(self) -> None:
        payload = fixture("manifold_bets_6LpNssU8Cd.json")
        chosen = last_before(parse_manifold_bets(payload), cutoff=MF_CUTOFF)
        newest_real = next(bet for bet in payload if not bet.get("isRedemption"))
        assert chosen is not None
        assert chosen.probability == newest_real["probAfter"]
        assert chosen.observed_at == datetime.fromtimestamp(
            newest_real["createdTime"] / 1000, tz=UTC
        )

    def test_a_non_list_body_is_an_error(self) -> None:
        with pytest.raises(MarketPayloadError):
            parse_manifold_bets({"message": "Contract not found"})


class TestReferencesAndUrls:
    def test_a_polymarket_ref_resolves_to_its_market_id(self) -> None:
        ref = parse_source_ref("polymarket", PM_REF)
        assert ref is not None and ref.market_id == "550700"

    def test_a_manifold_ref_resolves_to_its_contract_id(self) -> None:
        ref = parse_source_ref("manifold", MF_REF)
        assert ref is not None and ref.market_id == "6LpNssU8Cd"

    @pytest.mark.parametrize(
        ("source", "ref"),
        [
            ("polymarket", "polymarket:slug-only"),
            ("polymarket", "polymarket:slug:not-a-number"),
            ("polymarket", "manifold:abc"),
            ("manifold", "manifold:"),
            ("manifold", "manifold:a:b"),
            ("curated", "curated:anything"),
        ],
    )
    def test_anything_else_is_none_rather_than_a_guess(self, source: str, ref: str) -> None:
        assert parse_source_ref(source, ref) is None

    def test_the_history_window_ends_at_the_cutoff(self) -> None:
        url = polymarket_history_url(
            "123", start=PM_CUTOFF - DAY, cutoff=PM_CUTOFF, fidelity_minutes=1
        )
        query = parse_qs(urlparse(url).query)
        assert query["market"] == ["123"]
        assert query["fidelity"] == ["1"]
        end = int(query["endTs"][0])
        # Rounded up to the whole second, and not one second further.
        assert 0 <= end - PM_CUTOFF.timestamp() < 1
        assert int(query["startTs"][0]) == int((PM_CUTOFF - DAY).timestamp())
        assert "interval" not in query, "interval=max returns the whole life, outcome included"

    def test_the_bets_request_ends_at_the_cutoff(self) -> None:
        url = manifold_bets_url("abc", cutoff=MF_CUTOFF, limit=1000)
        query = parse_qs(urlparse(url).query)
        before_ms = int(query["beforeTime"][0])
        assert 0 <= before_ms - MF_CUTOFF.timestamp() * 1000 < 1
        assert query["contractId"] == ["abc"] and query["limit"] == ["1000"]


# ---------------------------------------------------------------------------
# Coverage: excluded means counted
# ---------------------------------------------------------------------------


class TestCoverage:
    def prices(self) -> list[MarketPrice]:
        return [
            priced("a", age=timedelta(seconds=30)),
            priced("b", age=timedelta(seconds=50)),
            priced("c", age=timedelta(days=3), source="manifold"),
            missing("d", "no_price_history"),
            missing("e", "no_price_history"),
            missing("f", "market_created_after_cutoff"),
            missing("g", "not_a_market", source="curated"),
        ]

    def test_every_scenario_lands_in_exactly_one_row(self) -> None:
        summary = coverage_summary(self.prices(), max_staleness=DAY)
        assert summary.n_scenarios == 7
        assert summary.n_usable == 2
        assert summary.n_stale == 1
        assert summary.n_priced == 3
        assert summary.n_usable + summary.n_stale + sum(n for _, n in summary.unobtainable) == 7

    def test_the_unobtainable_are_grouped_by_reason_and_sorted(self) -> None:
        summary = coverage_summary(self.prices(), max_staleness=DAY)
        assert summary.unobtainable == (
            ("market_created_after_cutoff", 1),
            ("no_price_history", 2),
            ("not_a_market", 1),
        )

    def test_the_breakdown_by_source_adds_up(self) -> None:
        summary = coverage_summary(self.prices(), max_staleness=DAY)
        assert summary.by_source == (
            ("curated", 1, 0, 0),
            ("manifold", 1, 0, 1),
            ("polymarket", 5, 2, 0),
        )
        assert sum(row[1] for row in summary.by_source) == summary.n_scenarios

    def test_the_staleness_distribution_includes_the_stale(self) -> None:
        """The distribution is what lets a reader judge the bound, so the bound
        must not pre-filter it."""
        summary = coverage_summary(self.prices(), max_staleness=DAY)
        assert summary.staleness is not None
        assert summary.staleness.n == 3
        assert summary.staleness.minimum == 30.0
        assert summary.staleness.p50 == 50.0
        assert summary.staleness.maximum == timedelta(days=3).total_seconds()

    def test_the_bound_moves_usable_into_stale_and_nothing_else(self) -> None:
        tight = coverage_summary(self.prices(), max_staleness=timedelta(seconds=40))
        loose = coverage_summary(self.prices(), max_staleness=timedelta(days=30))
        assert (tight.n_usable, tight.n_stale) == (1, 2)
        assert (loose.n_usable, loose.n_stale) == (3, 0)
        assert tight.unobtainable == loose.unobtainable
        assert tight.n_priced == loose.n_priced == 3

    def test_nothing_priced_means_no_distribution_not_zeros(self) -> None:
        summary = coverage_summary([missing("d", "no_price_history")], max_staleness=DAY)
        assert summary.staleness is None
        assert summary.n_usable == 0

    def test_a_repeated_scenario_is_refused(self) -> None:
        with pytest.raises(ValueError, match="more than once"):
            coverage_summary(
                [priced("a", age=DAY), missing("a", "no_price_history")], max_staleness=DAY
            )


# ---------------------------------------------------------------------------
# The baseline: excluded and counted, never imputed
# ---------------------------------------------------------------------------


class TestTheBaseline:
    def rows(self) -> list[tuple[MarketPrice, Any, str]]:
        return [
            (priced("a", age=timedelta(seconds=30), p=0.9), 1, "elections"),
            (priced("b", age=timedelta(days=5), p=0.2), 0, "sports"),
            (missing("c", "no_price_history"), 1, "sports"),
            (priced("d", age=timedelta(minutes=1), p=0.9995), 0, "corporate"),
        ]

    def test_only_usable_prices_are_scored(self) -> None:
        scored = scored_market_forecasts(self.rows(), max_staleness=DAY)
        assert [item.scenario_id for item in scored] == ["a", "d"]

    def test_a_missing_price_is_never_imputed(self) -> None:
        """Not 0.5, not the base rate, not anything: a market that said nothing
        is not a calibrated forecaster."""
        scored = scored_market_forecasts(self.rows(), max_staleness=DAY)
        assert "c" not in {item.scenario_id for item in scored}
        assert all(item.p_hat != 0.5 for item in scored)

    def test_a_stale_price_is_excluded_not_silently_used(self) -> None:
        scored = scored_market_forecasts(self.rows(), max_staleness=DAY)
        assert "b" not in {item.scenario_id for item in scored}
        loose = scored_market_forecasts(self.rows(), max_staleness=timedelta(days=30))
        assert "b" in {item.scenario_id for item in loose}

    def test_the_probability_is_passed_through_unclipped(self) -> None:
        scored = {i.scenario_id: i for i in scored_market_forecasts(self.rows(), max_staleness=DAY)}
        assert scored["d"].p_hat == 0.9995

    def test_log_loss_uses_the_projects_one_clipping_rule(self) -> None:
        """A market at 0.9995 that resolved NO must cost what 0.99 costs -- the
        existing clip -- and not explode, and not be clipped a second way."""
        from cascade.eval.metrics import LOG_LOSS_CLIP, log_loss
        from cascade.eval.score import metrics_for

        scored = [
            i for i in scored_market_forecasts(self.rows(), max_staleness=DAY) if i.outcome == 0
        ]
        assert [i.scenario_id for i in scored] == ["d"]
        measured = metrics_for(scored, config_id=MARKET_CONFIG_ID)
        assert measured.log_loss == pytest.approx(log_loss([1.0 - LOG_LOSS_CLIP], [0]))
        assert measured.brier == pytest.approx(0.9995**2), "Brier is on the price as quoted"

    def test_it_is_stamped_as_having_no_decider(self) -> None:
        scored = scored_market_forecasts(self.rows(), max_staleness=DAY)
        assert {item.policy for item in scored} == {"none"}
        assert {item.config_id for item in scored} == {MARKET_CONFIG_ID}

    def test_output_is_sorted_by_scenario(self) -> None:
        scored = scored_market_forecasts(list(reversed(self.rows())), max_staleness=DAY)
        assert [item.scenario_id for item in scored] == ["a", "d"]

    def test_the_comparison_against_it_says_it_runs_on_the_intersection(self) -> None:
        from cascade.eval.ablation import comparison_family

        family = comparison_family(available=[MARKET_CONFIG_ID, "C01", "C09"], headline="C01")
        spec = next(item for item in family if item.config_a == MARKET_CONFIG_ID)
        assert spec.config_b == "C01"
        assert "intersection" in spec.reading
        assert "never imputed" in spec.reading
        assert "strictly before the cutoff" in spec.reading

    def test_no_comparison_is_made_when_there_is_no_market_baseline(self) -> None:
        from cascade.eval.ablation import comparison_family

        family = comparison_family(available=["C01", "C09"], headline="C01")
        assert MARKET_CONFIG_ID not in {item.config_a for item in family}

    def test_it_is_declared_beside_the_specs_five_not_among_them(self) -> None:
        spec = next(item for item in BASELINES if item.baseline_id == "market")
        assert spec.config_id == MARKET_CONFIG_ID
        assert not spec.in_spec and not spec.from_grid
        assert sum(1 for item in BASELINES if item.in_spec) == 5


# ---------------------------------------------------------------------------
# The fetcher, through a mock transport
# ---------------------------------------------------------------------------


def _config(**overrides: Any) -> MarketBaselineConfig:
    return Settings().market_baseline.model_copy(update=overrides)


class Wire:
    """A mock transport that answers from a routing table and keeps a log."""

    def __init__(self, routes: dict[str, Any]) -> None:
        self.routes = routes
        self.log: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.log.append(url)
        for needle in sorted(self.routes, key=len, reverse=True):
            if needle in url:
                answer = self.routes[needle]
                if callable(answer):
                    answer = answer(request)
                if isinstance(answer, httpx.Response):
                    return answer
                return httpx.Response(200, json=answer)
        return httpx.Response(404, json={"error": "not found"})

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self))


def _fetcher(tmp_path: Path, wire: Wire, **kwargs: Any) -> tuple[MarketFetcher, list[float]]:
    slept: list[float] = []
    fetcher = MarketFetcher(
        cache_root=tmp_path / "cache",
        config=kwargs.pop("config", _config()),
        client=wire.client(),
        sleep=slept.append,
        now=lambda: NOW,
        **kwargs,
    )
    return fetcher, slept


def _refuse(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"network call in a run that must make none: {request.url}")


PM_SCENARIO = scenario("pm", source="polymarket", source_ref=PM_REF, cutoff=PM_CUTOFF)
LATE_SCENARIO = scenario("late", source="polymarket", source_ref=LATE_REF, cutoff=LATE_CUTOFF)
MF_SCENARIO = scenario("mf", source="manifold", source_ref=MF_REF, cutoff=MF_CUTOFF)


def _pm_routes(history: Any) -> dict[str, Any]:
    return {
        "gamma-api.polymarket.com/markets/550700": fixture("gamma_market_550700.json"),
        "clob.polymarket.com/prices-history": history,
    }


class TestFetchingPolymarket:
    def test_the_price_is_the_last_real_point_before_the_cutoff(self, tmp_path: Path) -> None:
        history = fixture("clob_history_550700_window.json")
        wire = Wire(_pm_routes(history))
        fetcher, slept = _fetcher(tmp_path, wire)

        price = fetcher.price_for(PM_SCENARIO)

        last = history["history"][-1]
        assert price.probability == last["p"]
        assert price.observed_at == datetime.fromtimestamp(last["t"], tz=UTC)
        assert price.observed_at < PM_CUTOFF
        assert price.staleness is not None and price.staleness < timedelta(minutes=2)
        assert price.source_ref == PM_REF
        # Two hops, each followed by the configured pause.
        assert fetcher.requests == 2
        assert slept == [0.5, 0.5]

    def test_the_history_is_asked_for_the_yes_token_up_to_the_cutoff(self, tmp_path: Path) -> None:
        wire = Wire(_pm_routes(fixture("clob_history_550700_window.json")))
        fetcher, _ = _fetcher(tmp_path, wire)
        fetcher.price_for(PM_SCENARIO)

        token = json.loads(fixture("gamma_market_550700.json")["clobTokenIds"])[0]
        asked = [parse_qs(urlparse(url).query) for url in wire.log if "prices-history" in url]
        assert asked and all(query["market"] == [token] for query in asked)
        assert all(int(q["endTs"][0]) - PM_CUTOFF.timestamp() < 1 for q in asked)

    def test_a_point_stamped_at_the_cutoff_is_not_the_price(self, tmp_path: Path) -> None:
        """End to end: the source may answer inclusively; the selector may not."""
        cutoff = datetime(2025, 8, 12, 7, 3, 9, tzinfo=UTC)  # a whole second
        history = {
            "history": [
                {"t": int(cutoff.timestamp()) - 60, "p": 0.41},
                {"t": int(cutoff.timestamp()), "p": 0.97},  # synthetic: exactly at the cutoff
            ]
        }
        fetcher, _ = _fetcher(tmp_path, Wire(_pm_routes(history)))
        price = fetcher.price_for(
            scenario("pm", source="polymarket", source_ref=PM_REF, cutoff=cutoff)
        )
        assert price.probability == 0.41

    def test_the_price_does_not_depend_on_how_the_market_resolved(self, tmp_path: Path) -> None:
        results = []
        for index, resolution in enumerate(('["1", "0"]', '["0", "1"]')):
            routes = _pm_routes(fixture("clob_history_550700_window.json"))
            document = dict(routes["gamma-api.polymarket.com/markets/550700"])
            document["outcomePrices"] = resolution  # synthetic
            routes["gamma-api.polymarket.com/markets/550700"] = document
            fetcher, _ = _fetcher(tmp_path / str(index), Wire(routes))
            results.append(fetcher.price_for(PM_SCENARIO).probability)
        assert results[0] == results[1]

    def test_a_rerun_replays_the_recordings_and_makes_no_request(self, tmp_path: Path) -> None:
        """Rebuildable and resumable: the cache is the checkpoint."""
        first, _ = _fetcher(tmp_path, Wire(_pm_routes(fixture("clob_history_550700_window.json"))))
        recorded = first.price_for(PM_SCENARIO)

        second, slept = _fetcher(tmp_path, Wire({"": _refuse}))
        replayed = second.price_for(PM_SCENARIO)

        assert (replayed.probability, replayed.observed_at) == (
            recorded.probability,
            recorded.observed_at,
        )
        assert second.requests == 0 and second.replays == 2
        assert slept == [], "nothing was asked, so nothing is paced"

    def test_an_empty_staleness_window_falls_back_and_reports_the_age(self, tmp_path: Path) -> None:
        stale_at = int((PM_CUTOFF - timedelta(days=3)).timestamp())

        def history(request: httpx.Request) -> httpx.Response:
            wide = request.url.params["fidelity"] == "60"
            points = [{"t": stale_at, "p": 0.33}] if wide else []
            return httpx.Response(200, json={"history": points})

        wire = Wire(_pm_routes(history))
        fetcher, _ = _fetcher(tmp_path, wire)
        price = fetcher.price_for(PM_SCENARIO)

        assert price.probability == 0.33
        assert price.staleness is not None and price.staleness > timedelta(days=2)
        assert is_stale(price, max_staleness=DAY), "reported with its age, and not scored"
        spans = [
            int(q["endTs"][0]) - int(q["startTs"][0])
            for q in (parse_qs(urlparse(u).query) for u in wire.log if "prices-history" in u)
        ]
        assert [round(span / 86400) for span in spans] == [1, 14]

    def test_no_history_at_all_is_a_reason_not_a_number(self, tmp_path: Path) -> None:
        fetcher, _ = _fetcher(tmp_path, Wire(_pm_routes(fixture("clob_history_empty.json"))))
        price = fetcher.price_for(PM_SCENARIO)
        assert price.probability is None
        assert price.unobtainable_reason == "no_price_history"

    def test_a_market_created_after_its_cutoff_says_so(self, tmp_path: Path) -> None:
        """Real case: Gamma `startDate` 2023-01, `createdAt` 2023-12, and the
        registry's cutoff -- derived from `startDate` -- falls in between."""
        wire = Wire(
            {
                "gamma-api.polymarket.com/markets/253452": fixture("gamma_market_253452.json"),
                "clob.polymarket.com/prices-history": fixture("clob_history_empty.json"),
            }
        )
        fetcher, _ = _fetcher(tmp_path, wire)
        price = fetcher.price_for(LATE_SCENARIO)
        assert price.unobtainable_reason == "market_created_after_cutoff"
        assert price.probability is None
        assert "createdAt" in price.detail

    def test_a_refused_lookup_is_fetch_failed_and_is_not_recorded(self, tmp_path: Path) -> None:
        wire = Wire({"gamma-api": httpx.Response(429, text="slow down")})
        fetcher, slept = _fetcher(tmp_path, wire)
        price = fetcher.price_for(PM_SCENARIO)
        assert price.unobtainable_reason == "fetch_failed"
        assert "429" in price.detail
        assert slept == [0.5], "a refusal is paced too"

        # Nothing was recorded, so the next run asks again rather than
        # replaying the failure.
        retry, _ = _fetcher(tmp_path, Wire(_pm_routes(fixture("clob_history_550700_window.json"))))
        assert retry.price_for(PM_SCENARIO).priced

    def test_a_changed_api_shape_is_fetch_failed_not_an_empty_market(self, tmp_path: Path) -> None:
        fetcher, _ = _fetcher(tmp_path, Wire(_pm_routes({"data": []})))
        price = fetcher.price_for(PM_SCENARIO)
        assert price.unobtainable_reason == "fetch_failed"
        assert "MarketPayloadError" in price.detail

    def test_a_market_without_a_yes_token_says_so(self, tmp_path: Path) -> None:
        document = dict(fixture("gamma_market_550700.json"))
        document["clobTokenIds"] = "[]"
        wire = Wire({"gamma-api.polymarket.com/markets/550700": document})
        fetcher, _ = _fetcher(tmp_path, wire)
        assert fetcher.price_for(PM_SCENARIO).unobtainable_reason == "no_yes_token"
        assert not [url for url in wire.log if "prices-history" in url]

    def test_offline_with_nothing_recorded_is_not_recorded(self, tmp_path: Path) -> None:
        fetcher, slept = _fetcher(tmp_path, Wire({"": _refuse}), offline=True)
        price = fetcher.price_for(PM_SCENARIO)
        assert price.unobtainable_reason == "not_recorded"
        assert "market-prices" in price.detail
        assert fetcher.requests == 0 and slept == []

    def test_refresh_and_offline_together_are_refused(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="contradict"):
            MarketFetcher(cache_root=tmp_path, config=_config(), refresh=True, offline=True)

    def test_the_pause_follows_the_configured_rate(self, tmp_path: Path) -> None:
        wire = Wire(_pm_routes(fixture("clob_history_550700_window.json")))
        fetcher, slept = _fetcher(tmp_path, wire, config=_config(requests_per_second=0.25))
        fetcher.price_for(PM_SCENARIO)
        assert slept == [4.0, 4.0]


class TestFetchingManifold:
    def test_the_price_is_the_last_bets_prob_after(self, tmp_path: Path) -> None:
        payload = fixture("manifold_bets_6LpNssU8Cd.json")
        wire = Wire({"api.manifold.markets/v0/bets": payload})
        fetcher, _ = _fetcher(tmp_path, wire)

        price = fetcher.price_for(MF_SCENARIO)

        newest_real = next(bet for bet in payload if not bet.get("isRedemption"))
        assert price.probability == newest_real["probAfter"]
        assert price.observed_at is not None and price.observed_at < MF_CUTOFF
        assert fetcher.requests == 1
        query = parse_qs(urlparse(wire.log[0]).query)
        assert query["contractId"] == ["6LpNssU8Cd"]
        assert int(query["beforeTime"][0]) - MF_CUTOFF.timestamp() * 1000 < 1

    def test_a_thin_market_is_priced_and_reported_stale(self, tmp_path: Path) -> None:
        """Real case: the last bet on this contract was 2.4 days before the
        cutoff. It is a price, it is reported with its age, it is not scored."""
        wire = Wire({"api.manifold.markets/v0/bets": fixture("manifold_bets_6LpNssU8Cd.json")})
        fetcher, _ = _fetcher(tmp_path, wire)
        price = fetcher.price_for(MF_SCENARIO)
        assert price.staleness is not None and price.staleness > timedelta(days=2)
        assert is_stale(price, max_staleness=DAY)

    def test_a_bet_at_the_cutoff_is_not_the_price(self, tmp_path: Path) -> None:
        cutoff = datetime(2025, 11, 3, 5, 5, 16, 534000, tzinfo=UTC)  # a whole millisecond
        at_ms = int(cutoff.timestamp() * 1000)
        payload = [
            {"createdTime": at_ms, "probAfter": 0.98},  # synthetic: exactly at the cutoff
            {"createdTime": at_ms - 1, "probAfter": 0.12},
        ]
        fetcher, _ = _fetcher(tmp_path, Wire({"api.manifold.markets/v0/bets": payload}))
        price = fetcher.price_for(
            scenario("mf", source="manifold", source_ref=MF_REF, cutoff=cutoff)
        )
        assert price.probability == 0.12

    def test_no_bets_before_the_cutoff_is_a_reason(self, tmp_path: Path) -> None:
        fetcher, _ = _fetcher(tmp_path, Wire({"api.manifold.markets/v0/bets": []}))
        price = fetcher.price_for(MF_SCENARIO)
        assert price.probability is None
        assert price.unobtainable_reason == "no_price_history"


class TestFetchingTheRegistry:
    def test_a_question_without_a_market_is_counted_and_never_asked_about(
        self, tmp_path: Path
    ) -> None:
        fetcher, _ = _fetcher(tmp_path, Wire({"": _refuse}))
        curated = scenario("cur", source="curated", source_ref="curated:x", cutoff=PM_CUTOFF)
        price = fetcher.price_for(curated)
        assert price.unobtainable_reason == "not_a_market"
        assert fetcher.requests == 0

    def test_an_unparseable_ref_is_a_reason(self, tmp_path: Path) -> None:
        fetcher, _ = _fetcher(tmp_path, Wire({"": _refuse}))
        odd = scenario("odd", source="polymarket", source_ref="polymarket:slug", cutoff=PM_CUTOFF)
        assert fetcher.price_for(odd).unobtainable_reason == "unparseable_ref"

    def test_every_scenario_yields_one_row_in_sorted_order(self, tmp_path: Path) -> None:
        routes = _pm_routes(fixture("clob_history_550700_window.json"))
        routes["api.manifold.markets/v0/bets"] = fixture("manifold_bets_6LpNssU8Cd.json")
        fetcher, _ = _fetcher(tmp_path, Wire(routes))
        curated = scenario("cur", source="curated", source_ref="curated:x", cutoff=PM_CUTOFF)
        seen: list[str] = []

        prices = fetch_market_prices(
            [PM_SCENARIO, curated, MF_SCENARIO],
            fetcher,
            on_price=lambda price: seen.append(price.scenario_id),
        )

        assert [price.scenario_id for price in prices] == ["cur", "mf", "pm"]
        assert seen == ["cur", "mf", "pm"]
        summary = coverage_summary(prices, max_staleness=DAY)
        assert (summary.n_usable, summary.n_stale, summary.unobtainable) == (
            1,
            1,
            (("not_a_market", 1),),
        )


# ---------------------------------------------------------------------------
# The store: routed, not copied -- and the CLI that fills it
# ---------------------------------------------------------------------------


class FakeCursor:
    """Answers each statement from a table of ``substring -> rows``."""

    def __init__(self, answers: dict[str, list[tuple[Any, ...]]], log: list[str]) -> None:
        self.answers = answers
        self.log = log
        self.rows: list[tuple[Any, ...]] = []

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        flat = " ".join(sql.split())
        self.log.append(flat)
        self.rows = next(
            (rows for needle, rows in sorted(self.answers.items()) if needle in flat), []
        )

    def executemany(self, sql: str, rows: Any) -> None:
        self.log.append(" ".join(sql.split()))
        self.rows = list(rows)

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self.rows


class FakeConnection:
    def __init__(self, answers: dict[str, list[tuple[Any, ...]]], log: list[str]) -> None:
        self.answers = answers
        self.log = log
        self.commits = 0

    def __enter__(self) -> FakeConnection:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def cursor(self) -> FakeCursor:
        return FakeCursor(self.answers, self.log)

    def commit(self) -> None:
        self.commits += 1


def _row(price: MarketPrice, *extra: Any) -> tuple[Any, ...]:
    return (
        price.scenario_id,
        price.source,
        price.source_ref,
        price.cutoff_ts,
        price.probability,
        price.observed_at,
        price.unobtainable_reason,
        price.detail,
        price.fetched_at,
        *extra,
    )


class TestTheStore:
    def connect(
        self, monkeypatch: pytest.MonkeyPatch, answers: dict[str, list[tuple[Any, ...]]]
    ) -> tuple[list[str], list[str]]:
        from cascade.eval import store

        log: list[str] = []
        roles: list[str] = []

        def fake(settings: Settings, role: str) -> FakeConnection:
            roles.append(role)
            return FakeConnection(answers, log)

        monkeypatch.setattr(store, "_connect", fake)
        return log, roles

    def test_the_market_config_is_scored_from_its_own_table(
        self, monkeypatch: pytest.MonkeyPatch, settings: Settings
    ) -> None:
        """Routed inside `scored_forecasts`, so `eval score`, the report and the
        Holm family reach it by the path every other configuration takes."""
        from cascade.eval.store import scored_forecasts

        rows = [
            _row(priced("a", age=timedelta(seconds=30), p=0.8), 1, "elections"),
            _row(priced("b", age=timedelta(days=9), p=0.1), 0, "sports"),
            _row(missing("c", "no_price_history"), 0, "sports"),
        ]
        log, roles = self.connect(monkeypatch, {"FROM market_prices": rows})

        scored = scored_forecasts(settings, config_id=MARKET_CONFIG_ID)

        assert [(item.scenario_id, item.p_hat, item.outcome) for item in scored] == [("a", 0.8, 1)]
        assert roles == ["eval"]
        assert all("FROM forecasts" not in statement for statement in log)
        # The cutoff the price was selected against must still be the registry's.
        assert any("s.cutoff_ts = m.cutoff_ts" in statement for statement in log)

    def test_it_is_listed_with_its_usable_count(
        self, monkeypatch: pytest.MonkeyPatch, settings: Settings
    ) -> None:
        from cascade.eval.store import available_configs

        rows = [
            _row(priced("a", age=timedelta(seconds=30))),
            _row(priced("b", age=timedelta(days=9))),
            _row(missing("c", "no_price_history")),
        ]
        log, _ = self.connect(
            monkeypatch, {"FROM forecasts": [("C09", 40)], "FROM market_prices": rows}
        )
        assert available_configs(settings) == ((MARKET_CONFIG_ID, 1), ("C09", 40))
        # Counted over prices selected against the registry's *current* cutoff,
        # and without reading a label: coverage needs none.
        counted = next(statement for statement in log if "FROM market_prices" in statement)
        assert "s.cutoff_ts = m.cutoff_ts" in counted
        assert "scenario_labels" not in counted

    def test_it_is_absent_when_no_price_is_usable(
        self, monkeypatch: pytest.MonkeyPatch, settings: Settings
    ) -> None:
        """Absent, like any configuration with nothing stored -- not listed at
        zero, which the report would read as a baseline that was produced."""
        from cascade.eval.store import available_configs

        rows = [_row(missing("c", "no_price_history"))]
        self.connect(monkeypatch, {"FROM forecasts": [("C09", 40)], "FROM market_prices": rows})
        assert available_configs(settings) == (("C09", 40),)

    def test_prices_are_written_by_the_admin_role_into_their_own_table(
        self, monkeypatch: pytest.MonkeyPatch, settings: Settings
    ) -> None:
        from cascade.eval.store import write_market_prices

        log, roles = self.connect(monkeypatch, {})
        written = write_market_prices(
            settings, [missing("z", "not_a_market"), priced("a", age=timedelta(seconds=5))]
        )
        assert written == 2
        assert roles == ["admin"]
        assert len(log) == 1 and log[0].startswith("INSERT INTO market_prices")
        assert "forecasts" not in log[0]

    def test_a_stored_row_that_breaks_the_lock_fails_on_read(
        self, monkeypatch: pytest.MonkeyPatch, settings: Settings
    ) -> None:
        """The CHECK holds the lock in the table; the model re-asserts it, so a
        row written around the constraint is not scored."""
        from cascade.eval.store import load_market_prices

        good = priced("a", age=timedelta(seconds=30))
        tampered = list(_row(good))
        tampered[5] = good.cutoff_ts  # observed_at == cutoff_ts
        self.connect(monkeypatch, {"FROM market_prices": [tuple(tampered)]})
        with pytest.raises(ValidationError, match="time lock"):
            load_market_prices(settings)


class TestTheCommand:
    def run(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        routes: dict[str, Any],
        scenarios: list[Scenario],
        *args: str,
    ) -> tuple[Any, list[MarketPrice]]:
        from typer.testing import CliRunner

        from cascade import cli
        from cascade.eval import market_fetch, store
        from cascade.ledger import store as ledger_store

        stored: list[MarketPrice] = []
        real = market_fetch.MarketFetcher

        def fetcher(**kwargs: Any) -> MarketFetcher:
            kwargs["cache_root"] = tmp_path / "cache"
            return real(client=Wire(routes).client(), sleep=lambda _s: None, **kwargs)

        monkeypatch.setattr(market_fetch, "MarketFetcher", fetcher)
        monkeypatch.setattr(ledger_store, "load_scenarios", lambda *_a, **_k: tuple(scenarios))
        monkeypatch.setattr(
            store,
            "write_market_prices",
            lambda _s, prices, **_k: stored.extend(prices) or len(prices),
        )
        result = CliRunner().invoke(
            cli.app, ["eval", "market-prices", *args], env={"COLUMNS": "200"}
        )
        return result, stored

    def test_it_prints_the_coverage_and_stores_every_row(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        routes = _pm_routes(fixture("clob_history_550700_window.json"))
        routes["api.manifold.markets/v0/bets"] = fixture("manifold_bets_6LpNssU8Cd.json")
        curated = scenario("cur", source="curated", source_ref="curated:x", cutoff=PM_CUTOFF)

        result, stored = self.run(
            monkeypatch, tmp_path, routes, [PM_SCENARIO, MF_SCENARIO, curated]
        )

        assert result.exit_code == 0, result.output
        assert "usable" in result.output and "stale" in result.output
        assert "unobtainable: not_a_market" in result.output
        assert "never imputed" in result.output
        assert "staleness over 2 priced scenario(s)" in result.output
        assert [price.scenario_id for price in stored] == ["cur", "mf", "pm"]
        assert [price.priced for price in stored] == [False, True, True]

    def test_no_write_stores_nothing(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        routes = _pm_routes(fixture("clob_history_550700_window.json"))
        result, stored = self.run(monkeypatch, tmp_path, routes, [PM_SCENARIO], "--no-write")
        assert result.exit_code == 0, result.output
        assert stored == []

    def test_an_unfinished_fetch_exits_three(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A source that did not answer is not a fact about the market, and the
        run must not pass as finished."""
        from cascade.version import EXIT_PRECONDITION

        routes = {"gamma-api": httpx.Response(503, text="unavailable")}
        result, stored = self.run(monkeypatch, tmp_path, routes, [PM_SCENARIO])
        assert result.exit_code == EXIT_PRECONDITION
        assert "unobtainable: fetch_failed" in result.output
        assert [price.unobtainable_reason for price in stored] == ["fetch_failed"]

    def test_refresh_and_offline_together_exit_three(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from cascade.version import EXIT_PRECONDITION

        result, _ = self.run(monkeypatch, tmp_path, {}, [PM_SCENARIO], "--refresh", "--offline")
        assert result.exit_code == EXIT_PRECONDITION


# ---------------------------------------------------------------------------
# The grant, read off the SQL
# ---------------------------------------------------------------------------


def _sql_statements(path: Path) -> list[str]:
    """The migration's statements with comments and string literals removed.

    Prose in this migration *names* `cascade_sim` to explain its absence, so
    the check has to read code, not text.
    """
    text = re.sub(r"--[^\n]*", "", path.read_text(encoding="utf-8"))
    text = re.sub(r"'(?:[^']|'')*'", "''", text)
    return [" ".join(part.split()) for part in text.split(";") if part.strip()]


def _grants_on(table: str, statements: list[str]) -> list[str]:
    return [
        statement
        for statement in statements
        if re.match(r"(?i)^grant\b", statement)
        and re.search(rf"(?i)\bon\s+(table\s+)?{table}\b", statement)
    ]


class TestMigration017:
    path = MIGRATIONS / "017_market_prices.sql"

    def test_cascade_sim_is_granted_nothing(self) -> None:
        """The mechanism is the *absence* of a grant (ADR-0005)."""
        grants = _grants_on("market_prices", _sql_statements(self.path))
        assert grants, "the migration must grant the eval role something"
        for statement in grants:
            grantees = statement.lower().split(" to ", 1)[1]
            assert "cascade_sim" not in grantees, statement
            assert "public" not in grantees, statement

    def test_no_blanket_grant_reaches_the_table(self) -> None:
        for statement in _sql_statements(self.path):
            assert not re.search(r"(?i)grant\b.*\ball tables\b", statement), statement

    def test_cascade_eval_reads_and_cannot_write(self) -> None:
        grants = _grants_on("market_prices", _sql_statements(self.path))
        assert grants == ["GRANT SELECT ON market_prices TO cascade_eval"]

    def test_no_later_migration_hands_the_table_to_the_simulation(self) -> None:
        for path in sorted(MIGRATIONS.glob("*.sql")):
            for statement in _grants_on("market_prices", _sql_statements(path)):
                assert "cascade_sim" not in statement.lower(), f"{path.name}: {statement}"

    def test_the_time_lock_is_a_strict_constraint(self) -> None:
        body = " ".join(_sql_statements(self.path))
        assert "observed_at < cutoff_ts" in body
        assert "observed_at <= cutoff_ts" not in body

    def test_a_row_is_priced_or_explained(self) -> None:
        body = " ".join(_sql_statements(self.path))
        assert "(probability IS NULL) = (observed_at IS NULL)" in body
        assert "(probability IS NULL) <> (unobtainable_reason IS NULL)" in body

    def test_no_default_probability_exists(self) -> None:
        column = next(
            line
            for line in self.path.read_text(encoding="utf-8").splitlines()
            if line.strip().startswith("probability ")
        )
        assert "default" not in column.lower()
        assert "not null" not in column.lower(), "unobtainable must be representable as NULL"

    def test_the_detector_would_catch_a_grant_to_the_simulation(self, tmp_path: Path) -> None:
        """A guard that cannot fail is not a guard."""
        bad = tmp_path / "bad.sql"
        bad.write_text(
            "-- cascade_sim gets nothing\n"
            "GRANT SELECT ON market_prices TO cascade_eval, cascade_sim;\n"
        )
        grants = _grants_on("market_prices", _sql_statements(bad))
        assert any("cascade_sim" in statement for statement in grants)


# ---------------------------------------------------------------------------
# Structure: purity, and the one table the price may live in
# ---------------------------------------------------------------------------


class TestStructure:
    def test_the_pure_module_imports_nothing_that_does_io(self) -> None:
        tree = ast.parse((REPO_ROOT / "cascade" / "eval" / "market.py").read_text())
        roots: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                roots.add(node.module)
        impure = {"httpx", "psycopg", "time", "os", "pathlib", "random", "requests", "socket"}
        assert not {name for name in roots if name.split(".")[0] in impure}
        assert not {
            name for name in roots if name.startswith(("cascade.ledger.http", "cascade.db"))
        }

    def test_the_pure_module_never_reads_the_clock(self) -> None:
        tree = ast.parse((REPO_ROOT / "cascade" / "eval" / "market.py").read_text())
        calls = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        assert not calls & {"now", "utcnow", "today"}

    @pytest.mark.parametrize("module", ["market.py", "market_fetch.py"])
    def test_the_price_is_never_written_into_forecasts(self, module: str) -> None:
        """`cascade_sim` can read `forecasts`; a copy there would undo the grant."""
        source = (REPO_ROOT / "cascade" / "eval" / module).read_text()
        assert "write_forecast" not in source

    def test_the_cli_stores_prices_only_in_their_own_table(self) -> None:
        source = (REPO_ROOT / "cascade" / "cli.py").read_text()
        start = source.index("def _market_prices(")
        body = source[start : source.index("\n@eval_app.command", start)]
        assert "write_market_prices" in body
        assert "write_forecast" not in body


class TestConfig:
    def test_the_shipped_section_binds(self) -> None:
        config = Settings().market_baseline
        assert config.max_staleness_hours == 24
        assert config.lookback_days == 14
        assert config.requests_per_second <= 2.0, "politeness: at most two requests a second"

    def test_a_lookback_shorter_than_the_bound_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="lookback_days"):
            MarketBaselineConfig(
                max_staleness_hours=72,
                lookback_days=1,
                fidelity_minutes=1,
                lookback_fidelity_minutes=60,
                manifold_bets_limit=1000,
                requests_per_second=2.0,
            )

    def test_the_market_url_is_the_documented_one(self) -> None:
        assert polymarket_market_url("550700") == (
            "https://gamma-api.polymarket.com/markets/550700"
        )
