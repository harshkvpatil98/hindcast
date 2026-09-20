"""The §12.4 cost-ledger gate, against N records of the same spend (M8, M15).

The meter's run ledger must agree with every other record of what a phase cost.
The arithmetic is pure and is tested here; reading any of the sources needs a
service and lives in the integration suite.

The cases that matter are the ones that look like a pass:

* **zero against zero** agrees perfectly, and a gate that accepts it is a gate
  that cannot fail. This is the defect M8 found over 452,328 model calls.
* **one source agreeing while another was never read.** Adding a second record
  makes this newly possible, and it is the same failure wearing a better suit:
  the output looks like a reconciliation and covers less than it appears to.
  Every verdict here therefore names which sources were compared.
* **corroboration published as verification.** Langfuse is written by this
  process, so agreeing with it does not test whether the calls happened. That
  distinction is `independently_verified`, kept apart from `reconciled` on
  purpose (ADR-0048).
* **two different quantities compared and the difference blamed on the meter.**
  The ledger is scoped -- to a configuration, and to a window -- and a log
  group is not. Nothing about the arithmetic notices: it returns a percentage
  either way, and §12.4 reads a percentage above 2% as "a bug in the meter",
  which is the one cause it cannot be. So a reading carries the slice it
  actually covers and `at_scope` refuses one that covers something else.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest
from pydantic import SecretStr

from cascade.config import Settings
from cascade.trace.ledger import (
    LANGFUSE_SOURCE,
    LOCAL_SOURCE,
    RECONCILE_TOLERANCE,
    Basis,
    LedgerReconciliation,
    PhaseSpend,
    ReadingStatus,
    Scope,
    SourceComparison,
    SourceReading,
    WrittenBy,
    at_scope,
    iso,
    langfuse_reading,
    local_detail,
    parse_bound,
    runs_filter,
)

AWS_SOURCE = "bedrock invocation logs"

# 100 calls at the shape `spend()` builds: 45,000 in + 4,500 out.
TOKENS_PER_CALL = 495


def spend(source: str, usd: str | None, calls: int = 100) -> PhaseSpend:
    return PhaseSpend(
        source=source,
        total_usd=None if usd is None else Decimal(usd),
        calls=calls,
        input_tokens=calls * 450,
        output_tokens=calls * 45,
    )


def tokens(source: str, *, calls: int, input_tokens: int, output_tokens: int) -> PhaseSpend:
    """A record that counts tokens and states no price, as AWS's logs do."""
    return PhaseSpend(
        source=source,
        total_usd=None,
        calls=calls,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


def reading(
    name: str,
    usd: str | None,
    *,
    calls: int = 100,
    written_by: WrittenBy = "this process",
    basis: Basis = "usd",
    covers: Scope = Scope(),
) -> SourceReading:
    return SourceReading(
        name=name,
        written_by=written_by,
        basis=basis,
        spend=spend(name, usd, calls),
        covers=covers,
    )


SINCE = datetime(2026, 9, 1, tzinfo=UTC)
UNTIL = datetime(2026, 9, 2, tzinfo=UTC)
WINDOW = Scope(since=SINCE, until=UNTIL)
PHASE = Scope(since=SINCE, until=UNTIL, config_id="C01")


def unread(
    name: str,
    status: ReadingStatus,
    *,
    written_by: WrittenBy = "aws",
    basis: Basis = "tokens",
) -> SourceReading:
    return SourceReading(
        name=name, written_by=written_by, basis=basis, status=status, note="in a test"
    )


def against(
    local: PhaseSpend, *readings: SourceReading, tolerance: Decimal = RECONCILE_TOLERANCE
) -> LedgerReconciliation:
    return LedgerReconciliation(
        local=local,
        comparisons=tuple(
            SourceComparison(local=local, reading=r, tolerance=tolerance) for r in readings
        ),
        tolerance=tolerance,
    )


def pair(local: str | None, remote: str | None, *, calls: int = 100) -> LedgerReconciliation:
    """The M8 shape: the ledger against Langfuse alone, nothing else asked."""
    local_spend = spend(LOCAL_SOURCE, local, calls)
    if remote is None:
        return against(
            local_spend,
            unread(LANGFUSE_SOURCE, "unreachable", written_by="this process", basis="usd"),
        )
    return against(local_spend, reading(LANGFUSE_SOURCE, remote, calls=calls))


class TestAgreement:
    def test_identical_totals_reconcile(self) -> None:
        assert pair("1.234567", "1.234567").reconciled

    def test_a_difference_inside_the_tolerance_reconciles(self) -> None:
        assert pair("100.00", "99.00").reconciled  # 1%

    def test_a_difference_above_the_tolerance_does_not(self) -> None:
        result = pair("100.00", "95.00")  # 5%
        assert not result.reconciled
        assert result.relative_difference == pytest.approx(Decimal("0.05"))

    def test_the_boundary_is_inclusive(self) -> None:
        """'above 2%' fails; exactly 2% does not."""
        result = pair("100.00", "98.00")
        assert result.relative_difference == pytest.approx(Decimal("0.02"))
        assert result.reconciled

    def test_the_tolerance_is_the_spec_value(self) -> None:
        assert str(RECONCILE_TOLERANCE) == "0.02"

    def test_the_direction_of_the_difference_does_not_change_the_verdict(self) -> None:
        """Divided by the larger side, so the figure does not depend on which
        record is called the reference."""
        assert (
            pair("100.00", "95.00").relative_difference
            == pair("95.00", "100.00").relative_difference
        )


class TestVacuousComparison:
    def test_zero_against_zero_is_not_a_pass(self) -> None:
        """The failure this exists to prevent: a study run entirely in replay,
        or under a stand-in decider, books nothing on either side. Zero agrees
        with zero to 0.0000% and would pass a gate guarding the cost claim."""
        result = pair("0", "0", calls=0)
        assert result.vacuous
        assert not result.reconciled
        assert result.within_tolerance  # the arithmetic agrees; the gate still refuses

    def test_zero_spend_with_real_calls_is_not_vacuous(self) -> None:
        """A fully cached phase legitimately costs nothing and did make calls."""
        result = pair("0", "0", calls=5_000)
        assert not result.vacuous
        assert result.reconciled

    def test_a_non_zero_total_is_never_vacuous(self) -> None:
        assert not pair("0.01", "0.01", calls=0).vacuous

    def test_one_live_source_keeps_the_whole_thing_non_vacuous(self) -> None:
        """Vacuity is a claim about all of the evidence, not about one piece of
        it: a source with something in it means there was something to check."""
        local = spend(LOCAL_SOURCE, "0", calls=0)
        result = against(
            local,
            reading(LANGFUSE_SOURCE, "0", calls=0),
            SourceReading(
                name=AWS_SOURCE,
                written_by="aws",
                basis="tokens",
                spend=tokens(AWS_SOURCE, calls=7, input_tokens=10, output_tokens=1),
            ),
        )
        assert not result.vacuous


class TestUnreachableLangfuse:
    def test_a_missing_remote_total_is_not_reconciled(self) -> None:
        """Observability must never fail a run (`tracing.py`), so an outage is
        reported as unreconciled rather than as a discrepancy -- but "we could
        not check" is not "we checked"."""
        result = pair("12.34", None)
        assert result.remote is None
        assert not result.reconciled
        assert result.relative_difference is None
        assert result.difference_usd is None

    def test_it_is_distinguishable_from_a_disagreement(self) -> None:
        unreachable = pair("12.34", None)
        disagreeing = pair("12.34", "6.00")
        assert unreachable.relative_difference is None
        assert disagreeing.relative_difference is not None


class TestArithmetic:
    def test_the_difference_is_signed(self) -> None:
        assert pair("10.00", "8.00").difference_usd == Decimal("2.00")
        assert pair("8.00", "10.00").difference_usd == Decimal("-2.00")

    def test_a_zero_remote_against_a_real_local_reads_as_total_disagreement(self) -> None:
        """Not a division by zero: divided by the larger side, this is 100%."""
        result = pair("10.00", "0", calls=100)
        assert result.relative_difference == Decimal(1)
        assert not result.reconciled

    def test_totals_stay_exact(self) -> None:
        """The meter prices in exact Decimal; the reconciliation must not
        round the comparison through a float."""
        result = pair("0.000001", "0.000001")
        assert result.difference_usd == Decimal("0")
        assert isinstance(result.local.total_usd, Decimal)


class TestWhichSourcesWereCompared:
    """A verdict that cannot say what it compared is a gate that cannot fail."""

    def test_the_three_states_are_reported_separately(self) -> None:
        local = spend(LOCAL_SOURCE, "10.00")
        result = against(
            local,
            reading(LANGFUSE_SOURCE, "10.00"),
            unread(AWS_SOURCE, "unreachable"),
            unread("some future source", "not configured"),
        )
        assert result.compared == (LANGFUSE_SOURCE,)
        assert result.unreachable == (AWS_SOURCE,)
        assert result.not_configured == ("some future source",)
        assert result.attempted == (AWS_SOURCE, LANGFUSE_SOURCE)  # sorted, invariant 7

    def test_the_verdict_names_the_unreachable_source(self) -> None:
        local = spend(LOCAL_SOURCE, "10.00")
        result = against(
            local, reading(LANGFUSE_SOURCE, "10.00"), unread(AWS_SOURCE, "unreachable")
        )
        assert AWS_SOURCE in result.verdict
        assert "unreconciled" in result.verdict

    def test_the_verdict_names_a_source_that_was_never_asked(self) -> None:
        """A source that vanishes when unconfigured is how a partial check comes
        to look like a full one."""
        local = spend(LOCAL_SOURCE, "10.00")
        result = against(
            local, reading(LANGFUSE_SOURCE, "10.00"), unread(AWS_SOURCE, "not configured")
        )
        assert result.reconciled
        assert AWS_SOURCE in result.verdict
        assert "not configured" in result.verdict

    def test_asking_nothing_reconciles_nothing(self) -> None:
        result = against(spend(LOCAL_SOURCE, "10.00"))
        assert not result.reconciled
        assert not result.within_tolerance
        assert "nothing was reconciled" in result.verdict


class TestASourceCannotBeSilentlySkipped:
    """Adding a second record must not weaken the first gate."""

    def test_agreement_with_one_source_is_not_a_reconciliation_when_another_is_unreachable(
        self,
    ) -> None:
        local = spend(LOCAL_SOURCE, "10.00")
        result = against(
            local, reading(LANGFUSE_SOURCE, "10.00"), unread(AWS_SOURCE, "unreachable")
        )
        assert not result.reconciled
        assert not result.within_tolerance

    def test_an_unconfigured_source_does_not_block(self) -> None:
        """A deployment with no AWS account still has §12.4's gate; making it
        unpassable would be relaxing a criterion in the other direction."""
        local = spend(LOCAL_SOURCE, "10.00")
        result = against(
            local, reading(LANGFUSE_SOURCE, "10.00"), unread(AWS_SOURCE, "not configured")
        )
        assert result.reconciled

    def test_one_disagreeing_source_fails_the_whole_gate(self) -> None:
        local = spend(LOCAL_SOURCE, "10.00")
        result = against(
            local,
            reading(LANGFUSE_SOURCE, "10.00"),
            SourceReading(
                name=AWS_SOURCE,
                written_by="aws",
                basis="tokens",
                spend=tokens(AWS_SOURCE, calls=100, input_tokens=1, output_tokens=1),
            ),
        )
        assert not result.reconciled
        assert result.widest is not None
        assert result.widest.name == AWS_SOURCE

    def test_the_reported_difference_is_the_widest_not_the_average(self) -> None:
        """A mean over sources would let a silent source dilute a loud one."""
        local = spend(LOCAL_SOURCE, "100.00")
        result = against(
            local,
            reading(LANGFUSE_SOURCE, "100.00"),
            SourceReading(
                name="another usd record",
                written_by="aws",
                basis="usd",
                spend=spend("another usd record", "50.00"),
            ),
        )
        assert result.relative_difference == Decimal("0.5")


class TestIndependence:
    """Independence is a property of who wrote the record (ADR-0048)."""

    def test_langfuse_alone_is_corroboration_not_verification(self) -> None:
        result = pair("10.00", "10.00")
        assert result.reconciled
        assert not result.independently_verified
        assert "corroboration" in result.verdict

    def test_a_record_written_by_aws_verifies(self) -> None:
        local = spend(LOCAL_SOURCE, "10.00")
        result = against(
            local,
            reading(LANGFUSE_SOURCE, "10.00"),
            SourceReading(
                name=AWS_SOURCE,
                written_by="aws",
                basis="tokens",
                spend=tokens(
                    AWS_SOURCE,
                    calls=100,
                    input_tokens=100 * 450,
                    output_tokens=100 * 45,
                ),
            ),
        )
        assert result.reconciled
        assert result.independently_verified
        assert result.independent_sources == (AWS_SOURCE,)
        assert "independently verified" in result.verdict

    def test_independent_verification_requires_reconciliation_first(self) -> None:
        """An independent source that agrees while Langfuse is unreachable is
        still not a reconciliation."""
        local = spend(LOCAL_SOURCE, "10.00")
        result = against(
            local,
            unread(LANGFUSE_SOURCE, "unreachable", written_by="this process", basis="usd"),
            SourceReading(
                name=AWS_SOURCE,
                written_by="aws",
                basis="tokens",
                spend=tokens(AWS_SOURCE, calls=100, input_tokens=100 * 450, output_tokens=100 * 45),
            ),
        )
        assert not result.reconciled
        assert not result.independently_verified


class TestTokenBasis:
    """A record that counts tokens is compared on tokens, never on a price this
    process derived from its own table."""

    def test_matching_token_counts_agree_without_any_dollar_figure(self) -> None:
        local = spend(LOCAL_SOURCE, "10.00", calls=100)
        comparison = SourceComparison(
            local=local,
            reading=SourceReading(
                name=AWS_SOURCE,
                written_by="aws",
                basis="tokens",
                spend=tokens(AWS_SOURCE, calls=100, input_tokens=100 * 450, output_tokens=100 * 45),
            ),
        )
        assert comparison.source_quantity == Decimal(100 * TOKENS_PER_CALL)
        assert comparison.relative_difference == Decimal(0)
        assert comparison.agrees
        assert comparison.difference_usd is None  # the record states no price

    def test_a_token_discrepancy_is_measured_on_tokens(self) -> None:
        local = spend(LOCAL_SOURCE, "10.00", calls=100)  # 49,500 tokens
        comparison = SourceComparison(
            local=local,
            reading=SourceReading(
                name=AWS_SOURCE,
                written_by="aws",
                basis="tokens",
                spend=tokens(AWS_SOURCE, calls=100, input_tokens=45_000, output_tokens=0),
            ),
        )
        # 49,500 vs 45,000: the missing output tokens are 9.09% of the larger.
        assert comparison.relative_difference == pytest.approx(Decimal("0.0909"), abs=1e-4)
        assert not comparison.agrees

    def test_a_usd_basis_source_with_no_price_cannot_be_compared(self) -> None:
        """Rather than silently falling back to tokens: the basis states what
        the record measures, and a record that does not carry it is a defect to
        surface, not a comparison to improvise."""
        local = spend(LOCAL_SOURCE, "10.00")
        comparison = SourceComparison(
            local=local,
            reading=SourceReading(
                name="broken",
                written_by="aws",
                basis="usd",
                spend=spend("broken", None),
            ),
        )
        assert comparison.relative_difference is None
        assert not comparison.within_tolerance
        assert not comparison.agrees

    def test_an_empty_independent_log_against_a_real_ledger_is_a_disagreement(self) -> None:
        """AWS answering "no invocations" for a phase the ledger says made
        thousands is the loudest thing this gate can find, and it must never
        read as agreement."""
        local = spend(LOCAL_SOURCE, "10.00", calls=5_000)
        result = against(
            local,
            SourceReading(
                name=AWS_SOURCE,
                written_by="aws",
                basis="tokens",
                spend=tokens(AWS_SOURCE, calls=0, input_tokens=0, output_tokens=0),
            ),
        )
        assert result.relative_difference == Decimal(1)
        assert not result.reconciled
        assert not result.vacuous


class TestAReadingCannotMisrepresentItself:
    """Guards, checked against synthetic violations."""

    def test_a_reading_marked_read_must_carry_a_total(self) -> None:
        with pytest.raises(ValueError, match="status and content must agree"):
            SourceReading(name=AWS_SOURCE, written_by="aws", basis="tokens", status="read")

    def test_an_unreachable_reading_must_not_carry_one(self) -> None:
        """It would be excluded from the gate while its numbers were printed."""
        with pytest.raises(ValueError, match="status and content must agree"):
            SourceReading(
                name=AWS_SOURCE,
                written_by="aws",
                basis="tokens",
                status="unreachable",
                spend=tokens(AWS_SOURCE, calls=1, input_tokens=1, output_tokens=1),
            )

    def test_a_total_labelled_with_another_source_is_refused(self) -> None:
        """One source, one name -- otherwise the verdict names one record and
        the table prints another."""
        with pytest.raises(ValueError, match="one source, one name"):
            SourceReading(
                name=AWS_SOURCE,
                written_by="aws",
                basis="tokens",
                spend=tokens(LANGFUSE_SOURCE, calls=1, input_tokens=1, output_tokens=1),
            )

    def test_comparisons_scored_against_another_tolerance_are_refused(self) -> None:
        """The CLI prints the reconciliation's tolerance beside a verdict
        computed from the comparisons' own."""
        local = spend(LOCAL_SOURCE, "10.00")
        with pytest.raises(ValueError, match="different tolerance"):
            LedgerReconciliation(
                local=local,
                comparisons=(
                    SourceComparison(
                        local=local,
                        reading=reading(LANGFUSE_SOURCE, "10.00"),
                        tolerance=Decimal("0.5"),
                    ),
                ),
                tolerance=RECONCILE_TOLERANCE,
            )

    def test_one_source_cannot_be_compared_twice(self) -> None:
        """Two readings of the same name would let a passing copy of a source
        sit beside a failing one, and `compared` would name it once."""
        local = spend(LOCAL_SOURCE, "10.00")
        with pytest.raises(ValueError, match="duplicated"):
            against(local, reading(LANGFUSE_SOURCE, "10.00"), reading(LANGFUSE_SOURCE, "1.00"))


class TestTheM8ShapeStillWorks:
    """`cascade trace cost` reads `remote` and the tolerance off this object."""

    def test_remote_is_langfuse(self) -> None:
        result = pair("10.00", "9.99")
        assert result.remote is not None
        assert result.remote.source == LANGFUSE_SOURCE

    def test_remote_ignores_other_sources(self) -> None:
        local = spend(LOCAL_SOURCE, "10.00")
        result = against(
            local,
            SourceReading(
                name=AWS_SOURCE,
                written_by="aws",
                basis="tokens",
                spend=tokens(AWS_SOURCE, calls=1, input_tokens=1, output_tokens=1),
            ),
        )
        assert result.remote is None


class TestTheWindowIsAValueThatRefusesAmbiguity:
    """Invariant 1's reasoning, applied to a window bound rather than to `as_of`.

    A bound with no timezone is not a bound: it means a different instant on
    every machine, so the same command would reconcile a different slice of the
    same phase depending on who ran it.
    """

    @pytest.mark.parametrize("field", ["since", "until"])
    def test_a_naive_bound_is_refused_on_either_end(self, field: str) -> None:
        with pytest.raises(ValueError, match="naive"):
            Scope(**{field: datetime(2026, 9, 1)})

    def test_the_same_instant_in_another_zone_is_the_same_scope(self) -> None:
        """Refusing naive is not refusing non-UTC. Two operators in different
        places must be able to name the same phase boundary."""
        tokyo = datetime(2026, 9, 1, 9, tzinfo=timezone(timedelta(hours=9)))
        assert Scope(since=tokyo) == Scope(since=SINCE)

    def test_an_inverted_window_is_refused_rather_than_answered(self) -> None:
        """It would return zero from every source, and zero against zero is the
        vacuous agreement this module exists to refuse -- reported, in that
        case, as though the phase had genuinely cost nothing."""
        with pytest.raises(ValueError, match="half-open"):
            Scope(since=UNTIL, until=SINCE)

    def test_an_empty_window_is_refused_for_the_same_reason(self) -> None:
        with pytest.raises(ValueError, match="half-open"):
            Scope(since=SINCE, until=SINCE)

    def test_an_unbounded_scope_is_the_whole_of_every_record(self) -> None:
        """The M8 shape, and the default: nothing about it may change."""
        assert Scope().unbounded
        assert not Scope().windowed
        assert not PHASE.unbounded

    def test_a_configuration_alone_is_bounded_but_not_windowed(self) -> None:
        """The distinction a source that can filter by time and not by
        configuration depends on."""
        scope = Scope(config_id="C01")
        assert not scope.unbounded
        assert not scope.windowed
        assert scope.window == Scope()

    def test_the_window_property_drops_only_the_configuration(self) -> None:
        assert PHASE.window == WINDOW

    @pytest.mark.parametrize(
        "scope,expected",
        [
            (Scope(), "every record in full"),
            (Scope(config_id="C01"), "config C01"),
            (Scope(since=SINCE), "t >= 2026-09-01T00:00:00Z"),
            (Scope(until=UNTIL), "t < 2026-09-02T00:00:00Z"),
            (WINDOW, "2026-09-01T00:00:00Z <= t < 2026-09-02T00:00:00Z"),
            (PHASE, "config C01, 2026-09-01T00:00:00Z <= t < 2026-09-02T00:00:00Z"),
        ],
    )
    def test_the_slice_describes_itself_for_the_verdict(self, scope: Scope, expected: str) -> None:
        assert scope.describes() == expected

    def test_a_bound_is_rendered_in_utc_whatever_zone_it_was_given_in(self) -> None:
        """Two requests for the same instant must reach a service as the same
        string, or the record read back depends on where the operator sat."""
        tokyo = datetime(2026, 9, 1, 9, tzinfo=timezone(timedelta(hours=9)))
        assert iso(tokyo) == iso(SINCE) == "2026-09-01T00:00:00Z"


class TestParsingABoundFromACommandLine:
    """The boundary a `--since` crosses. Typer and click parse a datetime option
    into a *naive* one, so this is where the machine's offset would get in."""

    def test_an_explicit_utc_bound_is_accepted(self) -> None:
        assert parse_bound("2026-09-01T00:00:00Z", field="--since") == SINCE

    def test_an_explicit_offset_is_accepted_and_is_the_same_instant(self) -> None:
        assert parse_bound("2026-09-01T09:00:00+09:00", field="--since") == SINCE

    def test_a_bound_with_no_timezone_is_refused_and_says_how_to_fix_it(self) -> None:
        with pytest.raises(ValueError, match="states no timezone"):
            parse_bound("2026-09-01T00:00:00", field="--since")

    def test_a_bare_date_is_refused_rather_than_read_as_local_midnight(self) -> None:
        """The most likely thing an operator types, and the one most likely to
        shift a phase boundary by hours without anyone noticing."""
        with pytest.raises(ValueError, match="states no timezone"):
            parse_bound("2026-09-01", field="--since")

    def test_something_that_is_not_a_datetime_names_the_field(self) -> None:
        with pytest.raises(ValueError, match=r"--until"):
            parse_bound("last tuesday", field="--until")


class TestASourceCannotSilentlyAnswerAWiderQuestion:
    """`at_scope`, checked against synthetic violations in both directions."""

    def test_a_reading_that_covers_the_slice_asked_for_is_unchanged(self) -> None:
        asked = reading(LANGFUSE_SOURCE, "10.00", covers=WINDOW)
        assert at_scope(asked, WINDOW) is asked

    def test_a_reader_that_ignored_the_window_is_refused(self) -> None:
        """The failure this guard exists for: a source answering for its whole
        lifetime while the ledger answers for one phase. The arithmetic would
        return a large percentage and §12.4 would call it a bug in the meter."""
        ignored = at_scope(reading(LANGFUSE_SOURCE, "10.00"), WINDOW)
        assert ignored.status == "unreachable"
        assert ignored.spend is None
        assert "every record in full" in ignored.note
        assert "2026-09-01T00:00:00Z <= t < 2026-09-02T00:00:00Z" in ignored.note

    def test_a_reader_that_narrowed_further_than_asked_is_also_refused(self) -> None:
        """Not only the wider direction. A reading of *less* than the slice
        understates the independent record, which is the direction that makes
        the meter look like it over-booked."""
        narrow = at_scope(reading(LANGFUSE_SOURCE, "10.00", covers=WINDOW), Scope(since=SINCE))
        assert narrow.status == "unreachable"

    def test_a_window_honoured_but_a_configuration_not_is_refused(self) -> None:
        """No remote record here can be filtered by `config_id`: an invocation
        log has no such field, and Langfuse's daily metrics filter by trace
        name, user, tags and environment, none of which the tracer sets. So
        `--config-id` refuses rather than comparing one cell against all of
        them -- which is what this call did before the scope existed."""
        refused = at_scope(reading(LANGFUSE_SOURCE, "10.00", covers=WINDOW), PHASE)
        assert refused.status == "unreachable"
        assert "config C01" in refused.note

    def test_an_unread_source_keeps_its_status(self) -> None:
        """Nothing was read, so nothing can cover the wrong slice. Promoting
        *not configured* to *unreachable* would make every deployment without an
        AWS account fail §12.4's gate the moment anyone passed `--since`."""
        never_asked = unread(AWS_SOURCE, "not configured")
        assert at_scope(never_asked, WINDOW) is never_asked
        outage = unread(AWS_SOURCE, "unreachable")
        assert at_scope(outage, WINDOW) is outage

    def test_the_refusal_blocks_the_gate_rather_than_dropping_the_source(self) -> None:
        """The whole point of reusing *unreachable*: it is listed, it blocks,
        and the verdict names it -- exactly as an outage does."""
        local = spend(LOCAL_SOURCE, "10.00")
        result = LedgerReconciliation(
            local=local,
            comparisons=(
                SourceComparison(
                    local=local,
                    reading=at_scope(reading(LANGFUSE_SOURCE, "10.00"), WINDOW),
                ),
            ),
            scope=WINDOW,
        )
        assert not result.reconciled
        assert result.unreachable == (LANGFUSE_SOURCE,)
        assert "unreconciled" in result.verdict

    def test_without_the_guard_the_mismatch_would_have_read_as_agreement(self) -> None:
        """Guard the guard. The same two readings compared without `at_scope`
        reconcile perfectly, which is the state this module shipped in: one
        source measuring a phase, another measuring everything, and a green
        verdict over the pair."""
        local = spend(LOCAL_SOURCE, "10.00")
        unguarded = LedgerReconciliation(
            local=local,
            comparisons=(SourceComparison(local=local, reading=reading(LANGFUSE_SOURCE, "10.00")),),
            scope=WINDOW,
        )
        assert unguarded.reconciled


class TestTheLedgerSideOfTheWindow:
    """`runs_filter` and `local_detail`: the SQL a scope produces, checked
    without a database, because the database is where it would not be checked."""

    def test_an_unbounded_scope_produces_the_m8_query(self) -> None:
        """No window clause, no configuration clause: the predicates degenerate
        to constants a planner drops, so the default path is what it was."""
        built = runs_filter(Scope())
        assert built.where == ""
        assert built.in_window == "true"
        assert built.straddles == "false"

    def test_a_configuration_is_filtered_in_where_not_in_filter(self) -> None:
        """A run of another cell is not part of this reconciliation in any
        sense, so it is excluded from the straddle count too."""
        built = runs_filter(Scope(config_id="C01"))
        assert built.where == "WHERE config_id = %(config_id)s"
        assert built.params["config_id"] == "C01"

    def test_the_window_is_applied_to_completion_time(self) -> None:
        """§12.4: "written to the runs table on completion of each run". That
        instant is when the whole run's cost entered the ledger."""
        built = runs_filter(WINDOW)
        assert built.in_window == "completed_at >= %(since)s AND completed_at < %(until)s"
        assert built.params["since"] == SINCE
        assert built.params["until"] == UNTIL

    def test_the_upper_bound_is_exclusive_so_two_phases_partition_the_spend(self) -> None:
        assert "completed_at < %(until)s" in runs_filter(Scope(until=UNTIL)).in_window
        assert ">= %(until)s" not in runs_filter(Scope(until=UNTIL)).in_window

    def test_one_bound_alone_produces_one_predicate(self) -> None:
        assert runs_filter(Scope(since=SINCE)).in_window == "completed_at >= %(since)s"

    def test_a_run_in_flight_across_either_bound_is_counted(self) -> None:
        """The honest limit of a run-granularity ledger against a
        call-granularity record. It cannot be resolved from `runs` -- there are
        no per-call timestamps -- so it is measured, because a 2% disagreement
        with a named cause is a different object from one §12.4 calls a bug in
        the meter."""
        built = runs_filter(WINDOW)
        assert built.straddles == (
            "(started_at < %(since)s AND completed_at >= %(since)s)"
            " OR (started_at < %(until)s AND completed_at >= %(until)s)"
        )

    def test_a_straddling_run_is_inside_the_window_and_counted_as_straddling(self) -> None:
        """Hand-checked against one run: started 2026-08-31, completed
        2026-09-01T06:00Z. It is *in* the window (it completed inside it) and it
        also straddles (it started before it), which is the case the count
        exists to surface rather than a contradiction between the predicates."""
        started = datetime(2026, 8, 31, tzinfo=UTC)
        completed = datetime(2026, 9, 1, 6, tzinfo=UTC)
        assert SINCE <= completed < UNTIL  # in the window
        assert started < SINCE <= completed  # and straddling its lower bound

    def test_no_bound_is_ever_interpolated_into_the_sql(self) -> None:
        """Every value is bound. S608 is suppressed on the query because the
        predicates are this module's own literals, and that suppression is only
        honest while no datum reaches the text."""
        built = runs_filter(PHASE)
        for clause in (built.where, built.in_window, built.straddles):
            assert "2026" not in clause
            assert "C01" not in clause

    def test_the_detail_line_states_what_was_summed(self) -> None:
        assert local_detail(runs=3, straddling=0, scope=Scope()) == "3 run(s)"

    def test_the_detail_line_names_the_slice_when_there_is_one(self) -> None:
        line = local_detail(runs=3, straddling=0, scope=PHASE)
        assert line.startswith("3 run(s), config C01, 2026-09-01T00:00:00Z <= t <")

    def test_the_detail_line_reports_straddling_runs_rather_than_absorbing_them(self) -> None:
        """An operator reading a boundary artefact as the meter being wrong by
        that much is the failure; naming it is the whole fix available here."""
        line = local_detail(runs=3, straddling=2, scope=WINDOW)
        assert "2 run(s) were in flight across a window bound" in line

    def test_no_straddling_runs_says_nothing_about_them(self) -> None:
        assert "in flight" not in local_detail(runs=3, straddling=0, scope=WINDOW)


class TestTheVerdictStatesTheSliceItCompared:
    """ "Reconciled" over one phase and "reconciled" over the whole ledger are
    different claims, and a verdict that prints the same for both is a gate a
    reader cannot check."""

    def test_an_unbounded_reconciliation_reads_exactly_as_it_did_at_m8(self) -> None:
        """The default output is unchanged: a scope nobody asked for must not
        add words to the demo path's report."""
        assert pair("10.00", "10.00").verdict.startswith("reconciled against langfuse")

    def test_a_scoped_reconciliation_names_its_slice_first(self) -> None:
        local = spend(LOCAL_SOURCE, "10.00")
        result = LedgerReconciliation(
            local=local,
            comparisons=(
                SourceComparison(
                    local=local, reading=reading(LANGFUSE_SOURCE, "10.00", covers=WINDOW)
                ),
            ),
            scope=WINDOW,
        )
        assert result.reconciled
        assert result.verdict.startswith(
            "for 2026-09-01T00:00:00Z <= t < 2026-09-02T00:00:00Z: reconciled against langfuse"
        )

    def test_the_scope_is_stated_on_a_refusal_too(self) -> None:
        """A blocked gate is the one that most needs to say what it was asking
        about -- the slice is usually the thing that was wrong."""
        local = spend(LOCAL_SOURCE, "10.00")
        result = LedgerReconciliation(
            local=local,
            comparisons=(
                SourceComparison(
                    local=local, reading=at_scope(reading(LANGFUSE_SOURCE, "10.00"), PHASE)
                ),
            ),
            scope=PHASE,
        )
        assert result.verdict.startswith("for config C01, ")
        assert "unreconciled" in result.verdict


def langfuse_settings(settings: Settings) -> Settings:
    """Enabled, with both keys, so `langfuse_reading` gets as far as the wire."""
    return settings.model_copy(
        update={
            "langfuse": settings.langfuse.model_copy(
                update={"enabled": True, "host": "http://langfuse.invalid"}
            ),
            "langfuse_public_key": SecretStr("pk-test"),
            "langfuse_secret_key": SecretStr("sk-test"),
        }
    )


def daily_metrics(requests: list[httpx.Request]) -> httpx.MockTransport:
    """One page of daily metrics, recording what was asked for.

    $10.00 over 100 observations, which is the shape the helpers above build,
    so a reading off this transport compares against `spend()` by construction
    rather than by coincidence.
    """

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "date": "2026-09-01",
                        "totalCost": 10.0,
                        "usage": [
                            {
                                "model": "claude-haiku-4-5",
                                "countObservations": 100,
                                "inputUsage": 45_000,
                                "outputUsage": 4_500,
                            }
                        ],
                    }
                ],
                "meta": {"page": 1, "totalPages": 1},
            },
        )

    return httpx.MockTransport(handle)


class TestLangfuseHonoursTheWindowAndAdmitsWhatItCannot:
    """Measured against the pinned SDK rather than assumed.

    langfuse 2.60.10's own `api/resources/metrics/client.py` sends
    `fromTimestamp` and `toTimestamp` to `/api/public/metrics/daily`, and
    documents them as "on or after" and "before" -- the half-open window
    `Scope` defines, so the bounds pass through untranslated.

    It filters by trace name, user, tags and environment, and `llm/tracing.py`
    sets none of them: the configuration reaches Langfuse only as trace
    metadata, which this aggregate does not filter on. So the reading covers
    the window and never the configuration, and says so.
    """

    def test_the_window_is_sent_as_the_endpoints_own_parameters(self, settings: Settings) -> None:
        asked: list[httpx.Request] = []
        result = langfuse_reading(
            langfuse_settings(settings), scope=WINDOW, transport=daily_metrics(asked)
        )
        assert result.status == "read"
        assert asked[0].url.params["fromTimestamp"] == "2026-09-01T00:00:00Z"
        assert asked[0].url.params["toTimestamp"] == "2026-09-02T00:00:00Z"

    def test_an_unbounded_scope_sends_no_bounds(self, settings: Settings) -> None:
        """The M8 request, unchanged: a default scope must not start narrowing
        a total that every existing caller reads as the whole of it."""
        asked: list[httpx.Request] = []
        langfuse_reading(langfuse_settings(settings), transport=daily_metrics(asked))
        assert "fromTimestamp" not in asked[0].url.params
        assert "toTimestamp" not in asked[0].url.params

    def test_the_reading_declares_the_window_it_covers(self, settings: Settings) -> None:
        result = langfuse_reading(
            langfuse_settings(settings), scope=WINDOW, transport=daily_metrics([])
        )
        assert result.covers == WINDOW
        assert result.spend is not None
        assert result.spend.total_usd == Decimal("10")

    def test_a_configuration_is_never_claimed_as_covered(self, settings: Settings) -> None:
        """The defect this closes. Claiming it would set one ablation cell's
        ledger against every cell's Langfuse total and report the difference
        as a discrepancy in the meter."""
        result = langfuse_reading(
            langfuse_settings(settings), scope=PHASE, transport=daily_metrics([])
        )
        assert result.covers == WINDOW
        assert at_scope(result, PHASE).status == "unreachable"

    def test_the_detail_says_which_window_was_asked_for(self, settings: Settings) -> None:
        result = langfuse_reading(
            langfuse_settings(settings), scope=WINDOW, transport=daily_metrics([])
        )
        assert result.spend is not None
        assert "fromTimestamp/toTimestamp" in result.spend.detail

    def test_an_outage_is_still_unreachable_and_carries_no_window(self, settings: Settings) -> None:
        """Observability degrading to a no-op must not become a discrepancy,
        window or no window."""
        transport = httpx.MockTransport(lambda request: httpx.Response(503))
        result = langfuse_reading(langfuse_settings(settings), scope=WINDOW, transport=transport)
        assert result.status == "unreachable"
        assert result.spend is None


class TestEveryReadingIsCheckedAgainstTheScopeAsked:
    """`readings()` is the one place `at_scope` is applied, so a reader added
    later that ignores the window is refused by default rather than on
    remembering to be refused. Tested with exactly such a reader."""

    def test_a_reader_that_ignores_the_window_is_refused_not_compared(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import cascade.trace.aws_spend as aws_spend
        import cascade.trace.ledger as ledger_module

        def ignores_the_window(_settings: Settings, **_kwargs: object) -> SourceReading:
            return reading(LANGFUSE_SOURCE, "10.00")  # covers=Scope(): everything

        monkeypatch.setattr(ledger_module, "langfuse_reading", ignores_the_window)
        monkeypatch.setattr(
            aws_spend,
            "invocation_log_reading",
            lambda _settings, **_kwargs: unread(AWS_SOURCE, "not configured"),
        )
        found = {r.name: r for r in ledger_module.readings(settings, scope=WINDOW)}

        assert found[LANGFUSE_SOURCE].status == "unreachable"
        assert found[LANGFUSE_SOURCE].spend is None
        # And the source that was never asked is untouched, so a deployment
        # without AWS does not start failing the moment `--since` is passed.
        assert found[AWS_SOURCE].status == "not configured"

    def test_a_reader_that_honours_it_is_compared(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Guard the guard: without this, the assertion above would hold over a
        `readings()` that refused everything."""
        import cascade.trace.aws_spend as aws_spend
        import cascade.trace.ledger as ledger_module

        monkeypatch.setattr(
            ledger_module,
            "langfuse_reading",
            lambda _settings, **_kwargs: reading(LANGFUSE_SOURCE, "10.00", covers=WINDOW),
        )
        monkeypatch.setattr(
            aws_spend,
            "invocation_log_reading",
            lambda _settings, **_kwargs: unread(AWS_SOURCE, "not configured"),
        )
        found = {r.name: r for r in ledger_module.readings(settings, scope=WINDOW)}

        assert found[LANGFUSE_SOURCE].status == "read"

    def test_readings_are_sorted_by_name(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Invariant 7: the verdict must list sources in the same order twice."""
        import cascade.trace.aws_spend as aws_spend
        import cascade.trace.ledger as ledger_module

        monkeypatch.setattr(
            ledger_module,
            "langfuse_reading",
            lambda _settings, **_kwargs: reading(LANGFUSE_SOURCE, "10.00"),
        )
        monkeypatch.setattr(
            aws_spend,
            "invocation_log_reading",
            lambda _settings, **_kwargs: unread(AWS_SOURCE, "not configured"),
        )
        assert [r.name for r in ledger_module.readings(settings)] == sorted(
            [AWS_SOURCE, LANGFUSE_SOURCE]
        )
