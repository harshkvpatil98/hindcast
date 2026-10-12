"""Appendix D's figures (ADR-0024).

Written as SVG from the pinned stack, so they are testable as documents: a
test parses the output and asserts on what a reader would see, rather than on
whether a file appeared.
"""

from __future__ import annotations

from xml.etree import ElementTree as ET

import pytest

from cascade.eval.figures import Box, convergence_svg, forest_svg, reliability_svg, scatter_svg
from cascade.eval.schema import BootstrapInterval, CalibrationBin, CalibrationReport, Comparison


def _parse(svg: str) -> ET.Element:
    """Parse a figure this module's own code just produced.

    Bandit's S314 warns that `xml.etree` is unsafe on *untrusted* input. The
    input here is the output of the function under test, which is the only way
    to assert that it produced a well-formed document at all -- swapping in a
    hardened parser would harden nothing and would add a dependency the stack
    does not pin.
    """
    return ET.fromstring(svg)  # noqa: S314 -- input is this module's own output


def _report(counts: tuple[int, ...] = (0, 0, 0, 0, 12, 9, 3, 0, 0, 0)) -> CalibrationReport:
    bins = []
    for index, count in enumerate(counts):
        lo, hi = index / 10, (index + 1) / 10
        if count == 0:
            bins.append(
                CalibrationBin(
                    index=index,
                    lo=lo,
                    hi=hi,
                    count=0,
                    mean_pred=None,
                    obs_freq=None,
                    wilson_lo=None,
                    wilson_hi=None,
                )
            )
            continue
        bins.append(
            CalibrationBin(
                index=index,
                lo=lo,
                hi=hi,
                count=count,
                mean_pred=(lo + hi) / 2,
                obs_freq=min(1.0, (lo + hi) / 2 + 0.08),
                wilson_lo=max(0.0, lo - 0.1),
                wilson_hi=min(1.0, hi + 0.1),
            )
        )
    return CalibrationReport(bins=tuple(bins), ece=0.0812, mce=0.1300, n=sum(counts))


def _comparison(name: str, point: float, adjusted: float | None) -> Comparison:
    return Comparison(
        name=name,
        config_a="C05",
        config_b="C01",
        brier_a=0.168,
        brier_b=0.141,
        n_paired=180,
        interval=BootstrapInterval(
            point=point, lo=point - 0.01, hi=point + 0.01, b=10000, p_value=0.01
        ),
        p_adjusted=adjusted,
    )


class TestBox:
    def test_it_maps_the_data_range_onto_the_canvas(self) -> None:
        box = Box(10, 20, 100, 50, 0.0, 1.0, 0.0, 1.0)
        assert box.sx(0.0) == 10
        assert box.sx(1.0) == 110

    def test_the_y_axis_is_flipped_because_svg_grows_downward(self) -> None:
        box = Box(0, 0, 100, 100, 0.0, 1.0, 0.0, 1.0)
        assert box.sy(0.0) == 100
        assert box.sy(1.0) == 0

    def test_values_outside_the_range_are_clamped_not_drawn_off_canvas(self) -> None:
        box = Box(0, 0, 100, 100, 0.0, 1.0, 0.0, 1.0)
        assert box.sx(2.0) == 100
        assert box.sx(-1.0) == 0

    def test_a_degenerate_range_lands_in_the_middle(self) -> None:
        box = Box(0, 0, 100, 100, 0.5, 0.5, 0.0, 1.0)
        assert box.sx(0.5) == 50


class TestReliability:
    def test_it_is_a_valid_svg_document(self) -> None:
        _parse(reliability_svg(_report(), title="Reliability"))

    def test_the_bin_counts_are_drawn_and_labelled(self) -> None:
        """§10.5: "a reliability curve without bin counts hides the fact that
        the interesting bins may hold nine scenarios"."""
        svg = reliability_svg(_report(), title="Reliability")
        assert ">12<" in svg
        assert ">9<" in svg
        assert ">3<" in svg

    def test_the_wilson_intervals_are_drawn(self) -> None:
        svg = reliability_svg(_report(), title="Reliability")
        assert svg.count("<line") > 10

    def test_ece_and_mce_appear_on_the_figure(self) -> None:
        svg = reliability_svg(_report(), title="Reliability")
        assert "ECE 0.0812" in svg
        assert "MCE 0.1300" in svg

    def test_an_empty_report_still_renders(self) -> None:
        _parse(reliability_svg(_report(counts=(0,) * 10), title="Empty"))

    def test_the_title_is_escaped(self) -> None:
        svg = reliability_svg(_report(), title="A & B <not a tag>")
        _parse(svg)
        assert "&amp;" in svg


class TestForest:
    def test_it_is_a_valid_svg_document(self) -> None:
        _parse(forest_svg([_comparison("LOO asymmetry", 0.027, 0.01)], title="Forest"))

    def test_significant_and_non_significant_intervals_are_drawn_differently(self) -> None:
        """The Holm correction is visible in the figure, not only in a table
        two pages away."""
        significant = forest_svg([_comparison("a", 0.03, 0.001)], title="f")
        not_significant = forest_svg([_comparison("a", 0.03, 0.9)], title="f")
        assert significant != not_significant

    def test_the_point_estimate_and_bounds_are_labelled(self) -> None:
        svg = forest_svg([_comparison("LOO asymmetry", 0.027, 0.01)], title="Forest")
        assert "+0.0270" in svg

    def test_an_empty_family_renders_without_crashing(self) -> None:
        _parse(forest_svg([], title="Forest"))

    def test_zero_is_drawn_as_a_rule(self) -> None:
        """ "The interval crosses zero" is the whole content of the chart."""
        svg = forest_svg([_comparison("a", 0.03, 0.01)], title="f")
        assert 'stroke-width="1"' in svg


class TestConvergence:
    def test_it_is_a_valid_svg_document(self) -> None:
        _parse(convergence_svg([(25, 0.03), (50, 0.02), (100, 0.01)], title="c"))

    def test_the_rungs_are_labelled_by_replicate_count(self) -> None:
        svg = convergence_svg([(25, 0.03), (200, 0.004)], title="c")
        assert "n=25" in svg
        assert "n=200" in svg

    def test_no_data_says_so_rather_than_drawing_an_empty_axis(self) -> None:
        svg = convergence_svg([], title="c")
        assert "no convergence data" in svg

    def test_a_flat_curve_does_not_divide_by_zero(self) -> None:
        _parse(convergence_svg([(25, 0.0), (50, 0.0)], title="c"))


class TestScatter:
    def test_it_is_a_valid_svg_document(self) -> None:
        _parse(scatter_svg([0.1, 0.4], [0.2, 0.7], title="s", x_label="sigma", y_label="error"))

    def test_the_annotation_is_passed_in_not_computed(self) -> None:
        """So the figure and the significance table can never disagree about
        the same number."""
        svg = scatter_svg(
            [0.1],
            [0.2],
            title="s",
            x_label="sigma",
            y_label="error",
            annotation="Spearman rho = 0.4210",
        )
        assert "Spearman rho = 0.4210" in svg

    def test_mismatched_columns_raise(self) -> None:
        with pytest.raises(ValueError, match="length mismatch"):
            scatter_svg([0.1], [0.2, 0.3], title="s", x_label="x", y_label="y")

    def test_an_empty_scatter_renders(self) -> None:
        _parse(scatter_svg([], [], title="s", x_label="x", y_label="y"))


class TestThemeAndAccessibility:
    def test_every_figure_paints_its_own_background(self) -> None:
        """These are read inside a report that may be on a dark ground; a
        transparent chart with dark ink is an invisible chart."""
        for svg in (
            reliability_svg(_report(), title="t"),
            forest_svg([_comparison("a", 0.01, 0.4)], title="t"),
            convergence_svg([(25, 0.01)], title="t"),
            scatter_svg([0.1], [0.2], title="t", x_label="x", y_label="y"),
        ):
            assert 'fill="#ffffff"' in svg

    def test_every_figure_carries_a_title_and_a_label(self) -> None:
        svg = reliability_svg(_report(), title="Reliability diagram")
        assert 'role="img"' in svg
        assert "<title>Reliability diagram</title>" in svg


def _texts(svg: str) -> list[tuple[float, str]]:
    """Every text node as (y, content), in document order."""
    root = _parse(svg)
    ns = "{http://www.w3.org/2000/svg}"
    return [
        (float(node.attrib["y"]), node.text or "")
        for node in root.iter(f"{ns}text")
        if "y" in node.attrib
    ]


class TestTheForestLabelsDoNotCollide:
    """Every row's numbers were drawn at one y, stacking four labels into a
    smear over the tick labels -- in the one display whose job is to show four
    intervals apart."""

    def test_each_row_s_numbers_are_on_that_row(self) -> None:
        rows = [_comparison(f"row {index}", 0.01 * (index + 1), 0.5) for index in range(4)]
        numeric = [(y, text) for y, text in _texts(forest_svg(rows, title="f")) if "[" in text]
        assert len(numeric) == 4
        assert len({y for y, _ in numeric}) == 4

    def test_no_label_shares_a_line_with_another(self) -> None:
        rows = [_comparison(f"row {index}", 0.01 * (index + 1), 0.5) for index in range(4)]
        ys = [y for y, text in _texts(forest_svg(rows, title="f")) if "row " in text or "[" in text]
        assert len(ys) == len(set(ys))

    def test_each_row_carries_the_n_it_was_paired_on(self) -> None:
        svg = forest_svg([_comparison("a", 0.02, 0.5)], title="f")
        assert "n=180" in svg

    def test_an_unadjusted_row_says_so_rather_than_dropping_the_field(self) -> None:
        svg = forest_svg([_comparison("a", 0.02, None)], title="f")
        assert "p* not measured" in svg


class TestTheConvergenceAxesAreNamed:
    def test_both_axes_carry_a_label(self) -> None:
        svg = convergence_svg([(25, 0.04), (50, 0.02)], title="c")
        assert "replicates in the ensemble" in svg
        assert "mean |change in p|" in svg

    def test_the_y_label_is_rotated_onto_the_y_axis(self) -> None:
        """A quantity printed under the plot is read as the x quantity."""
        svg = convergence_svg([(25, 0.04), (50, 0.02)], title="c")
        rotated = [line for line in svg.split("<text") if "mean |change in p|" in line]
        assert rotated == [] or "rotate(-90" in svg
        assert "rotate(-90" in svg

    def test_a_flat_series_does_not_print_an_axis_maximum_no_datum_reaches(self) -> None:
        """peak 0 used to borrow a unit scale and label the top 1.0000."""
        svg = convergence_svg([(25, 0.0), (50, 0.0)], title="c")
        assert "1.0000" not in svg
        assert "no measured maximum" in svg

    def test_the_axis_top_is_the_measured_maximum(self) -> None:
        svg = convergence_svg([(25, 0.0432), (50, 0.0211)], title="c")
        assert "0.0432" in svg

    def test_a_single_rung_says_there_is_nothing_to_converge_across(self) -> None:
        svg = convergence_svg([(25, 0.04)], title="c")
        assert "nothing to converge across" in svg


class TestTheScatterNamesADegenerateAxis:
    def test_no_spread_in_x_is_stated_on_the_figure(self) -> None:
        svg = scatter_svg(
            [0.0] * 5, [0.1, 0.4, 0.2, 0.9, 0.3], title="s", x_label="sigma", y_label="error"
        )
        assert "no spread on the x axis" in svg

    def test_a_real_spread_carries_no_such_note(self) -> None:
        svg = scatter_svg(
            [0.0, 0.2, 0.4], [0.1, 0.4, 0.2], title="s", x_label="sigma", y_label="error"
        )
        assert "no spread on the x axis" not in svg


class TestTickLabelsCarryTheAxisRange:
    def test_a_narrow_axis_does_not_print_the_same_tick_six_times(self) -> None:
        """Six "0.00" labels read as an axis with no range rather than as an
        axis whose range is small."""
        svg = scatter_svg(
            [0.0, 0.002, 0.004, 0.006],
            [0.1, 0.4, 0.2, 0.9],
            title="s",
            x_label="sigma",
            y_label="error",
        )
        ticks = [text for _y, text in _texts(svg) if text.replace(".", "").isdigit()]
        assert len(set(ticks)) > 2

    def test_a_unit_axis_still_prints_two_decimals(self) -> None:
        svg = scatter_svg([0.0, 1.0], [0.0, 1.0], title="s", x_label="x", y_label="y")
        assert "0.20" in svg
