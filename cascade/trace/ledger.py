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

**And every source must be measuring the same slice.** §12.4 reconciles *a
phase*. The meter's ledger is scoped -- `local_spend` filters `runs` by
`config_id`, and now by when they completed -- while a log group holds whatever
it has held since it was created. Comparing a phase's spend against a lifetime
total is a comparison of two different quantities, and the tolerance check then
measures the difference *between the quantities* and reports it as a bug in the
meter. So the slice is a value (:class:`Scope`), it is carried on every reading
as the slice that reading actually covers, and :func:`at_scope` refuses a
reading that covers something else. A source that cannot honour the scope
cannot silently answer a wider question.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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
    "RunsFilter",
    "Scope",
    "SourceComparison",
    "SourceReading",
    "WrittenBy",
    "at_scope",
    "epoch_ms",
    "iso",
    "langfuse_reading",
    "local_detail",
    "local_spend",
    "parse_bound",
    "readings",
    "reconcile",
    "remote_spend",
    "runs_filter",
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

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MILLISECOND = timedelta(milliseconds=1)


@dataclass(frozen=True, slots=True)
class Scope:
    """The slice of spend every record in one reconciliation must describe.

    Preserves the property the tolerance check depends on and cannot itself
    verify: that both sides answer the same question. A phase's ledger against
    a log group's lifetime total differs by however much else the group holds,
    and §12.4 would read that as "a bug in the meter" -- the one diagnosis it
    is guaranteed not to be.

    The window is half-open, ``[since, until)``, which is the convention
    ``as_of`` already sets in this codebase: the promise is *strictly before*.
    Two adjacent phases therefore partition the spend instead of both claiming
    a call made exactly on the boundary.

    ``config_id`` is part of the slice because `local_spend` filters on it. No
    remote record here can be filtered by it -- a CloudWatch invocation record
    has no such field, and Langfuse's daily-metrics endpoint filters by trace
    name, user, tags and environment, none of which `llm/tracing.py` sets. That
    is not a reason to compare anyway; it is the reason `at_scope` refuses.
    """

    since: datetime | None = None
    until: datetime | None = None
    config_id: str | None = None

    def __post_init__(self) -> None:
        """Refuse a bound with no timezone, and a window nothing can fall in.

        A naive bound is read as *local* time by every conversion downstream --
        ``timestamp()`` at the CloudWatch boundary, ``isoformat()`` at the
        Langfuse one -- so the window would silently shift by the developer's
        UTC offset and land differently on another machine. Invariant 1 forbids
        exactly this for ``as_of`` ("defaults are how leakage gets in"), and
        nothing in the reasoning is specific to retrieval: a temporal bound
        with no zone is not a bound.

        An empty or inverted window is refused rather than answered, because it
        returns zero from every source, and zero against zero is the vacuous
        agreement this whole module exists to refuse.
        """
        for name, moment in (("since", self.since), ("until", self.until)):
            if moment is not None and moment.tzinfo is None:
                raise ValueError(
                    f"{name}={moment!r} is naive; pass a timezone-aware datetime. "
                    "A bound with no zone is read as local time and scopes the "
                    "reconciliation to hours that differ between machines"
                )
        if self.since is not None and self.until is not None and self.since >= self.until:
            raise ValueError(
                f"since={self.since.isoformat()} is not before until={self.until.isoformat()}; "
                "the window is half-open [since, until) and this one holds nothing"
            )

    @property
    def windowed(self) -> bool:
        """True when either bound is set, so a reader must apply one."""
        return self.since is not None or self.until is not None

    @property
    def unbounded(self) -> bool:
        """True for the whole of every record -- the M8 shape, and the default."""
        return self.since is None and self.until is None and self.config_id is None

    @property
    def window(self) -> Scope:
        """This scope with the configuration dropped.

        What a source that can filter by time and not by configuration is able
        to cover, stated as a value so it can be compared rather than described
        in a note nobody parses.
        """
        return Scope(since=self.since, until=self.until)

    def describes(self) -> str:
        """One phrase naming the slice, for a verdict that must say what it compared."""
        parts: list[str] = []
        if self.config_id is not None:
            parts.append(f"config {self.config_id}")
        if self.since is not None and self.until is not None:
            parts.append(f"{iso(self.since)} <= t < {iso(self.until)}")
        elif self.since is not None:
            parts.append(f"t >= {iso(self.since)}")
        elif self.until is not None:
            parts.append(f"t < {iso(self.until)}")
        return ", ".join(parts) if parts else "every record in full"


def iso(moment: datetime) -> str:
    """One rendering of an instant, in UTC, as the pinned Langfuse SDK writes it.

    Keeps the wire format of a window bound independent of the caller's
    timezone: the same instant expressed in two zones must reach a service as
    the same string, or two operators reconciling the same phase would send
    different requests. ``Z`` rather than ``+00:00`` is what langfuse 2.60's
    own ``serialize_datetime`` emits for UTC.
    """
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def epoch_ms(moment: datetime) -> int:
    """Milliseconds since the epoch, by integer arithmetic, refusing a naive bound.

    Computed as a `timedelta` floor-divided by a millisecond rather than
    ``int(timestamp() * 1000)``: the float form rounds a window bound by up to
    a millisecond in whichever direction the binary expansion falls, and a
    bound is the thing that decides which side of a phase boundary a call is
    counted on. The naive guard is the second of two -- :class:`Scope` refuses
    one first -- because a caller can still build a window dict by hand, and
    ``timestamp()`` would answer a naive datetime in local time rather than
    raise.
    """
    if moment.tzinfo is None:
        raise ValueError(f"window bound {moment!r} is naive; pass an aware datetime")
    return (moment - _EPOCH) // _MILLISECOND


def parse_bound(text: str, *, field: str) -> datetime:
    """An ISO-8601 window bound that states its own timezone, or a refusal.

    The boundary a command line crosses. Typer and click parse ``--since`` into
    a *naive* datetime, so accepting one here would put the machine's UTC
    offset into the gate that guards the study's cost claim, and the same
    command would reconcile a different slice in another timezone. ``Z`` and an
    explicit offset are both accepted; nothing else is, and the message names
    the field so the fix is the next thing typed.
    """
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(
            f"{field}={text!r} is not an ISO-8601 datetime "
            f"(for example 2026-09-01T00:00:00Z): {exc}"
        ) from exc
    if parsed.tzinfo is None:
        raise ValueError(
            f"{field}={text!r} states no timezone. Add 'Z' for UTC or an explicit "
            "offset -- a bound read as local time scopes the reconciliation "
            "differently on every machine"
        )
    return parsed


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

    ``covers`` is the slice this total actually describes, which is a
    *measurement by the reader*, not a restatement of what was asked for. A
    reader that ignores the window returns the default -- everything -- and
    :func:`at_scope` refuses it. The alternative is a source answering a wider
    question than the one put to it and the difference being booked against
    the meter.
    """

    name: str
    written_by: WrittenBy
    basis: Basis
    spend: PhaseSpend | None = None
    status: ReadingStatus = "read"
    note: str = ""
    covers: Scope = Scope()

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


def at_scope(reading: SourceReading, scope: Scope) -> SourceReading:
    """The reading if it covers the slice asked for, otherwise an unread one.

    The guard that makes a scope enforceable rather than advisory. A source
    that cannot filter by time, or by configuration, returns a total for a
    wider slice; comparing it would measure the width of the difference and
    §12.4 would name the meter as the cause. Refusing it is the same choice
    ADR-0048 already made for a source that could not be read at all: it is
    listed, it blocks, and the note says what was asked and what came back.

    *Unreachable* rather than a fourth status, deliberately. The gate's whole
    behaviour -- ``attempted``, ``compared``, ``within_tolerance``,
    ``reconciled``, the verdict, and the command's exit code -- is defined over
    three states, and a fourth would need a decision in each of those places,
    several of which are not in this file. "Asked, and what came back was not a
    reading of the slice asked for" is, for every one of those decisions, the
    same thing as unreachable: not an agreement, not an absence, and blocking.

    A source that was never asked keeps its status: nothing was read, so there
    is nothing that could cover the wrong slice, and promoting it to
    *unreachable* would make a deployment without an AWS account fail §12.4's
    gate the moment anyone passed ``--since``.
    """
    if reading.status != "read" or reading.covers == scope:
        return reading
    return SourceReading(
        name=reading.name,
        written_by=reading.written_by,
        basis=reading.basis,
        status="unreachable",
        note=(
            f"answered for {reading.covers.describes()}, which is not the "
            f"{scope.describes()} that was asked for, so it is not a reading of "
            "this phase's spend"
        ),
    )


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
    #: The slice every side of every comparison here describes. Carried so the
    #: verdict can state it: "reconciled" over one phase and "reconciled" over
    #: the whole ledger are different claims and must not print the same.
    scope: Scope = Scope()

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

        A bounded scope is stated first and an unbounded one says nothing, so
        the default sentence is unchanged and a narrowed one cannot be read as
        a claim about the whole ledger.
        """
        aside = ""
        if self.not_configured:
            aside = f"; not configured: {', '.join(self.not_configured)}"
        prefix = "" if self.scope.unbounded else f"for {self.scope.describes()}: "
        return prefix + self._outcome(aside)

    def _outcome(self, aside: str) -> str:
        """The verdict's sentence without its scope, so the scope is stated once."""
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


@dataclass(frozen=True, slots=True)
class RunsFilter:
    """How one :class:`Scope` reads against the `runs` table.

    Built as text and parameters so the SQL a scope produces is checkable
    without a database, which is the only way it gets checked at all: the
    window is the difference between reconciling a phase and reconciling the
    table, and the rest of this module's tests are pure.
    """

    #: Applied as `WHERE`, because a run of another configuration is not part
    #: of this reconciliation in any sense.
    where: str
    #: Applied as `FILTER`, not `WHERE`, so runs outside the window are still
    #: visible to `straddles` below.
    in_window: str
    #: A run whose interval crosses a bound: some of its calls fall inside the
    #: window and some outside, while the ledger books its whole cost at one
    #: instant. Counted rather than resolved -- see `local_spend`.
    straddles: str
    params: dict[str, Any]


def runs_filter(scope: Scope) -> RunsFilter:
    """The predicates that scope the run ledger, and the ones that admit their limit.

    Preserves the only honest account of a run-granularity ledger against a
    call-granularity record: a run is attributed to the instant its row was
    written, and a run that was in flight across a bound has calls on both
    sides of it. That is not fixable from `runs` -- there are no per-call
    timestamps in it -- so it is measured and reported, because a 2%
    disagreement with a named cause is a different object from one §12.4 calls
    a bug in the meter.

    Every value is a bound parameter. The predicates are chosen from this
    function's own literals, never built from input.
    """
    params: dict[str, Any] = {"config_id": scope.config_id}
    where = "WHERE config_id = %(config_id)s" if scope.config_id is not None else ""

    window: list[str] = []
    straddles: list[str] = []
    if scope.since is not None:
        params["since"] = scope.since
        window.append("completed_at >= %(since)s")
        straddles.append("(started_at < %(since)s AND completed_at >= %(since)s)")
    if scope.until is not None:
        params["until"] = scope.until
        window.append("completed_at < %(until)s")
        straddles.append("(started_at < %(until)s AND completed_at >= %(until)s)")

    return RunsFilter(
        where=where,
        in_window=" AND ".join(window) if window else "true",
        straddles=" OR ".join(straddles) if straddles else "false",
        params=params,
    )


def local_spend(settings: Settings, *, scope: Scope = Scope(), role: Role = "eval") -> PhaseSpend:
    """What the meter booked over one slice, summed from the run ledger.

    ``runs`` is the ledger §12.4 names: one row per completed run carrying the
    cost the meter priced for it. Summing it rather than re-pricing keeps this
    a *reconciliation* -- re-pricing here would compare the price table with
    itself and agree by construction.

    A run is in the window when ``completed_at`` is, because that is the
    instant the row -- and with it the whole run's cost -- entered the ledger;
    §12.4's own wording is "written to the runs table on completion of each
    run". The ledger has no per-call timestamps, so a run that spans a bound
    cannot be split, and the count of such runs is reported in ``detail``
    rather than silently absorbed into the discrepancy.
    """
    runs_scope = runs_filter(scope)
    with _connect(settings, role) as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT COALESCE(sum(cost_usd)   FILTER (WHERE {runs_scope.in_window}), 0)::text,
                   COALESCE(sum(llm_calls)  FILTER (WHERE {runs_scope.in_window}), 0),
                   COALESCE(sum(tokens_in)  FILTER (WHERE {runs_scope.in_window}), 0),
                   COALESCE(sum(tokens_out) FILTER (WHERE {runs_scope.in_window}), 0),
                   count(*) FILTER (WHERE {runs_scope.in_window}),
                   count(*) FILTER (WHERE {runs_scope.straddles})
            FROM runs {runs_scope.where}
            """,  # noqa: S608 -- predicates are this module's literals; values are bound
            runs_scope.params,
        )
        row = cur.fetchone()
    total, calls, tokens_in, tokens_out, runs, straddling = row if row else ("0", 0, 0, 0, 0, 0)
    return PhaseSpend(
        source=LOCAL_SOURCE,
        total_usd=Decimal(str(total)),
        calls=int(calls),
        input_tokens=int(tokens_in),
        output_tokens=int(tokens_out),
        detail=local_detail(runs=int(runs), straddling=int(straddling), scope=scope),
    )


def local_detail(*, runs: int, straddling: int, scope: Scope) -> str:
    """One line stating what was summed, including what the window could not split.

    Pure, so the sentence a straddling run produces is checkable without a
    database. It is written at all because the alternative is an operator
    reading a boundary artefact as the meter being wrong by that much.
    """
    parts = [f"{runs:,} run(s)"]
    if not scope.unbounded:
        parts.append(scope.describes())
    if straddling:
        parts.append(
            f"{straddling:,} run(s) were in flight across a window bound and are "
            "booked whole at completion, so some of their calls fall outside it"
        )
    return ", ".join(parts)


def langfuse_reading(
    settings: Settings,
    *,
    scope: Scope = Scope(),
    transport: Any | None = None,
    timeout_s: float = 30.0,
) -> SourceReading:
    """What Langfuse recorded over one slice, or why it could not be asked.

    Distinguishes *not configured* from *unreachable*: a disabled tracer is a
    deployment choice and does not block §12.4's gate, while a tracer that was
    enabled and did not answer does. Neither is reported as a zero total -- a
    zero would compare as a 100% discrepancy and fail the gate for an
    observability outage, which inverts the rule that observability must never
    fail a run (`tracing.py`).

    Reads the daily metrics endpoint, which Langfuse v2 (ADR-0006) serves as an
    aggregate rather than requiring a walk over every observation -- 4.2M
    generations is not something to paginate for a total.

    The window is applied through that endpoint's own ``fromTimestamp`` /
    ``toTimestamp`` parameters, read off the pinned SDK (langfuse 2.60.10,
    `api/resources/metrics/client.py`) rather than guessed: it documents them
    as "on or after" and "before", which is the half-open window
    :class:`Scope` defines, so no translation is needed and none is done.

    **It cannot honour ``config_id``.** The endpoint filters by trace name,
    user, tags and environment, and `llm/tracing.py` sets none of them -- the
    configuration reaches Langfuse only as trace metadata, which the aggregate
    does not filter on. So the reading declares that it covers the *window*
    and not the configuration, and `at_scope` refuses to compare it against a
    configuration-scoped ledger instead of answering a wider question quietly.

    Langfuse is written by this process, so it is marked as such: it
    corroborates the meter and cannot verify it (ADR-0048).

    ``transport`` is a test seam, the same one `invocation_log_reading` takes a
    ``client`` for and for the same reason: the two lines that decide whether
    the window reaches the service, and whether this reading admits what it
    covers, are otherwise only reachable with a live Langfuse -- which means
    they are only checked where nobody checks them. Production passes nothing.
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
    window: dict[str, str] = {}
    if scope.since is not None:
        window["fromTimestamp"] = iso(scope.since)
    if scope.until is not None:
        window["toTimestamp"] = iso(scope.until)

    total = Decimal(0)
    calls = tokens_in = tokens_out = 0
    page = 1
    try:
        with httpx.Client(timeout=timeout_s, transport=transport) as client:
            while True:
                response = client.get(url, auth=auth, params={"page": page, "limit": 100, **window})
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

    detail = f"{page} page(s) of daily metrics"
    if scope.windowed:
        detail += f", fromTimestamp/toTimestamp for {scope.window.describes()}"
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
            detail=detail,
        ),
        covers=scope.window,
    )


def remote_spend(
    settings: Settings, *, scope: Scope = Scope(), timeout_s: float = 30.0
) -> PhaseSpend | None:
    """Langfuse's total alone, or ``None`` when it could not be asked.

    The M8 shape, kept for callers that predate the multi-source reading. It
    collapses *not configured* and *unreachable* into one ``None``, which is
    the distinction :func:`langfuse_reading` exists to keep -- prefer that. It
    does not apply :func:`at_scope` either, so a caller passing a scope
    Langfuse cannot honour gets a total for a wider slice with nothing to say
    so: one more reason to prefer the reading.
    """
    return langfuse_reading(settings, scope=scope, timeout_s=timeout_s).spend


def readings(
    settings: Settings,
    *,
    scope: Scope = Scope(),
    transport: Any | None = None,
    timeout_s: float = 30.0,
) -> tuple[SourceReading, ...]:
    """Every other record of the spend, asked once each, at one scope, sorted.

    The one place :func:`at_scope` is applied, so every reading a comparison is
    built from has been checked against the slice that was asked for. A reader
    added later that quietly ignores the window is refused here by default
    rather than on remembering to be refused, which is the difference between
    an invariant and a convention.

    Sorted by name so a report diffs against its predecessor for a reason, and
    so the verdict lists sources in the same order twice (invariant 7).

    Imported here rather than at module scope because `boto3` ships in the
    optional `aws` extra: a checkout without it must still run §12.4's gate
    against Langfuse instead of failing to import the module that says so.
    """
    from cascade.trace.aws_spend import invocation_log_reading

    found = [
        langfuse_reading(settings, scope=scope, transport=transport, timeout_s=timeout_s),
        invocation_log_reading(settings, scope=scope, timeout_s=timeout_s),
    ]
    return tuple(sorted((at_scope(reading, scope) for reading in found), key=lambda r: r.name))


def reconcile(
    settings: Settings,
    *,
    config_id: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    tolerance: Decimal = RECONCILE_TOLERANCE,
) -> LedgerReconciliation:
    """Compare the meter against every other record of the *same slice* of spend (§12.4).

    ``since`` and ``until`` are timezone-aware and the window is half-open,
    ``[since, until)``; a naive bound raises rather than being read in the
    machine's own zone. With none of the three given this is M8's whole-ledger
    reconciliation, unchanged.

    The slice is applied to both sides or the comparison does not happen. A
    source that cannot narrow to it -- Langfuse to a configuration, a log group
    to either -- is reported unreachable with the reason, which blocks. The
    alternative is what this call did before: scoping the ledger to one
    configuration, leaving every other record at its lifetime total, and
    reporting the difference between two different quantities as a discrepancy
    in the meter.
    """
    scope = Scope(since=since, until=until, config_id=config_id)
    local = local_spend(settings, scope=scope)
    return LedgerReconciliation(
        local=local,
        comparisons=tuple(
            SourceComparison(local=local, reading=reading, tolerance=tolerance)
            for reading in readings(settings, scope=scope)
        ),
        tolerance=tolerance,
        scope=scope,
    )
