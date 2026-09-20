"""Reconciling the local cost ledger against every other record of the same
spend (spec §12.4; M8, extended at M15).

§12.4 makes this a gate, not a dashboard: "Token counts and cost are written to
the runs table on completion of each run and reconciled against the Langfuse
total at the end of each phase. A discrepancy above 2% is a bug in the meter
and blocks the report."

**Independence is a property of who wrote the record, not of how many records
there are.** The **meter** prices every call from the pinned per-token table
(`llm/meter.py`) in exact `Decimal` and writes the per-run total to
`runs.cost_usd`. The **tracer** emits one Langfuse generation per call carrying
the same cost. Those are two pieces of code, but one process: a call the client
never made is missing from both, and a usage figure the SDK reported wrongly is
wrong in both. Langfuse therefore *corroborates* the meter -- it catches a
tracer that drifted from the meter -- and cannot *verify* it. A record written
by the provider's own service side can (`trace/aws_spend.py`, ADR-0048).

So the comparison is against **N sources**, and every verdict here names which
of them were actually read. A reconciliation that cannot say what it compared
is a gate that cannot fail, which is the defect M8 already found once in this
file: zero against zero agreed to 0.0000% over 452,328 model calls.

Three distinctions are kept apart, and adding sources must not blur any of
them:

* **not configured** -- never asked. Listed, so a partial check is visible, and
  it does not block: a deployment with no AWS account still has §12.4's gate.
* **unreachable** -- asked and could not be read. Blocks. `tracing.py` is
  allowed to degrade to a no-op because observability must never fail a run,
  so this is *unreconciled* rather than *disagreeing* -- but "we could not
  check" is not "we checked", and a source that is silently dropped is exactly
  how a partial check passes for a full one.
* **vacuous** -- read, and there is no spend on either side. Zero agrees with
  zero perfectly and proves nothing.

Only a measured difference above the threshold is a *discrepancy*.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

from cascade.config import Settings

__all__ = [
    "LANGFUSE_SOURCE",
    "LOCAL_SOURCE",
    "RECONCILE_TOLERANCE",
    "Basis",
    "LedgerReconciliation",
    "PhaseSpend",
    "ReadingStatus",
    "SourceComparison",
    "SourceReading",
    "WrittenBy",
    "langfuse_reading",
    "local_spend",
    "readings",
    "reconcile",
    "remote_spend",
]

Role = Literal["admin", "sim", "eval"]

#: What quantity a source's record actually measures. A record that carries
#: token counts and no price cannot be compared in dollars without pricing it
#: from the meter's own table, which would compare the price table with itself
#: and agree by construction (ADR-0048).
Basis = Literal["usd", "tokens"]

#: Who wrote the record. The whole point of the reconciliation.
WrittenBy = Literal["this process", "aws"]

#: Whether a source was read, asked and missed, or never asked.
ReadingStatus = Literal["read", "unreachable", "not configured"]

LOCAL_SOURCE = "runs ledger"
LANGFUSE_SOURCE = "langfuse"

# §12.4's threshold, stated once. "A discrepancy above 2% is a bug in the
# meter", so this is a criterion rather than a tuning knob.
RECONCILE_TOLERANCE = Decimal("0.02")


@dataclass(frozen=True, slots=True)
class PhaseSpend:
    """What one source says a phase cost.

    ``total_usd`` is ``None`` for a record that does not carry a price --
    Bedrock's model-invocation logs count tokens and say nothing about money.
    ``None`` rather than ``Decimal(0)`` because a zero here would read as a
    free phase and compare as a 100% discrepancy against a real one, which
    reports a record's *shape* as the meter's error.
    """

    source: str
    total_usd: Decimal | None
    calls: int
    input_tokens: int
    output_tokens: int
    detail: str = ""

    @property
    def total_tokens(self) -> int:
        """Both directions summed, which is what a token-basis source compares."""
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True, slots=True)
class SourceReading:
    """One other record of the spend, or the reason there is not one.

    Carries ``name`` and ``status`` even when ``spend`` is absent, so an
    unreachable source can still be named in the verdict. A source that
    vanishes from the output when it cannot be read is how a partial check
    comes to look like a full one.
    """

    name: str
    written_by: WrittenBy
    basis: Basis
    spend: PhaseSpend | None = None
    status: ReadingStatus = "read"
    note: str = ""

    def __post_init__(self) -> None:
        """Refuse a reading whose status and content disagree.

        A reading marked ``read`` with nothing in it would be counted as a
        comparison that happened, and one marked ``unreachable`` while holding
        a total would be dropped from the gate while its numbers were printed.
        Both are silent, and both defeat the distinction this type exists to
        carry.
        """
        if (self.status == "read") != (self.spend is not None):
            raise ValueError(
                f"reading {self.name!r} is marked {self.status!r} but "
                f"{'carries' if self.spend is not None else 'carries no'} a total; "
                "status and content must agree"
            )
        if self.spend is not None and self.spend.source != self.name:
            raise ValueError(
                f"reading {self.name!r} carries a total labelled "
                f"{self.spend.source!r}; one source, one name"
            )

    @property
    def independent(self) -> bool:
        """True when something other than this process wrote the record."""
        return self.written_by != "this process"


@dataclass(frozen=True, slots=True)
class SourceComparison:
    """The local ledger measured against exactly one other record.

    Self-contained on purpose: a reader holding one of these can state the
    whole comparison -- which source, who wrote it, what quantity was compared,
    and whether it was compared at all -- without consulting the reconciliation
    it came from.
    """

    local: PhaseSpend
    reading: SourceReading
    tolerance: Decimal = RECONCILE_TOLERANCE

    @property
    def name(self) -> str:
        return self.reading.name

    @property
    def basis(self) -> Basis:
        return self.reading.basis

    @property
    def status(self) -> ReadingStatus:
        return self.reading.status

    @property
    def independent(self) -> bool:
        return self.reading.independent

    @property
    def compared(self) -> bool:
        """True only when a record was actually read and measured against."""
        return self.reading.status == "read" and self.reading.spend is not None

    @property
    def local_quantity(self) -> Decimal | None:
        return _quantity(self.local, self.basis)

    @property
    def source_quantity(self) -> Decimal | None:
        spend = self.reading.spend
        return None if spend is None else _quantity(spend, self.basis)

    @property
    def vacuous(self) -> bool:
        """True when the two records were read and neither holds anything.

        Zero against zero agrees to 0.0000%, and reporting that as reconciled
        is a guard that cannot fail: a study run entirely in replay, or under a
        stand-in decider, books nothing on either side and would pass §12.4's
        gate without either record having been tested against the other.
        """
        spend = self.reading.spend
        if spend is None:
            return False
        local_quantity, source_quantity = self.local_quantity, self.source_quantity
        if local_quantity is None or source_quantity is None:
            return False
        return (
            self.local.calls == 0
            and spend.calls == 0
            and local_quantity == 0
            and source_quantity == 0
        )

    @property
    def difference_usd(self) -> Decimal | None:
        """Signed dollars, or ``None`` when either record has no price."""
        spend = self.reading.spend
        if spend is None or self.local.total_usd is None or spend.total_usd is None:
            return None
        return self.local.total_usd - spend.total_usd

    @property
    def relative_difference(self) -> Decimal | None:
        """``|local - source| / max(local, source)``, or ``None``.

        Divided by the larger of the two rather than by one of them, so the
        figure does not depend on which side is called the reference -- and so
        a source total of zero against a non-zero local one reads as 100%
        rather than as a division by zero.
        """
        local_quantity, source_quantity = self.local_quantity, self.source_quantity
        if local_quantity is None or source_quantity is None:
            return None
        base = max(local_quantity, source_quantity)
        if base == 0:
            return Decimal(0)
        return abs(local_quantity - source_quantity) / base

    @property
    def within_tolerance(self) -> bool:
        relative = self.relative_difference
        return relative is not None and relative <= self.tolerance

    @property
    def agrees(self) -> bool:
        """Read, non-trivial, and inside §12.4's threshold."""
        return self.compared and not self.vacuous and self.within_tolerance


def _quantity(spend: PhaseSpend, basis: Basis) -> Decimal | None:
    """The number this basis compares, or ``None`` when the record lacks it."""
    if basis == "usd":
        return spend.total_usd
    return Decimal(spend.total_tokens)


@dataclass(frozen=True, slots=True)
class LedgerReconciliation:
    """The §12.4 gate, measured against every source that was asked.

    ``reconciled`` is conjunctive: it is true only when at least one source was
    compared, **every** source that was asked was reachable, none of the
    comparisons was vacuous, and all of them agree. Agreement with one record
    while another is unreachable is not a reconciliation, and a verdict that
    did not say which sources it compared would make the two indistinguishable.
    """

    local: PhaseSpend
    comparisons: tuple[SourceComparison, ...] = ()
    tolerance: Decimal = RECONCILE_TOLERANCE

    def __post_init__(self) -> None:
        """Refuse a set of comparisons scored against different thresholds.

        The CLI prints ``self.tolerance`` beside a verdict computed from the
        comparisons' own; if they could differ, the printed threshold would not
        be the one the gate applied.
        """
        wrong = sorted(c.name for c in self.comparisons if c.tolerance != self.tolerance)
        if wrong:
            raise ValueError(
                f"comparisons scored against a different tolerance than the "
                f"reconciliation's {self.tolerance}: {', '.join(wrong)}"
            )
        names = sorted(c.name for c in self.comparisons)
        duplicated = sorted({name for name in names if names.count(name) > 1})
        if duplicated:
            raise ValueError(f"one source, one comparison; duplicated: {', '.join(duplicated)}")

    # -- what was and was not compared ---------------------------------------

    def _names(self, *statuses: ReadingStatus) -> tuple[str, ...]:
        return tuple(sorted(c.name for c in self.comparisons if c.status in statuses))

    @property
    def attempted(self) -> tuple[str, ...]:
        """Every source this deployment asked, sorted (invariant 7)."""
        return self._names("read", "unreachable")

    @property
    def compared(self) -> tuple[str, ...]:
        """Every source actually read and measured against, sorted."""
        return self._names("read")

    @property
    def unreachable(self) -> tuple[str, ...]:
        """Asked and missed. Non-empty means the gate cannot pass."""
        return self._names("unreachable")

    @property
    def not_configured(self) -> tuple[str, ...]:
        """Never asked. Listed so a partial check is visible, not blocking."""
        return self._names("not configured")

    @property
    def independent_sources(self) -> tuple[str, ...]:
        """Compared sources that something other than this process wrote."""
        return tuple(sorted(c.name for c in self.comparisons if c.compared and c.independent))

    # -- the verdict ---------------------------------------------------------

    @property
    def remote(self) -> PhaseSpend | None:
        """Langfuse's total, or ``None``. The M8 shape, kept for its callers."""
        for comparison in self.comparisons:
            if comparison.name == LANGFUSE_SOURCE:
                return comparison.reading.spend
        return None

    @property
    def widest(self) -> SourceComparison | None:
        """The compared source that disagrees most, or ``None`` if none was read.

        The gate is conjunctive, so the figure worth reporting is the worst
        one: a mean over sources would let a silent source dilute a loud one.
        """
        measured = [c for c in self.comparisons if c.relative_difference is not None]
        if not measured:
            return None
        return max(measured, key=lambda c: (c.relative_difference or Decimal(0), c.name))

    @property
    def vacuous(self) -> bool:
        """True when something was read and nothing anywhere holds any spend."""
        read = [c for c in self.comparisons if c.compared]
        return bool(read) and all(c.vacuous for c in read)

    @property
    def difference_usd(self) -> Decimal | None:
        widest = self.widest
        return None if widest is None else widest.difference_usd

    @property
    def relative_difference(self) -> Decimal | None:
        """The widest measured difference, or ``None`` when nothing was read."""
        widest = self.widest
        return None if widest is None else widest.relative_difference

    @property
    def within_tolerance(self) -> bool:
        """True only when every source that was asked was read and agrees.

        An unreachable source makes this false rather than being skipped. The
        alternative -- scoring only what answered -- is how a source drops out
        of the gate without changing its verdict.
        """
        if not self.compared or self.unreachable:
            return False
        return all(c.within_tolerance for c in self.comparisons if c.compared)

    @property
    def reconciled(self) -> bool:
        """The §12.4 gate. See the class docstring for why it is conjunctive."""
        return bool(self.compared) and not self.vacuous and self.within_tolerance

    @property
    def independently_verified(self) -> bool:
        """``reconciled``, *and* against a record this process did not write.

        Kept separate from ``reconciled`` because §12.4's criterion names
        Langfuse, and Langfuse is written here. Folding the two together would
        either fail every deployment without AWS or quietly let corroboration
        be published as verification (ADR-0048).
        """
        if not self.reconciled:
            return False
        return any(c.agrees for c in self.comparisons if c.independent)

    @property
    def verdict(self) -> str:
        """One sentence naming what was compared, so a partial check reads as one.

        Generated for every outcome including the ones that never compared
        anything: the record of what was *not* measurable is what a blocked
        gate most needs to publish.
        """
        aside = ""
        if self.not_configured:
            aside = f"; not configured: {', '.join(self.not_configured)}"
        if not self.attempted:
            return f"no other record of this spend was asked, so nothing was reconciled{aside}"
        if self.unreachable:
            compared = ", ".join(self.compared) if self.compared else "nothing"
            return (
                f"unreconciled: {', '.join(self.unreachable)} could not be read "
                f"(compared: {compared}){aside}"
            )
        if self.vacuous:
            return (
                f"nothing to reconcile: {', '.join(self.compared)} and the local ledger "
                f"both hold no spend{aside}"
            )
        if not self.within_tolerance:
            widest = self.widest
            where = widest.name if widest is not None else "an unread source"
            return f"discrepancy above the {self.tolerance:.0%} tolerance against {where}{aside}"
        verified = (
            f"; independently verified against {', '.join(self.independent_sources)}"
            if self.independently_verified
            else "; no record written outside this process was compared, so this is "
            "corroboration rather than independent verification"
        )
        return f"reconciled against {', '.join(self.compared)}{verified}{aside}"


def _connect(settings: Settings, role: Role) -> Any:
    import psycopg

    return psycopg.connect(settings.database_url(role), connect_timeout=30)


def local_spend(
    settings: Settings, *, config_id: str | None = None, role: Role = "eval"
) -> PhaseSpend:
    """What the meter booked, summed from the run ledger.

    ``runs`` is the ledger §12.4 names: one row per completed run carrying the
    cost the meter priced for it. Summing it rather than re-pricing keeps this
    a *reconciliation* -- re-pricing here would compare the price table with
    itself and agree by construction.
    """
    clause = "WHERE config_id = %(config_id)s" if config_id else ""
    with _connect(settings, role) as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT COALESCE(sum(cost_usd), 0)::text,
                   COALESCE(sum(llm_calls), 0),
                   COALESCE(sum(tokens_in), 0),
                   COALESCE(sum(tokens_out), 0),
                   count(*)
            FROM runs {clause}
            """,  # noqa: S608 -- `clause` is a literal chosen above, not input
            {"config_id": config_id},
        )
        row = cur.fetchone()
    total, calls, tokens_in, tokens_out, runs = row if row else ("0", 0, 0, 0, 0)
    return PhaseSpend(
        source=LOCAL_SOURCE,
        total_usd=Decimal(str(total)),
        calls=int(calls),
        input_tokens=int(tokens_in),
        output_tokens=int(tokens_out),
        detail=f"{int(runs):,} run(s)" + (f", config {config_id}" if config_id else ""),
    )


def langfuse_reading(settings: Settings, *, timeout_s: float = 30.0) -> SourceReading:
    """What Langfuse recorded, or why it could not be asked.

    Distinguishes *not configured* from *unreachable*: a disabled tracer is a
    deployment choice and does not block §12.4's gate, while a tracer that was
    enabled and did not answer does. Neither is reported as a zero total -- a
    zero would compare as a 100% discrepancy and fail the gate for an
    observability outage, which inverts the rule that observability must never
    fail a run (`tracing.py`).

    Reads the daily metrics endpoint, which Langfuse v2 (ADR-0006) serves as an
    aggregate rather than requiring a walk over every observation -- 4.2M
    generations is not something to paginate for a total.

    Langfuse is written by this process, so it is marked as such: it
    corroborates the meter and cannot verify it (ADR-0048).
    """

    def unread(status: ReadingStatus, note: str) -> SourceReading:
        return SourceReading(
            name=LANGFUSE_SOURCE,
            written_by="this process",
            basis="usd",
            status=status,
            note=note,
        )

    if not settings.langfuse.enabled:
        return unread("not configured", "langfuse.enabled is false")
    if settings.langfuse_public_key is None or settings.langfuse_secret_key is None:
        return unread(
            "not configured",
            "CASCADE_LANGFUSE_PUBLIC_KEY / CASCADE_LANGFUSE_SECRET_KEY are not both set",
        )

    import httpx

    auth = (
        settings.langfuse_public_key.get_secret_value(),
        settings.langfuse_secret_key.get_secret_value(),
    )
    url = f"{settings.langfuse.host.rstrip('/')}/api/public/metrics/daily"
    total = Decimal(0)
    calls = tokens_in = tokens_out = 0
    page = 1
    try:
        with httpx.Client(timeout=timeout_s) as client:
            while True:
                response = client.get(url, auth=auth, params={"page": page, "limit": 100})
                if response.status_code != 200:
                    return unread(
                        "unreachable",
                        f"{settings.langfuse.host} answered HTTP {response.status_code}",
                    )
                payload = response.json()
                rows = payload.get("data") or []
                for day in rows:
                    total += Decimal(str(day.get("totalCost") or 0))
                    for usage in day.get("usage") or []:
                        calls += int(usage.get("countObservations") or 0)
                        tokens_in += int(usage.get("inputUsage") or 0)
                        tokens_out += int(usage.get("outputUsage") or 0)
                meta = payload.get("meta") or {}
                if page >= int(meta.get("totalPages") or 1) or not rows:
                    break
                page += 1
    except Exception as exc:  # noqa: BLE001 -- an unreachable Langfuse is not a discrepancy
        return unread("unreachable", f"{type(exc).__name__}: {exc}")

    return SourceReading(
        name=LANGFUSE_SOURCE,
        written_by="this process",
        basis="usd",
        spend=PhaseSpend(
            source=LANGFUSE_SOURCE,
            total_usd=total,
            calls=calls,
            input_tokens=tokens_in,
            output_tokens=tokens_out,
            detail=f"{page} page(s) of daily metrics",
        ),
    )


def remote_spend(settings: Settings, *, timeout_s: float = 30.0) -> PhaseSpend | None:
    """Langfuse's total alone, or ``None`` when it could not be asked.

    The M8 shape, kept for callers that predate the multi-source reading. It
    collapses *not configured* and *unreachable* into one ``None``, which is
    the distinction :func:`langfuse_reading` exists to keep -- prefer that.
    """
    return langfuse_reading(settings, timeout_s=timeout_s).spend


def readings(settings: Settings, *, timeout_s: float = 30.0) -> tuple[SourceReading, ...]:
    """Every other record of the spend, asked once each, in sorted order.

    Sorted by name so a report diffs against its predecessor for a reason, and
    so the verdict lists sources in the same order twice (invariant 7).

    Imported here rather than at module scope because `boto3` ships in the
    optional `aws` extra: a checkout without it must still run §12.4's gate
    against Langfuse instead of failing to import the module that says so.
    """
    from cascade.trace.aws_spend import invocation_log_reading

    found = [
        langfuse_reading(settings, timeout_s=timeout_s),
        invocation_log_reading(settings, timeout_s=timeout_s),
    ]
    return tuple(sorted(found, key=lambda reading: reading.name))


def reconcile(
    settings: Settings,
    *,
    config_id: str | None = None,
    tolerance: Decimal = RECONCILE_TOLERANCE,
) -> LedgerReconciliation:
    """Compare the meter against every other record of the same spend (§12.4)."""
    local = local_spend(settings, config_id=config_id)
    return LedgerReconciliation(
        local=local,
        comparisons=tuple(
            SourceComparison(local=local, reading=reading, tolerance=tolerance)
            for reading in readings(settings)
        ),
        tolerance=tolerance,
    )
