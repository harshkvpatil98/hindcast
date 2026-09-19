"""Fetching each market's price at its cutoff. The shell around ``market.py``.

Everything that decides *which* price is used lives in ``cascade/eval/market.py``
and is pure. This module only asks the sources, politely, and records what
they said.

**What the services actually are** (verified live, 2026-09, not from memory):

* Polymarket, two hops. ``GET gamma-api.polymarket.com/markets/{id}`` returns
  the market document, whose ``clobTokenIds`` (a JSON-encoded string) holds the
  YES and NO token ids in the order of ``outcomes``. Then
  ``GET clob.polymarket.com/prices-history?market={token}&startTs=&endTs=&fidelity=``
  returns ``{"history": [{"t": <unix s>, "p": <price>}]}`` -- the parameters are
  the ones the service's own OpenAPI document lists. Three things it does not
  say: ``interval=max`` with no ``fidelity`` answers ``{"history": []}`` for a
  resolved market; a ``startTs``/``endTs`` span much past two weeks is refused
  with HTTP 400 "interval is too long"; and the series is *sampled* once a
  minute whether or not anything traded, so the last point before a cutoff is
  normally under a minute old.
* Manifold, one hop. ``GET api.manifold.markets/v0/bets?contractId=&beforeTime=&limit=``
  returns bets newest first, each with ``createdTime`` (epoch ms) and
  ``probAfter``, the AMM's probability once that bet landed.

Raw responses go through the ledger's content-addressed :class:`SourceCache`,
so a rebuild replays them byte for byte and the benchmark is a function of a
directory rather than of the day it was fetched. That is also what makes this
phase resumable (invariant 8): a recorded response is never asked for twice,
and a failed one is never recorded, so re-running fetches exactly what is
missing.

Requests end at the cutoff. A resolved market's later prices converge on its
outcome, and the cleanest way not to use them is not to ask for them.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from cascade.config import MarketBaselineConfig
from cascade.eval.market import (
    MARKET_SOURCES,
    MarketPayloadError,
    MarketPrice,
    PriceObservation,
    UnobtainableReason,
    last_before,
    manifold_bets_url,
    market_created_at,
    parse_manifold_bets,
    parse_polymarket_history,
    parse_source_ref,
    polymarket_history_url,
    polymarket_market_url,
    polymarket_yes_token,
    unobtainable,
)
from cascade.ledger.http import SourceCache, SourceFetchError, SourceOffline
from cascade.ledger.schema import Scenario

__all__ = ["MarketFetcher", "fetch_market_prices"]


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass
class MarketFetcher:
    """Recorded responses first, the network only for what is missing, paced.

    ``offline`` never touches the network: an unrecorded response becomes the
    reason ``not_recorded``. ``refresh`` re-asks for everything, which is a
    deliberate act -- the sources can revise history, and a refreshed cache is
    a different benchmark.
    """

    cache_root: Path
    config: MarketBaselineConfig
    refresh: bool = False
    offline: bool = False
    client: httpx.Client | None = None
    sleep: Callable[[float], None] = time.sleep
    now: Callable[[], datetime] = _utc_now
    requests: int = 0
    replays: int = 0
    _recorded: SourceCache = field(init=False, repr=False)
    _live: SourceCache = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if self.refresh and self.offline:
            raise ValueError("refresh and offline contradict each other")
        # Two views of one directory, because SourceCache's `refresh` is
        # all-or-nothing and resuming needs "replay what exists, fetch the
        # rest". They share a client so there is one connection pool.
        self._recorded = SourceCache(self.cache_root, refresh=False, client=self.client)
        self._live = SourceCache(self.cache_root, refresh=True, client=self.client)

    def close(self) -> None:
        self._live.close()
        self._recorded.close()

    def get_json(self, url: str) -> Any:
        """The body for ``url``: from disk when recorded, else fetched and paced.

        Preserves politeness as a property of the fetcher rather than of its
        callers: every request that reaches a source is followed by the
        configured pause, so no caller can forget it.
        """
        if not self.refresh:
            try:
                payload = self._recorded.get_json(url)
            except SourceOffline:
                if self.offline:
                    raise
            else:
                self.replays += 1
                return payload
        try:
            return self._live.get_json(url)
        finally:
            # In `finally`, so a refused request is paced too: a burst of
            # retried failures is how GDELT's throttle was earned (M2).
            self.requests += 1
            self.sleep(1.0 / self.config.requests_per_second)

    def recorded_at(self, url: str) -> datetime:
        """When the recording for ``url`` was written; now, if there is none."""
        path = self._recorded.path_for(url)
        if path.is_file():
            return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
        return self.now()

    # -- one scenario ---------------------------------------------------------

    def price_for(self, scenario: Scenario) -> MarketPrice:
        """One scenario's market price at its own cutoff, or the reason for none.

        Never raises for a fact about a market and never returns a probability
        it did not read: every failure path ends in :func:`unobtainable`.
        """

        def none(reason: UnobtainableReason, detail: str = "") -> MarketPrice:
            return unobtainable(
                scenario_id=scenario.scenario_id,
                source=scenario.source,
                source_ref=scenario.source_ref,
                cutoff=scenario.cutoff_ts,
                reason=reason,
                fetched_at=self.now(),
                detail=detail,
            )

        if scenario.source not in MARKET_SOURCES:
            return none("not_a_market", f"source {scenario.source!r} has no market")
        ref = parse_source_ref(scenario.source, scenario.source_ref)
        if ref is None:
            return none("unparseable_ref", scenario.source_ref)

        try:
            if ref.source == "polymarket":
                found, url, detail = self._polymarket(ref.market_id, scenario.cutoff_ts)
            else:
                found, url, detail = self._manifold(ref.market_id, scenario.cutoff_ts)
        except SourceOffline:
            # SourceCache's own message names `cascade ledger build`, which is
            # the wrong command to send an operator to from here.
            return none(
                "not_recorded",
                "a response this price needs was never recorded; re-run "
                "`cascade eval market-prices` without --offline",
            )
        except (SourceFetchError, MarketPayloadError, httpx.HTTPError) as exc:
            return none("fetch_failed", f"{type(exc).__name__}: {exc}"[:500])

        if isinstance(found, str):
            return none(found, detail)
        return MarketPrice(
            scenario_id=scenario.scenario_id,
            source=scenario.source,
            source_ref=scenario.source_ref,
            cutoff_ts=scenario.cutoff_ts,
            probability=found.probability,
            observed_at=found.observed_at,
            detail=detail,
            fetched_at=self.recorded_at(url),
        )

    def _polymarket(
        self, market_id: str, cutoff: datetime
    ) -> tuple[PriceObservation | UnobtainableReason, str, str]:
        """``(observation-or-reason, url it came from, detail)`` for one market."""
        document = self.get_json(polymarket_market_url(market_id))
        if not isinstance(document, dict):
            raise MarketPayloadError(f"Gamma market {market_id} is not a JSON object")
        token = polymarket_yes_token(document)
        if token is None:
            return "no_yes_token", "", f"Gamma market {market_id} carries no Yes/No token pair"

        # First the window a usable price must fall in, at the source's own
        # sampling interval; only if that is empty, the wide one -- so a stale
        # price is reported with its age instead of as "no history".
        windows = (
            (timedelta(hours=self.config.max_staleness_hours), self.config.fidelity_minutes),
            (timedelta(days=self.config.lookback_days), self.config.lookback_fidelity_minutes),
        )
        for span, fidelity in windows:
            url = polymarket_history_url(
                token, start=cutoff - span, cutoff=cutoff, fidelity_minutes=fidelity
            )
            chosen = last_before(parse_polymarket_history(self.get_json(url)), cutoff=cutoff)
            if chosen is not None:
                return chosen, url, f"clob token {token[:12]}..., fidelity {fidelity} min"

        created = market_created_at(document)
        if created is not None and created >= cutoff:
            return (
                "market_created_after_cutoff",
                "",
                f"Gamma createdAt {created.isoformat()} is not before the cutoff "
                f"{cutoff.isoformat()}: the registry dated this cutoff from a startDate "
                "that precedes the market's own creation",
            )
        return (
            "no_price_history",
            "",
            f"no CLOB observation in the {self.config.lookback_days} days before the cutoff",
        )

    def _manifold(
        self, contract_id: str, cutoff: datetime
    ) -> tuple[PriceObservation | UnobtainableReason, str, str]:
        url = manifold_bets_url(contract_id, cutoff=cutoff, limit=self.config.manifold_bets_limit)
        payload = self.get_json(url)
        chosen = last_before(parse_manifold_bets(payload), cutoff=cutoff)
        if chosen is None:
            n = len(payload) if isinstance(payload, list) else 0
            return "no_price_history", "", f"{n} bet(s) before the cutoff, none an observation"
        return chosen, url, "probAfter of the last bet before the cutoff"


def fetch_market_prices(
    scenarios: Sequence[Scenario],
    fetcher: MarketFetcher,
    *,
    on_price: Callable[[MarketPrice], None] | None = None,
) -> tuple[MarketPrice, ...]:
    """One :class:`MarketPrice` per scenario, in scenario-id order.

    Every scenario yields a row -- priced or explained -- so the caller's
    coverage arithmetic starts from the whole registry and an excluded
    scenario is a counted one. Order is sorted (invariant 7), so two runs over
    the same cache produce the same sequence.
    """
    out: list[MarketPrice] = []
    for scenario in sorted(scenarios, key=lambda item: item.scenario_id):
        price = fetcher.price_for(scenario)
        out.append(price)
        if on_price is not None:
            on_price(price)
    return tuple(out)
