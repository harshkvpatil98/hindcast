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
    "StudyArtifact",
    "git_sha",
    "report_id",
    "write_report",
]

FIGURE_FORMAT = "svg"


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


def _metric_payload(item: MetricSet) -> dict[str, Any]:
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


def _headline_markdown(artifact: StudyArtifact) -> str:
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

    lines += ["## Replicate policy", ""]
    lines += [
        artifact.replicate_policy
        or "No replicate policy was recorded, because no ablation cell was executed.",
        "",
    ]
    lines += [f"- {note}" for note in artifact.replicate_notes]
    lines += [""]

    lines += ["## Reading the deltas", ""]
    lines += [
        "Deltas are leave-one-out against the full configuration. Information "
        "asymmetry is nested inside causal decomposition — the visibility policy "
        "is derived from the compiled graph — so **the two leave-one-out deltas "
        "do not sum**, and the decomposition-net-of-asymmetry figure is published "
        "here rather than left for a reader to compute.",
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
            f"(sigma > {finding.sigma_threshold} or dip p < 0.05). Brier on the flagged "
            f"subset {_fmt(finding.brier_flagged, '{:.6f}')} against "
            f"{_fmt(finding.brier_unflagged, '{:.6f}')} on the rest. The claim under "
            "test is that the flag identifies the forecasts to distrust; the numbers "
            "above are what it measured, whichever way they fell.",
            "",
        ]

    lines += [
        "## Artifact",
        "",
        "| File | Contents |",
        "|---|---|",
        "| `manifest.json` | scenario hash, git sha, model versions, config |",
        "| `metrics.json` | every metric, every cell, machine-readable |",
        "| `baselines.csv` | the §10.2 baselines and the market at the cutoff, per scenario |",
        "| `ablation_grid.csv` | the twelve Appendix C cells, per scenario |",
        "| `calibration.csv` | 10 bins: count, mean_pred, obs_freq, Wilson bounds |",
        "| `per_domain.csv` | Brier by domain with counts (sealed labels; see ADR-0043) |",
        "| `per_evidence_tier.csv` | Brier by evidence tier with counts |",
        "| `significance.json` | paired bootstrap CIs, Holm-adjusted p-values |",
        "| `leakage_report.json` | poison-pill hits and memorisation scores |",
        "| `cost_ledger.json` | per-phase spend |",
        f"| `figures/*.{FIGURE_FORMAT}` | reliability, forest, convergence, sigma vs error |",
        "",
    ]
    return "\n".join(lines)


def _fmt(value: float | None, pattern: str) -> str:
    """Format a measurement, or say plainly that there is not one."""
    return "not measured" if value is None else pattern.format(value)


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
            f"{', '.join(artifact.withheld_configs)}. A tuning variant is scored on dev "
            "only; declaring it in code is the price of reporting it on test.",
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
            }
        ),
        encoding="utf-8",
    )

    (directory / "headline.md").write_text(_headline_markdown(artifact), encoding="utf-8")

    (directory / "metrics.json").write_text(
        _json(
            {
                "partition": artifact.partition,
                "headline_by_partition": [
                    {
                        "partition": partition,
                        "is_headline": partition == artifact.partition,
                        "metrics": None if measured is None else _metric_payload(measured),
                    }
                    for partition, measured in artifact.headline_partitions
                ],
                "evidence": (
                    None if artifact.evidence is None else artifact.evidence.model_dump(mode="json")
                ),
                "configs": [_metric_payload(item) for item in artifact.metrics],
                "baselines": [
                    {
                        "baseline_id": baseline_id,
                        "name": name,
                        "config_id": config_id,
                        "metrics": None if metrics is None else _metric_payload(metrics),
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

    _write_figures(artifact, figures)
    return directory


def _write_figures(artifact: StudyArtifact, figures: Path) -> None:
    """Write whatever figures the measured data supports, and only those.

    A figure with no data is not written. An empty axis in a report reads as a
    result -- "we looked and found nothing" -- when the truth is that the
    measurement did not happen, and `headline.md` already says which.
    """
    from cascade.eval.figures import convergence_svg, forest_svg, reliability_svg, scatter_svg

    if artifact.calibration is not None and artifact.calibration.n:
        (figures / f"reliability.{FIGURE_FORMAT}").write_text(
            reliability_svg(
                artifact.calibration,
                title=f"Reliability — {artifact.headline_config} ({artifact.calibration.n} scenarios)",
            ),
            encoding="utf-8",
        )
    ablation_rows = _family(artifact, "appendix_c")
    if ablation_rows:
        (figures / f"ablation_forest.{FIGURE_FORMAT}").write_text(
            forest_svg(ablation_rows, title="Ablation effect sizes (paired bootstrap)"),
            encoding="utf-8",
        )
    if artifact.convergence:
        (figures / f"convergence.{FIGURE_FORMAT}").write_text(
            convergence_svg(
                [(n, change) for n, change, _, _ in artifact.convergence],
                title="Ensemble convergence (§9.3)",
            ),
            encoding="utf-8",
        )
    if artifact.scored and artifact.dispersion is not None:
        finding = artifact.dispersion
        annotation = (
            f"Spearman rho = {_fmt(finding.spearman_rho, '{:.4f}')}, "
            f"p = {_fmt(finding.spearman_p, '{:.4g}')}"
        )
        (figures / f"sigma_vs_error.{FIGURE_FORMAT}").write_text(
            scatter_svg(
                [item.sigma for item in artifact.scored],
                [item.abs_error for item in artifact.scored],
                title="Is sigma informative? (§9.2)",
                x_label="sigma across replicates",
                y_label="absolute forecast error",
                annotation=annotation,
            ),
            encoding="utf-8",
        )
