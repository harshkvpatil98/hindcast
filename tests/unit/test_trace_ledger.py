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
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from cascade.trace.ledger import (
    LANGFUSE_SOURCE,
    LOCAL_SOURCE,
    RECONCILE_TOLERANCE,
    Basis,
    LedgerReconciliation,
    PhaseSpend,
    ReadingStatus,
    SourceComparison,
    SourceReading,
    WrittenBy,
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
) -> SourceReading:
    return SourceReading(
        name=name,
        written_by=written_by,
        basis=basis,
        spend=spend(name, usd, calls),
    )


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
