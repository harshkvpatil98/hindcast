"""Writing ``reports/study_{ts}/`` (spec Appendix D, §10). The thin shell.

Every number that reaches this module has already been measured. That is the
whole discipline of §1 expressed as a module boundary: **no target value is
written into a report code path**, so there is no constant here to compare
against, no tolerance to pass, and nothing that can be steered. A quantity that
could not be produced is written as ``null`` and named in ``headline.md`` as
not produced -- never filled with the value the spec expects.

The artifact is deliberately plain: CSV that a spreadsheet opens, JSON that a
script reads, Markdown a person reads, SVG a browser renders (ADR-0024). No
notebook, no pickle, no database handle required to read it six months later.
"""

from __future__ import annotations

import csv
import json
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from typing import Any

from cascade.eval.ablation import HEADLINE_CELL
from cascade.eval.market import MARKET_CONFIG_ID, CoverageSummary
from cascade.eval.schema import (
    AblationCell,
    CalibrationReport,
    Comparison,
    ComparisonFamily,
    DispersionFinding,
    DomainMetrics,
    EvidenceFinding,
    MetricSet,
    ScoredForecast,
)
from cascade.eval.split import Partition, PartitionInteraction, SplitDeclaration

__all__ = [
    "FIGURE_FORMAT",
    "WITHHELD_RULE",
    "FigureNote",
    "StudyArtifact",
    "git_sha",
    "report_id",
    "write_report",
]

FIGURE_FORMAT = "svg"

WITHHELD_RULE = (
    "A stored configuration that is not a declared study configuration is a "
    "tuning variant. It is scored on dev only, so that a report cannot become "
    "the place variants get compared on held-out scenarios; declaring it in "
    "`cascade/eval/ablation.py` or `cascade/eval/supplementary.py` is the price "
    "of reporting it on test."
)


@dataclass(frozen=True, slots=True)
class FigureNote:
    """One figure slot: whether it was drawn, and if not, why not.

    Preserves the rule that the artifact's own index describes the directory
    that exists. A figure list that names four files when two were written
    reads as two missing files rather than as two measurements that did not
    happen, and those are different statements.
    """

    filename: str
    written: bool
    note: str


def report_id(*, now: datetime | None = None) -> str:
    """``study_YYYYMMDDTHHMMZ`` -- Appendix D's directory name.

    ``now`` is an argument rather than a clock read, for the same reason
    ``as_of`` is (invariant 1): a test that cannot fix the timestamp cannot
    assert on the path.
    """
    moment = now or datetime.now(UTC)
    return f"study_{moment.astimezone(UTC).strftime('%Y%m%dT%H%M')}Z"


def git_sha() -> str:
    """The commit the report describes, or ``"unknown"`` outside a checkout.

    Appendix D's ``manifest.json`` names the git sha alongside the scenario
    hash. A report that cannot say which code produced it is a report nobody
    can reproduce, so this fails soft to a visible string rather than to an
    empty one.
    """
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],  # noqa: S607 -- resolved from PATH by design
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    return out.stdout.strip() if out.returncode == 0 and out.stdout.strip() else "unknown"


@dataclass
class StudyArtifact:
    """Everything the report prints, already measured.

    Assembled by the CLI and handed here whole. The split exists so that
    writing the artifact is testable without a database and so that no metric
    is computed twice -- once for the terminal and once for the file -- which
    is how a report and its summary come to disagree.
    """

    report_id: str
    written_at: datetime
    manifest_sha256: str
    git_sha: str
    n_scenarios_sealed: int
    base_rate: float
    climatology_brier: float
    study_salt: str
    models: Mapping[str, str]
    config_snapshot: Mapping[str, Any]

    headline_config: str = HEADLINE_CELL
    replicate_policy: str = ""
    replicate_notes: tuple[str, ...] = ()

    metrics: tuple[MetricSet, ...] = ()
    baselines: tuple[tuple[str, str, str, MetricSet | None], ...] = ()
    """(baseline_id, name, config_id, metrics-or-None)."""
    cells: tuple[AblationCell, ...] = ()
    comparisons: tuple[Comparison, ...] = ()
    comparison_readings: Mapping[str, str] = field(default_factory=dict)
    calibration: CalibrationReport | None = None
    per_domain: tuple[DomainMetrics, ...] = ()
    dispersion: DispersionFinding | None = None
    convergence: tuple[tuple[int, float, float, int], ...] = ()
    """(n, mean_abs_change, mean_sigma, scenarios)."""
    scored: tuple[ScoredForecast, ...] = ()
    baseline_rows: tuple[tuple[str, str, float, int], ...] = ()
    """(baseline_config_id, scenario_id, p_hat, outcome)."""
    grid_rows: tuple[tuple[str, str, float, float, int], ...] = ()
    """(cell_id, scenario_id, p_hat, sigma, outcome)."""
    leakage: Mapping[str, Any] = field(default_factory=dict)
    cost_ledger: Mapping[str, Any] = field(default_factory=dict)
    blocked: tuple[str, ...] = ()
    """Quantities that could not be produced, and why. Printed prominently."""

    split: SplitDeclaration | None = None
    """The declared dev/test split. ``None`` only for an artifact assembled
    without one, which the report then says in so many words."""
    partition: Partition = "all"
    """The partition every figure in ``metrics``, ``calibration``,
    ``per_domain``, ``comparisons``, ``dispersion`` and ``evidence`` was
    measured on. The CLI writes ``test``; ``dev`` is a tuning report."""
    headline_partitions: tuple[tuple[Partition, MetricSet | None], ...] = ()
    """The headline configuration on test, all and dev, so the all-scenario
    figure is printed beside the headline and labelled, not left to be
    recomputed by a reader who will not label it."""
    split_interactions: tuple[PartitionInteraction, ...] = ()
    evidence: EvidenceFinding | None = None
    withheld_configs: tuple[str, ...] = ()
    """Stored configurations that are not declared study configurations and
    were therefore not scored on this partition (see
    ``split.require_declared_config``)."""

    market_coverage: CoverageSummary | None = None
    """What the market benchmark covers, over the whole sealed set: usable,
    stale, and each reason a price could not be had. Carried on the artifact so
    that it is printed *beside* the market's Brier rather than left in the
    output of a separate command -- a Brier over 143 of 180 scenarios that does
    not say so is the drift nothing downstream can detect."""

    provenance: Mapping[str, Any] = field(default_factory=dict)
    """What the forecasts were made against: corpus size and span, retrieval
    mode, prompt revision, and the installed versions of the pinned stack. A
    report whose numbers cannot be tied to the evidence and the code that
    produced them is a report nobody can reproduce."""

    def headline_metrics(self) -> MetricSet | None:
        for item in self.metrics:
            if item.config_id == self.headline_config:
                return item
        return None


def _csv(rows: Sequence[Sequence[Any]], header: Sequence[str]) -> str:
    buffer = StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    return buffer.getvalue()


def _json(payload: Any) -> str:
    """Stable JSON: sorted keys, two-space indent, trailing newline.

    Sorted because the artifact is committed and a re-run must produce a diff
    that shows which *numbers* changed rather than which keys moved.
    """
    return json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n"


def _coverage_payload(coverage: CoverageSummary | None) -> Any:
    """The market benchmark's coverage as primitive data, or a stated absence."""
    if coverage is None:
        return {
            "measured": False,
            "note": (
                "Coverage was not read. The Brier beside this is over whatever "
                "prices are stored; run `cascade eval market-prices` to measure "
                "what it covers."
            ),
        }
    payload = coverage.model_dump(mode="json")
    payload["measured"] = True
    payload["note"] = (
        "Counts every sealed scenario exactly once: usable + stale + the "
        "unobtainable reasons sum to n_scenarios. Only a usable price is "
        "scored; nothing is imputed."
    )
    return payload


def _metric_payload(item: MetricSet, *, coverage: CoverageSummary | None = None) -> dict[str, Any]:
    """One configuration's metrics as primitive data.

    The market benchmark's coverage travels inside its own metric block. §1's
    rule that a report never implies a number it did not measure applies to the
    denominator as much as to the numerator: a Brier is a claim about the
    scenarios it was taken over, and this is where those are counted.
    """
    if item.config_id == MARKET_CONFIG_ID:
        return {**_metric_body(item), "market_coverage": _coverage_payload(coverage)}
    return _metric_body(item)


def _metric_body(item: MetricSet) -> dict[str, Any]:
    return {
        "config_id": item.config_id,
        "n": item.n,
        "base_rate": item.base_rate,
        "mean_p_hat": item.mean_p_hat,
        "brier": item.brier,
        "decider_policies": list(item.policies),
        "log_loss": item.log_loss,
        "auc": item.auc,
        "ece": item.ece,
        "mce": item.mce,
        "bss_vs_climatology": item.bss_vs_climatology,
        "climatology_brier_on_these_scenarios": item.climatology_brier,
        "bss_vs_single_direct": item.bss_vs_direct,
        "brier_recalibrated_holdout": item.brier_recalibrated,
        "murphy": {
            "reliability": item.murphy.reliability,
            "resolution": item.murphy.resolution,
            "uncertainty": item.murphy.uncertainty,
            "brier": item.murphy.brier,
            "binning_residual": item.murphy.residual,
            "bins": item.murphy.bins,
        },
    }


def _headline_markdown(artifact: StudyArtifact, figures: Sequence[FigureNote] = ()) -> str:
    """Appendix D's ``headline.md``: the numbers, each with its paragraph.

    Written so that a blocked study reads as blocked. If the headline
    configuration has no scoreable forecasts the file says so in its first
    section, rather than opening with a table of dashes a skimming reader
    would mistake for a formatting problem.
    """
    head = artifact.headline_metrics()
    lines = [
        f"# Cascade — study {artifact.report_id}",
        "",
        f"- Scenario manifest: `{artifact.manifest_sha256}`",
        f"- Git commit: `{artifact.git_sha}`",
        f"- Written: {artifact.written_at.isoformat()}",
        f"- Sealed set: {artifact.n_scenarios_sealed} scenarios, "
        f"base rate {artifact.base_rate:.4f}, climatology Brier "
        f"{artifact.climatology_brier:.6f}",
        f"- Agent model: `{artifact.models.get('agent', 'unknown')}` · "
        f"compiler: `{artifact.models.get('compiler', 'unknown')}`",
        f"- Dev/test split: {_split_bullet(artifact)}",
        "",
        "Every number in this report is measured. Where a quantity could not be "
        "produced it is absent and named below, never substituted.",
        "",
    ]

    lines += _standin_banner(artifact)

    if artifact.blocked:
        lines += ["## Not produced", ""]
        lines += [f"- {item}" for item in artifact.blocked]
        lines += [""]

    lines += _split_section(artifact)

    lines += ["## Headline", ""]
    lines += _partition_banner(artifact)
    if head is not None and head.provisional:
        lines += [
            f"> **These forecasts were produced by "
            f"{', '.join(head.policies)} decider(s), not the study's agents.** "
            "Every number in this section verifies the evaluation harness end to "
            "end. None of them is a result about Cascade.",
            "",
        ]
    if head is None:
        lines += [
            f"The headline configuration `{artifact.headline_config}` has no scoreable "
            "forecasts, so there is no headline Brier, no skill score and no "
            "calibration curve. Nothing in this section is estimated from a "
            "partial grid.",
            "",
        ]
    else:
        lines += [
            f"**Brier {head.brier:.6f}** over {head.n} scenarios "
            f"{_PARTITION_PHRASE[artifact.partition]} "
            f"(`{head.config_id}`). Mean forecast {head.mean_p_hat:.4f} against a "
            f"base rate of {head.base_rate:.4f}.",
            "",
        ]
        lines += _partition_table(artifact)
        lines += [
            f"**Brier skill score vs climatology: "
            f"{_fmt(head.bss_vs_climatology, '{:.4f}')}** — climatology, the sealed "
            "base rate issued as a constant forecast, scores "
            f"{_fmt(head.climatology_brier, '{:.6f}')} on these scenarios. Beating it "
            "is necessary, not impressive.",
            "",
            f"**Brier skill score vs the single-model baseline: "
            f"{_fmt(head.bss_vs_direct, '{:.4f}')}** — the reference §10.2 asks the "
            "headline claim to be stated against.",
            "",
            f"**Murphy decomposition:** REL {head.murphy.reliability:.6f} - RES "
            f"{head.murphy.resolution:.6f} + UNC {head.murphy.uncertainty:.6f}; the "
            f"binning residual is {head.murphy.residual:+.6f} over "
            f"{head.murphy.bins} bins. REL is the calibration term and RES the "
            "discrimination term — two systems with the same Brier can fail for "
            "opposite reasons, and this is what separates them.",
            "",
            f"**Calibration:** ECE {head.ece:.4f}, MCE {head.mce:.4f}. AUC "
            f"{_fmt(head.auc, '{:.4f}')} — a calibration-free view, so a system can "
            "be miscalibrated and still rank correctly.",
            "",
        ]
        if head.brier_recalibrated is not None:
            lines += [
                f"Isotonic recalibration fitted on a held-out half gives "
                f"{head.brier_recalibrated:.6f}. This is a **secondary** number. The "
                "headline is the raw system.",
                "",
            ]

    lines += _baselines_section(artifact)
    lines += _cells_section(artifact)

    lines += ["## Replicate policy", ""]
    lines += [
        artifact.replicate_policy
        or "No replicate policy was recorded, because no ablation cell was executed.",
        "",
    ]
    if artifact.headline_config != HEADLINE_CELL:
        lines += [
            f"The notes below call `{HEADLINE_CELL}` the headline cell because that is "
            "Appendix C's design. This report leads with "
            f"`{artifact.headline_config}`, which is a different choice and does not "
            f"move the cap: `{artifact.headline_config}` ran at whatever its own row in "
            "the table above says.",
            "",
        ]
    lines += [f"- {note}" for note in artifact.replicate_notes]
    lines += [""]

    lines += ["## Reading the deltas", ""]
    lines += [
        f"Every delta below is against `{artifact.headline_config}`, the configuration "
        "this report leads with. Information asymmetry is nested inside causal "
        "decomposition — the visibility policy is derived from the compiled graph — so "
        "**the two leave-one-out deltas do not sum**, and the "
        "decomposition-net-of-asymmetry figure is published here rather than left for a "
        "reader to compute.",
        "",
    ]
    if artifact.headline_config != HEADLINE_CELL:
        lines += [
            f"`{artifact.headline_config}` is **not** Appendix C's full configuration "
            f"(`{HEADLINE_CELL}`), so these are not the leave-one-out deltas §6.4 "
            "defines. They are differences against whichever configuration was "
            "available to lead this report, and none of them measures the contribution "
            "of a factor the full configuration holds.",
            "",
        ]
    main = _family(artifact, "appendix_c")
    if main:
        lines += _comparison_table(artifact, main)
    else:
        lines += ["No cell pair had forecasts on both sides, so no delta was computed.", ""]

    lines += ["## Supplementary comparisons", ""]
    lines += [
        "Declared in `cascade/eval/supplementary.py` before any forecast existed, and "
        "**not part of Appendix C's twelve-cell family**. They are Holm-adjusted in a "
        "family of their own, so their presence changes none of the adjusted p-values "
        "above; each family is controlled at its own alpha, and a reader who wants one "
        "family-wise rate across both should adjust the raw p-values together. A "
        "favourable supplementary delta is a finding to report. Changing the headline "
        "configuration because of one measured on the test partition would be tuning "
        "on test: that decision belongs to the dev partition.",
        "",
    ]
    supplementary = _family(artifact, "supplementary")
    if supplementary:
        lines += _comparison_table(artifact, supplementary)
    else:
        lines += [
            "No supplementary cell has forecasts on both sides. Run "
            "`cascade eval grid --supplementary`.",
            "",
        ]

    exploratory = _family(artifact, "exploratory_dev")
    if exploratory:
        lines += [
            "## Exploratory comparisons (dev partition)",
            "",
            "Tuning variants against the headline configuration, on dev scenarios only. "
            "These inform decisions. None of them is a result, and none was computed "
            "on a held-out scenario.",
            "",
        ]
        lines += _comparison_table(artifact, exploratory)

    lines += _evidence_section(artifact)

    if artifact.dispersion is not None:
        finding = artifact.dispersion
        lines += [
            "## Dispersion",
            "",
            f"sigma is the standard deviation of terminal outcome scores across "
            f"replicates. Over {finding.n} scenarios, Spearman rho between sigma and "
            f"absolute error is {_fmt(finding.spearman_rho, '{:.4f}')} "
            f"(permutation p {_fmt(finding.spearman_p, '{:.4g}')}); Pearson r is "
            f"{_fmt(finding.pearson_r, '{:.4f}')} "
            f"(p {_fmt(finding.pearson_p, '{:.4g}')}).",
            "",
            f"{finding.flagged} of {finding.n} forecasts were flagged multi-modal "
            f"(sigma > {finding.sigma_threshold} or dip p < 0.05). Brier on the "
            f"{finding.flagged} flagged {_fmt(finding.brier_flagged, '{:.6f}')} against "
            f"{_fmt(finding.brier_unflagged, '{:.6f}')} on the "
            f"{finding.n - finding.flagged} unflagged. The claim under test is that the "
            "flag identifies the forecasts to distrust; the numbers above are what it "
            "measured, whichever way they fell.",
            "",
        ]

    lines += _artifact_section(artifact, figures)
    return "\n".join(lines)


def _rows_cell(count: int, empty: str) -> str:
    """A row count, or the reason there is none. Never a bare zero."""
    return str(count) if count else f"0 — {empty}"


def _artifact_section(artifact: StudyArtifact, figures: Sequence[FigureNote]) -> list[str]:
    """An index of the directory that exists, with each file's own row count.

    Preserves the report's one promise about itself. The previous index named
    ten files and four figures unconditionally, so a run that wrote a
    header-only CSV and no figure at all still published a table claiming
    otherwise — a reader would read the empty directory as a packaging fault
    rather than as a measurement that did not happen.
    """
    calibration_rows = 0 if artifact.calibration is None else len(artifact.calibration.bins)
    tier_rows = 0 if artifact.evidence is None else len(artifact.evidence.tiers)
    lines = [
        "## Artifact",
        "",
        "What this directory holds, as written. A row count of 0 means the file carries "
        "its header and nothing else, for the reason given.",
        "",
        "| File | Rows | Contents |",
        "|---|---|---|",
        "| `manifest.json` | - | scenario hash, git sha, model versions, config, " "provenance |",
        f"| `metrics.json` | {len(artifact.metrics)} | every metric, every cell, "
        "machine-readable |",
        f"| `baselines.csv` | {_rows_cell(len(artifact.baseline_rows), 'no baseline was scored')} "
        "| the §10.2 baselines and the market at the cutoff, per scenario |",
        f"| `ablation_grid.csv` | {_rows_cell(len(artifact.grid_rows), 'no cell was scored')} "
        "| the Appendix C cells, per scenario |",
        f"| `calibration.csv` | "
        f"{_rows_cell(calibration_rows, 'the headline configuration has no forecasts')} "
        "| bins: count, mean_pred, obs_freq, Wilson bounds |",
        f"| `per_domain.csv` | "
        f"{_rows_cell(len(artifact.per_domain), 'the headline configuration has no forecasts')} "
        "| Brier by domain with counts (sealed labels; see ADR-0043) |",
        f"| `per_evidence_tier.csv` | "
        f"{_rows_cell(tier_rows, 'the evidence tiering was not produced')} "
        "| Brier by evidence tier with counts |",
        f"| `significance.json` | "
        f"{_rows_cell(len(artifact.comparisons), 'no comparison had both sides')} "
        "| paired bootstrap CIs, Holm-adjusted p-values |",
        "| `leakage_report.json` | - | the three structural mechanisms; poison-pill and "
        "memorisation are measured elsewhere and read as null here |",
        "| `cost_ledger.json` | - | per-phase spend, from the meter checkpoints |",
        "",
        f"Figures, as `figures/*.{FIGURE_FORMAT}` (ADR-0024):",
        "",
        "| Figure | Written | Why |",
        "|---|---|---|",
    ]
    lines += [
        f"| `{note.filename}` | {'yes' if note.written else '**no**'} | {note.note} |"
        for note in figures
    ]
    return [*lines, ""]


def _fmt(value: float | None, pattern: str) -> str:
    """Format a measurement, or say plainly that there is not one."""
    return "not measured" if value is None else pattern.format(value)


def _standin_banner(artifact: StudyArtifact) -> list[str]:
    """Name every stand-in configuration in the artifact, above everything else.

    The headline section already warns when the *lead* figure came from a
    stand-in decider. That is not enough: a report whose headline configuration
    has no forecasts at all still ships a grid CSV, a per-domain table and four
    figures built entirely from stand-in runs, and says nothing. M5 stamps the
    policy on a run because a footnote is not a mechanism; the same argument
    applies to the document.
    """
    standins = [item for item in artifact.metrics if item.provisional]
    if not standins:
        return []
    listed = ", ".join(
        f"`{item.config_id}` ({', '.join(item.policies)}, n={item.n})" for item in standins
    )
    return [
        f"> **{len(standins)} of the {len(artifact.metrics)} scored configuration(s) in "
        f"this artifact were produced by a stand-in decider, not the study's agents:** "
        f"{listed}. Every number derived from them — in `metrics.json`, "
        "`ablation_grid.csv`, the delta table and the figures — verifies the harness "
        "end to end. None of them is a result about Cascade.",
        "",
    ]


def _coverage_bullets(coverage: CoverageSummary) -> list[str]:
    """The market benchmark's denominator, spelled out.

    Preserves the rule that a Brier is printed with the set it was taken over.
    Every sealed scenario appears in exactly one line here, so a reader can add
    the lines up and get the registry back.
    """
    hours = coverage.max_staleness_seconds / 3600.0
    bullets = [
        f"- **{coverage.n_usable} usable** of {coverage.n_scenarios} sealed scenarios — "
        f"a YES probability observed strictly before the cutoff and at most "
        f"{hours:g} h old. This is the benchmark's whole denominator.",
        f"- {coverage.n_stale} priced but **stale** (older than {hours:g} h): excluded.",
    ]
    bullets += [
        f"- {count} **unobtainable: {reason}**: excluded, never imputed."
        for reason, count in coverage.unobtainable
    ]
    bullets += [
        "",
        "| Source | scenarios | usable | stale |",
        "|---|---|---|---|",
    ]
    bullets += [
        f"| {source} | {total} | {usable} | {stale} |"
        for source, total, usable, stale in coverage.by_source
    ]
    bullets += [
        "",
        "Every sealed scenario appears in exactly one of the lines above. A scenario "
        "with no usable price is left out of the market's Brier and out of every "
        "comparison against it; it is never filled with 0.5 or with the base rate.",
        "",
    ]
    return bullets


def _baselines_section(artifact: StudyArtifact) -> list[str]:
    """§10.2's baselines, each with the scenarios its own number was taken over.

    Preserves two things a reader needs and neither the delta table nor
    ``metrics.json`` prose supplies. First, the absolute Brier of every
    reference: a delta of +0.06 against the market says nothing about whether
    either side beat climatology. Second, that **no two rows here are paired**
    — the market covers the scenarios with a usable price and a capped cell
    covers its subsample — so each row carries its own n and the table says in
    words that the rows are not comparable by subtraction.
    """
    if not artifact.baselines:
        return []
    lines = [
        "## Baselines",
        "",
        "§10.2's five, and the market at the cutoff beside them (M14). Each row is "
        f"measured on the **{artifact.partition}** partition, over **its own** "
        "scenarios: the market covers only those with a usable price and a capped cell "
        "only its subsample. **The rows are not paired and their Briers must not be "
        "subtracted from one another** — the paired, bootstrapped differences are in "
        "the delta table below and in `significance.json`.",
        "",
        "| Baseline | config | n | base rate | Brier | BSS vs climatology | decider |",
        "|---|---|---|---|---|---|---|",
    ]
    for _baseline_id, name, config_id, measured in artifact.baselines:
        if measured is None:
            lines.append(f"| {name} | `{config_id}` | - | - | not produced | not produced | - |")
            continue
        lines.append(
            f"| {name} | `{config_id}` | {measured.n} | {measured.base_rate:.4f} | "
            f"{measured.brier:.6f} | {_fmt(measured.bss_vs_climatology, '{:.4f}')} | "
            f"{', '.join(measured.policies)} |"
        )
    lines += [
        "",
        "`decider`: `agent` is the study's agents; `heuristic` or `mixed` is a stand-in "
        "and the row is a mechanism check, not a result; `none` means no decider of "
        "this study's produced the number at all — climatology is arithmetic over the "
        "sealed base rate and the market is other people's money. `-` means the row "
        "was not produced.",
        "",
    ]
    market = next(
        (
            measured
            for _id, _name, config_id, measured in artifact.baselines
            if config_id == MARKET_CONFIG_ID
        ),
        None,
    )
    if market is not None or artifact.market_coverage is not None:
        lines += ["### What the market benchmark covers", ""]
        if artifact.market_coverage is None:
            lines += [
                "**Coverage was not measured.** The market row above is over whatever "
                "prices are stored, and this report cannot say how many of the sealed "
                "scenarios that is. Run `cascade eval market-prices`.",
                "",
            ]
        else:
            lines += _coverage_bullets(artifact.market_coverage)
            if market is not None:
                lines += [
                    f"Of those usable prices, **{market.n}** fall on scenarios this "
                    f"report scored on the {artifact.partition} partition, and that is "
                    "the n behind the market's Brier above — not the usable count, and "
                    "not the sealed count.",
                    "",
                    "**The scenarios the market covers are not a random sample of the "
                    "sealed set.** A scenario has a usable price because it had a "
                    f"liquid market, and those {market.n} resolve YES at "
                    f"{market.base_rate:.4f} against the sealed set's "
                    f"{artifact.base_rate:.4f}. The market's Brier is therefore a "
                    "figure about an easier or harder population than any Brier taken "
                    "over the whole partition, and only the paired delta below compares "
                    "it with anything.",
                    "",
                ]
    return lines


def _cells_section(artifact: StudyArtifact) -> list[str]:
    """Appendix C's cells as *executed*, bridging stored runs to scored n.

    Preserves the count a reader would otherwise have to reconcile alone. A
    cell stores forecasts for the scenarios the grid ran; fewer survive into a
    figure, because the placeholder legs are excluded from every scored figure
    and the rest are split between dev and test. Both numbers are printed side
    by side, with the partition named, so the gap is visible rather than
    inferred.
    """
    executed = [cell for cell in artifact.cells if cell.scenarios_executed]
    if not executed:
        return []
    lines = [
        "## Cells as executed",
        "",
        f"`stored` is the scenarios the grid ran and collapsed into forecasts. `scored "
        f"on {artifact.partition}` is how many of those reached a number in this "
        "report: the rest are either excluded placeholder legs or in the other "
        "partition. The two columns are different counts and neither substitutes for "
        "the other.",
        "",
        f"| Cell | D | replicates run | stored | scored on {artifact.partition} | "
        "decider | Brier |",
        "|---|---|---|---|---|---|---|",
    ]
    for cell in executed:
        measured = cell.metrics
        lines.append(
            f"| {cell.cell_id} | {cell.replicates_design} | "
            f"{'-' if cell.replicates_executed is None else cell.replicates_executed} | "
            f"{cell.scenarios_executed} | {0 if measured is None else measured.n} | "
            f"{'-' if measured is None else ', '.join(measured.policies)} | "
            f"{_fmt(None if measured is None else measured.brier, '{:.6f}')} |"
        )
    return [*lines, ""]


_PARTITION_PHRASE: dict[Partition, str] = {
    "test": "of the held-out **test partition**",
    "dev": "of the **dev partition** (the tuning set)",
    "all": "-- **every scored scenario, dev and test together**",
}

_PARTITION_SCOPE: dict[Partition, str] = {
    "test": "test-partition",
    "dev": "dev-partition",
    "all": "dev and test",
}

_PARTITION_NOTE: dict[Partition, str] = {
    "test": (
        "Held out. No tuning decision was ever informed by these scenarios, so this "
        "is the figure the study reports."
    ),
    "all": (
        "Every scored scenario, dev included. Printed for comparison only and **not "
        "the headline**: it contains the scenarios the system was tuned on, and is "
        "optimistic by however much that tuning fitted them."
    ),
    "dev": (
        "The tuning partition. Optimistic by construction once anything has been "
        "tuned on it; never quote it as a result."
    ),
}


def _family(artifact: StudyArtifact, family: ComparisonFamily) -> list[Comparison]:
    return [item for item in artifact.comparisons if item.family == family]


def _comparison_table(artifact: StudyArtifact, rows: Sequence[Comparison]) -> list[str]:
    """One Holm family as a table, with its size stated and its readings below.

    The family size is printed with the table because it is the multiplier
    behind every adjusted p-value in it, and a reader comparing two tables
    needs to know they were corrected separately.
    """
    lines = [
        f"Holm-Bonferroni family of {len(rows)}, each comparison paired on the "
        f"{_PARTITION_SCOPE[artifact.partition]} scenarios both sides scored.",
        "",
        "| Comparison | delta Brier | 95% CI | n paired | p | Holm p* |",
        "|---|---|---|---|---|---|",
    ]
    for item in rows:
        lines.append(
            f"| {item.name} | {item.interval.point:+.6f} | "
            f"[{item.interval.lo:+.6f}, {item.interval.hi:+.6f}] | {item.n_paired} | "
            f"{item.interval.p_value:.4g} | {_fmt(item.p_adjusted, '{:.4g}')} |"
        )
    lines += [""]
    for item in rows:
        reading = artifact.comparison_readings.get(item.name)
        if reading:
            lines += [f"- **{item.name}** — {reading}", ""]
    return lines


def _split_bullet(artifact: StudyArtifact) -> str:
    split = artifact.split
    if split is None:
        return "none supplied to this report"
    return (
        f"`{split.sha256}` -- {len(split.dev)} dev / {len(split.test)} test; "
        f"this report is written on **{artifact.partition}**"
    )


def _split_section(artifact: StudyArtifact) -> list[str]:
    """State the split: what it is, when it was fixed, and what it applies to.

    Written into every report rather than into documentation, because the
    report is what gets quoted and the claim "this number was held out" is
    worth exactly as much as a reader's ability to check it from the artifact.
    """
    split = artifact.split
    lines = ["## Dev/test split", ""]
    if split is None:
        return [
            *lines,
            "No dev/test split was supplied to this report. Nothing in it may be read "
            "as a held-out figure.",
            "",
        ]
    if split.excluded:
        lines += [
            f"**{len(split.excluded)} of the {split.n + len(split.excluded)} sealed scenarios "
            "are excluded from every scored figure**, and counted here. They are exchange "
            'placeholder legs -- markets listed before a name was known, such as "Will '
            'Candidate B win ..." -- which resolved and so passed every registry rule, '
            "but name no party and cannot be forecast from evidence. The rule reads the "
            "question's wording alone, was fixed before any forecast existed, and the "
            "excluded ids are part of the pinned fingerprint below (ADR-0043). The sealed "
            "registry and its manifest are unchanged.",
            "",
            "Kept as sealed, and so to be read with care: the domain labels were "
            "keyword-matched over each question *and* its resolution boilerplate, so "
            "some are wrong -- the `health` label in particular was assigned on the "
            'pronoun "who" -- which loosens the 25% domain cap and makes the '
            "per-domain breakdown indicative only. ADR-0043 lists both kept defects.",
            "",
        ]
    lines += [
        f"The {split.n} scored scenarios are partitioned into **{len(split.dev)} dev** "
        f"and **{len(split.test)} test**. The partition was declared before any "
        "forecast existed: membership is a keyed hash of the scenario id under the "
        f"study salt (purpose `{split.purpose}`), stratified by domain, and it takes "
        "no outcome, forecast or error as input. Its fingerprint is pinned in "
        "`configs/base.yaml` and every evaluation path refuses to run if the "
        "recomputed split differs.",
        "",
        f"- Split sha256: `{split.sha256}`",
        f"- Applies to scenario manifest: `{artifact.manifest_sha256}`",
        f"- This report's figures are measured on: **{artifact.partition}**",
        "",
        "**Tuning is only ever legitimate on dev.** Every choice made by looking at "
        "accuracy -- evidence chunks per agent, a prompt edit, a blend weight -- is "
        "made on the dev scenarios, and the headline below is computed on test "
        "scenarios alone, so none of those choices can inflate it.",
        "",
        "| Domain | n | dev | test |",
        "|---|---|---|---|",
    ]
    lines += [f"| {row.domain} | {row.n} | {row.dev} | {row.test} |" for row in split.domains]
    lines += [""]
    if artifact.split_interactions:
        lines += [
            "Two other subsets of the scenarios are drawn by keyed hash, each "
            "independently of this split. Their overlap with it sets the paired n of "
            "every capped-cell comparison and the fitting n of every recalibrated "
            "figure, so it is stated:",
            "",
            "| Partition | n | in the ablation subsample | recalibration fit / held-out |",
            "|---|---|---|---|",
        ]
        lines += [
            f"| {row.partition} | {row.n} | {row.ablation_subsample} | "
            f"{row.recalibration_fit} / {row.recalibration_held} |"
            for row in artifact.split_interactions
        ]
        lines += [
            "",
            "Isotonic recalibration is fitted and scored inside one partition at a "
            "time; a fit never crosses the dev/test boundary.",
            "",
        ]
    if artifact.withheld_configs:
        lines += [
            f"{len(artifact.withheld_configs)} stored configuration(s) are not declared "
            "study configurations and were **not scored on this partition**: "
            f"{', '.join(artifact.withheld_configs)}. {WITHHELD_RULE}",
            "",
        ]
    return lines


def _partition_banner(artifact: StudyArtifact) -> list[str]:
    """Say loudly when the lead figure is not a held-out one."""
    if artifact.split is None or artifact.partition == "test":
        return []
    if artifact.partition == "dev":
        return [
            "> **This report is written on the DEV partition.** These are the numbers "
            "tuning is allowed to look at. They are optimistic by construction and "
            "none of them is a result about Cascade; the result is `cascade report` "
            "on the test partition.",
            "",
        ]
    return [
        "> **This report is written on ALL scenarios, dev and test together.** The "
        "lead figure contains the scenarios the system was tuned on. The held-out "
        "figure is the `test` row of the table below.",
        "",
    ]


def _partition_table(artifact: StudyArtifact) -> list[str]:
    """The headline configuration on every partition, each row labelled."""
    if not artifact.headline_partitions:
        return []
    lines = ["| Partition | n | Brier | What it is |", "|---|---|---|---|"]
    for partition, measured in artifact.headline_partitions:
        label = f"**{partition}**" if partition == artifact.partition else partition
        lines.append(
            f"| {label} | {'-' if measured is None else measured.n} | "
            f"{_fmt(None if measured is None else measured.brier, '{:.6f}')} | "
            f"{_PARTITION_NOTE[partition]} |"
        )
    return [*lines, ""]


def _evidence_section(artifact: StudyArtifact) -> list[str]:
    """Accuracy by evidence quality: the table, the one test, and the caveat."""
    finding = artifact.evidence
    if finding is None:
        return []
    lines = [
        "## Accuracy by evidence quality",
        "",
        f"Scenarios tiered by the number of corpus chunks published in the final "
        f"{finding.window_days} days before their cutoff. The tiers are fixed chunk "
        "thresholds declared before any forecast existed, not quantiles, so a "
        "scenario's tier cannot move unless its evidence does. Measured on the "
        f"{artifact.partition} partition, {finding.n} scenarios.",
        "",
        "| Tier | chunks in window | n | base rate | Brier |",
        "|---|---|---|---|---|",
    ]
    for row in finding.tiers:
        bounds = f"{row.lo:,}+" if row.hi is None else f"{row.lo:,}-{row.hi:,}"
        if row.lo == row.hi:
            bounds = f"{row.lo:,}"
        lines.append(
            f"| {row.tier} | {bounds} | {row.n} | {_fmt(row.base_rate, '{:.4f}')} | "
            f"{_fmt(row.brier, '{:.6f}')} |"
        )
    lines += [
        "",
        f"The one pre-declared test: Spearman rho between the chunk count and the "
        f"per-scenario squared error is {_fmt(finding.spearman_rho, '{:.4f}')} "
        f"(permutation p {_fmt(finding.spearman_p, '{:.4g}')}). Negative means more "
        "evidence went with smaller error.",
        "",
        "Evidence volume is not assigned at random: it rises with the cutoff year and "
        "differs by domain, and both move forecast difficulty too. This table "
        "describes where the system does well. It does not estimate what more "
        "evidence would do.",
        "",
    ]
    return lines


def _split_payload(artifact: StudyArtifact) -> dict[str, Any] | None:
    """The split, machine-readable, with the dev ids so it can be re-derived.

    Ids and counts only. Listing the dev scenarios discloses nothing about an
    outcome, and it is what lets someone holding only the artifact confirm that
    the scenarios tuned on are the ones this report excluded.
    """
    split = artifact.split
    if split is None:
        return None
    return {
        "sha256": split.sha256,
        "purpose": split.purpose,
        "declared_before_any_forecast": True,
        "applies_to_manifest_sha256": artifact.manifest_sha256,
        "n_dev": len(split.dev),
        "n_test": len(split.test),
        "dev_scenario_ids": list(split.dev),
        "excluded": [item.model_dump(mode="json") for item in split.excluded],
        "domains": [row.model_dump(mode="json") for row in split.domains],
        "interactions": [row.model_dump(mode="json") for row in artifact.split_interactions],
        "configs_withheld_from_this_partition": list(artifact.withheld_configs),
        "configs_withheld_rule": WITHHELD_RULE,
    }


def _withheld_payload(artifact: StudyArtifact) -> dict[str, Any]:
    """Which configurations this partition did not score, and under what rule.

    A list of ids says what happened; it does not say why, and a reader who
    finds a stored configuration missing from the metrics has no way to tell a
    policy from an omission.
    """
    return {
        "configs": list(artifact.withheld_configs),
        "partition": artifact.partition,
        "rule": WITHHELD_RULE,
    }


def write_report(artifact: StudyArtifact, *, root: Path) -> Path:
    """Write the Appendix D directory and return its path.

    Overwrites its own directory rather than appending, so a re-run of a report
    with the same id is idempotent -- but the id carries a minute-resolution
    timestamp, so two genuinely different runs land in different directories
    and neither silently replaces the other.
    """
    directory = Path(root) / artifact.report_id
    figures = directory / "figures"
    figures.mkdir(parents=True, exist_ok=True)
    # Drawn first, so `headline.md` indexes the directory that exists rather
    # than the one the writer intended.
    drawn = _write_figures(artifact, figures)

    (directory / "manifest.json").write_text(
        _json(
            {
                "report_id": artifact.report_id,
                "written_at": artifact.written_at.isoformat(),
                "manifest_sha256": artifact.manifest_sha256,
                "git_sha": artifact.git_sha,
                "headline_config": artifact.headline_config,
                "sealed_set": {
                    "n_scenarios": artifact.n_scenarios_sealed,
                    "base_rate": artifact.base_rate,
                    "climatology_brier": artifact.climatology_brier,
                },
                "study_salt": artifact.study_salt,
                "models": dict(artifact.models),
                "config": dict(artifact.config_snapshot),
                "figure_format": FIGURE_FORMAT,
                "figure_format_note": (
                    "Appendix D names .png; the pinned stack has no plotting library "
                    "and figures are SVG written from it. See ADR-0024."
                ),
                "replicate_policy": artifact.replicate_policy,
                "not_produced": list(artifact.blocked),
                "partition": artifact.partition,
                "split": _split_payload(artifact),
                "provenance": dict(artifact.provenance),
                "figures": [
                    {"filename": note.filename, "written": note.written, "note": note.note}
                    for note in drawn
                ],
            }
        ),
        encoding="utf-8",
    )

    (directory / "headline.md").write_text(_headline_markdown(artifact, drawn), encoding="utf-8")

    (directory / "metrics.json").write_text(
        _json(
            {
                "partition": artifact.partition,
                "withheld": _withheld_payload(artifact),
                "headline_by_partition": [
                    {
                        "partition": partition,
                        "is_headline": partition == artifact.partition,
                        "metrics": (
                            None
                            if measured is None
                            else _metric_payload(measured, coverage=artifact.market_coverage)
                        ),
                    }
                    for partition, measured in artifact.headline_partitions
                ],
                "evidence": (
                    None if artifact.evidence is None else artifact.evidence.model_dump(mode="json")
                ),
                "configs": [
                    _metric_payload(item, coverage=artifact.market_coverage)
                    for item in artifact.metrics
                ],
                "baselines": [
                    {
                        "baseline_id": baseline_id,
                        "name": name,
                        "config_id": config_id,
                        "metrics": (
                            None
                            if metrics is None
                            else _metric_payload(metrics, coverage=artifact.market_coverage)
                        ),
                    }
                    for baseline_id, name, config_id, metrics in artifact.baselines
                ],
                "cells": [
                    {
                        "cell_id": cell.cell_id,
                        "config_id": cell.config_id,
                        "decomposition": cell.decomposition,
                        "information_asymmetry": cell.information_asymmetry,
                        "grounding": cell.grounding,
                        "replicates_design": cell.replicates_design,
                        "replicates_executed": cell.replicates_executed,
                        "scenarios_executed": cell.scenarios_executed,
                        "role": cell.role,
                        "metrics": None if cell.metrics is None else _metric_payload(cell.metrics),
                    }
                    for cell in artifact.cells
                ],
                "convergence": [
                    {
                        "n": n,
                        "mean_abs_change": change,
                        "mean_sigma": sigma,
                        "scenarios": scenarios,
                    }
                    for n, change, sigma, scenarios in artifact.convergence
                ],
                "dispersion": (
                    None
                    if artifact.dispersion is None
                    else artifact.dispersion.model_dump(mode="json")
                ),
            }
        ),
        encoding="utf-8",
    )

    (directory / "baselines.csv").write_text(
        _csv(
            [list(row) for row in artifact.baseline_rows],
            ["config_id", "scenario_id", "p_hat", "outcome"],
        ),
        encoding="utf-8",
    )

    (directory / "ablation_grid.csv").write_text(
        _csv(
            [list(row) for row in artifact.grid_rows],
            ["cell_id", "scenario_id", "p_hat", "sigma", "outcome"],
        ),
        encoding="utf-8",
    )

    calibration_rows: list[list[Any]] = []
    if artifact.calibration is not None:
        calibration_rows = [
            [
                row.index,
                f"{row.lo:.2f}",
                f"{row.hi:.2f}",
                row.count,
                "" if row.mean_pred is None else f"{row.mean_pred:.6f}",
                "" if row.obs_freq is None else f"{row.obs_freq:.6f}",
                "" if row.wilson_lo is None else f"{row.wilson_lo:.6f}",
                "" if row.wilson_hi is None else f"{row.wilson_hi:.6f}",
            ]
            for row in artifact.calibration.bins
        ]
    (directory / "calibration.csv").write_text(
        _csv(
            calibration_rows,
            ["bin", "lo", "hi", "count", "mean_pred", "obs_freq", "wilson_lo", "wilson_hi"],
        ),
        encoding="utf-8",
    )

    (directory / "per_domain.csv").write_text(
        _csv(
            [
                [item.domain, item.n, f"{item.base_rate:.6f}", f"{item.brier:.6f}"]
                for item in artifact.per_domain
            ],
            ["domain", "n", "base_rate", "brier"],
        ),
        encoding="utf-8",
    )

    (directory / "per_evidence_tier.csv").write_text(
        _csv(
            [
                [
                    row.tier,
                    row.lo,
                    "" if row.hi is None else row.hi,
                    row.n,
                    "" if row.base_rate is None else f"{row.base_rate:.6f}",
                    "" if row.brier is None else f"{row.brier:.6f}",
                ]
                for row in (artifact.evidence.tiers if artifact.evidence is not None else ())
            ],
            ["tier", "chunks_lo", "chunks_hi", "n", "base_rate", "brier"],
        ),
        encoding="utf-8",
    )

    (directory / "significance.json").write_text(
        _json(
            {
                "method": {
                    "bootstrap": "paired percentile over scenarios",
                    "resample_unit": "scenario",
                    "multiple_comparison": (
                        "Holm-Bonferroni within each family; families are adjusted "
                        "separately and every comparison names its own"
                    ),
                    "family_size": len(_family(artifact, "appendix_c")),
                    "family_sizes": {
                        family: len(_family(artifact, family))
                        for family in ("appendix_c", "exploratory_dev", "supplementary")
                    },
                    "partition": artifact.partition,
                },
                "comparisons": [
                    {
                        **item.model_dump(mode="json"),
                        "reading": artifact.comparison_readings.get(item.name, ""),
                    }
                    for item in artifact.comparisons
                ],
            }
        ),
        encoding="utf-8",
    )

    (directory / "leakage_report.json").write_text(_json(dict(artifact.leakage)), encoding="utf-8")
    (directory / "cost_ledger.json").write_text(_json(dict(artifact.cost_ledger)), encoding="utf-8")

    return directory


def _write_figures(artifact: StudyArtifact, figures: Path) -> tuple[FigureNote, ...]:
    """Write whatever figures the measured data supports, and say what it did not.

    A figure with no data is not written. An empty axis in a report reads as a
    result -- "we looked and found nothing" -- when the truth is that the
    measurement did not happen. Nor is a *degenerate* one written: a single
    convergence rung is not a curve, and a sigma column with no spread plots as
    a stripe against the y axis that reads as a scatter. Both would be pictures
    of a measurement rather than of a result, so each slot returns the reason it
    is empty and `headline.md` prints it.
    """
    from cascade.eval.figures import (
        convergence_svg,
        forest_svg,
        has_spread,
        reliability_svg,
        scatter_svg,
    )

    notes: list[FigureNote] = []

    name = f"reliability.{FIGURE_FORMAT}"
    if artifact.calibration is not None and artifact.calibration.n:
        (figures / name).write_text(
            reliability_svg(
                artifact.calibration,
                title=(
                    f"Reliability — {artifact.headline_config} "
                    f"({artifact.calibration.n} scenarios, {artifact.partition})"
                ),
            ),
            encoding="utf-8",
        )
        notes.append(
            FigureNote(name, True, f"{artifact.calibration.n} scenarios, 10 bins with counts")
        )
    else:
        notes.append(
            FigureNote(
                name,
                False,
                f"`{artifact.headline_config}` has no scored forecasts on the "
                f"{artifact.partition} partition, so there is no calibration to draw",
            )
        )

    name = f"ablation_forest.{FIGURE_FORMAT}"
    ablation_rows = _family(artifact, "appendix_c")
    if ablation_rows:
        (figures / name).write_text(
            forest_svg(
                ablation_rows,
                title=(
                    f"Effect sizes against {artifact.headline_config} "
                    f"(paired bootstrap, {artifact.partition})"
                ),
            ),
            encoding="utf-8",
        )
        notes.append(FigureNote(name, True, f"{len(ablation_rows)} comparison(s) in the family"))
    else:
        notes.append(
            FigureNote(name, False, "no comparison in the Appendix C family had both sides scored")
        )

    name = f"convergence.{FIGURE_FORMAT}"
    if len(artifact.convergence) > 1:
        (figures / name).write_text(
            convergence_svg(
                [(n, change) for n, change, _, _ in artifact.convergence],
                title="Ensemble convergence (§9.3)",
            ),
            encoding="utf-8",
        )
        notes.append(FigureNote(name, True, f"{len(artifact.convergence)} rungs on the ladder"))
    elif artifact.convergence:
        notes.append(
            FigureNote(
                name,
                False,
                f"the ladder reached one rung ({artifact.convergence[0][0]} replicates); "
                "a convergence curve needs at least two to show a change",
            )
        )
    else:
        notes.append(
            FigureNote(name, False, "no scenario has enough replicates to reach the first rung")
        )

    name = f"sigma_vs_error.{FIGURE_FORMAT}"
    sigmas = [item.sigma for item in artifact.scored]
    if artifact.scored and artifact.dispersion is not None and has_spread(sigmas):
        finding = artifact.dispersion
        annotation = (
            f"Spearman rho = {_fmt(finding.spearman_rho, '{:.4f}')}, "
            f"p = {_fmt(finding.spearman_p, '{:.4g}')}"
        )
        (figures / name).write_text(
            scatter_svg(
                sigmas,
                [item.abs_error for item in artifact.scored],
                title=f"Is sigma informative? (§9.2, {artifact.partition})",
                x_label="sigma across replicates",
                y_label="absolute forecast error",
                annotation=annotation,
            ),
            encoding="utf-8",
        )
        notes.append(FigureNote(name, True, f"{len(sigmas)} scenarios"))
    elif artifact.scored and artifact.dispersion is not None:
        notes.append(
            FigureNote(
                name,
                False,
                f"every one of the {len(sigmas)} sigmas is identical, so the x axis has "
                "no spread and the chart could not relate the two",
            )
        )
    else:
        notes.append(
            FigureNote(
                name,
                False,
                f"`{artifact.headline_config}` has no scored forecasts on the "
                f"{artifact.partition} partition",
            )
        )
    return tuple(notes)
