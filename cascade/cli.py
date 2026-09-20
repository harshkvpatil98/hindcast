"""Typer application. Every phase of the study is a subcommand.

There are no notebook-driven pipelines (spec §13): if it is a step in the
study, it is reachable from here and therefore scriptable, resumable and
CI-checkable.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from decimal import Decimal
from importlib import metadata
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal, NoReturn

import typer
from rich.console import Console
from rich.table import Table

from cascade.config import Settings, env_file_path, load_settings, repo_root
from cascade.eval.split import SplitError
from cascade.llm.types import BudgetExceeded, CacheMiss, PromptTooShortToCache, ProviderNotReady

if TYPE_CHECKING:  # pragma: no cover -- types only, never imported at startup
    from cascade.eval.ablation import CellSpec
    from cascade.eval.schema import MetricSet, ScoredForecast
    from cascade.eval.split import Partition, SplitDeclaration
    from cascade.eval.store import FrozenSplit

from cascade.trace.replay import CHILD_HASH_SEED
from cascade.version import (
    EXIT_BUDGET_BREACH,
    EXIT_CACHE_MISS,
    EXIT_ERROR,
    EXIT_OK,
    EXIT_PRECONDITION,
    PINNED_STACK,
)

app = typer.Typer(
    name="cascade",
    help="Multi-agent causal simulation for strategic forecasting.",
    no_args_is_help=True,
    add_completion=False,
)
dev_app = typer.Typer(help="Self-test hooks. Not part of the study pipeline.", hidden=True)
app.add_typer(dev_app, name="dev")
db_app = typer.Typer(help="Forward-only schema migrations.", no_args_is_help=True)
app.add_typer(db_app, name="db")
corpus_app = typer.Typer(
    help="Build, inspect and verify the evidence corpus (M2).", no_args_is_help=True
)
app.add_typer(corpus_app, name="corpus")
ledger_app = typer.Typer(
    help="Build, seal and verify the scenario registry (M1).", no_args_is_help=True
)
app.add_typer(ledger_app, name="ledger")
retrieval_app = typer.Typer(
    help="Chronofence: index, benchmark and verify time-locked retrieval (M3).",
    no_args_is_help=True,
)
app.add_typer(retrieval_app, name="retrieval")
compile_app = typer.Typer(
    help="Lathe: compile scenarios into typed causal graphs (M4).", no_args_is_help=True
)
app.add_typer(compile_app, name="compile")
simulate_app = typer.Typer(
    help="Loom + Aperture: run seeded simulations of a compiled scenario (M5/M6).",
    no_args_is_help=True,
)
app.add_typer(simulate_app, name="simulate")
ensemble_app = typer.Typer(
    help="Chorus: collapse replicates into forecasts and report dispersion (M6).",
    no_args_is_help=True,
)
app.add_typer(ensemble_app, name="ensemble")
eval_app = typer.Typer(
    help="Assay: score forecasts, run the ablation grid, test significance (M7).",
    no_args_is_help=True,
)
app.add_typer(eval_app, name="eval")
trace_app = typer.Typer(
    help="Strata: replay determinism, provenance chains and the cost ledger (M8).",
    no_args_is_help=True,
)
app.add_typer(trace_app, name="trace")

console = Console()
err_console = Console(stderr=True)

OverlayOpt = Annotated[
    str | None,
    typer.Option("--config", help="Ablation overlay name from configs/ablations/."),
]


def _settings(overlay: str | None) -> Settings:
    return load_settings(overlay)


def _fail(message: str, code: int) -> NoReturn:
    """Print an error and exit with ``code``.

    Typed ``NoReturn`` so a caller's later code is unreachable to the type
    checker too: a guard that raises but is typed as returning None leaves
    every value it was guarding still optional at the call site.
    """
    err_console.print(f"[bold red]error[/bold red] {message}")
    raise typer.Exit(code)


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


def _dep_version(dist: str) -> str | None:
    try:
        return metadata.version(dist)
    except metadata.PackageNotFoundError:
        return None


def _provider_rows(table: Table, settings: Settings) -> bool:
    """Describe the active model provider; fail only where it would spend.

    Preserves the invariant that `doctor` never costs anything: it reports
    what a record run would do without making a model call. An unready
    provider fails the check only in record or live mode -- replay reaches no
    provider, and demanding one there would break the keyless demo path.
    """
    from cascade.llm.claude_cli import UNCONTROLLED_FIELDS
    from cascade.llm.providers import endpoint, readiness_problems, spec_for

    provider = settings.llm.provider
    spec = spec_for(provider)
    ok_mark, no_mark, info_mark = "[green]OK[/green]", "[red]NO[/red]", "[yellow]--[/yellow]"
    table.add_row("llm provider", "anthropic|aws|bedrock|claude_code", provider, ok_mark)
    table.add_row("  operated by", "", spec.operated_by, ok_mark)
    table.add_row(
        "  batches",
        "needed by simulate",
        "yes" if spec.supports_batches else "no -- batched phases refuse (exit 3)",
        ok_mark if spec.supports_batches else info_mark,
    )
    table.add_row(
        "  cache namespace",
        "ADR-0029 / ADR-0031",
        spec.cache_namespace or "shared with the API providers",
        ok_mark,
    )
    table.add_row(
        "  billing",
        "",
        "per token" if spec.billing == "per_token" else "subscription (ledger books $0)",
        ok_mark,
    )
    route = endpoint(settings)
    if route is not None:
        table.add_row("  endpoint", "explicit, never ambient", route, ok_mark)

    healthy = True
    if provider == "claude_code":
        executable = settings.providers.claude_code.executable
        found = _tool_version(executable, ["--version"])
        table.add_row(
            "  claude cli", executable, found or "not found", ok_mark if found else no_mark
        )
        table.add_row(
            "  not controllable",
            "recorded under its own namespace",
            ", ".join(UNCONTROLLED_FIELDS),
            info_mark,
        )
        healthy = found is not None or settings.llm.mode == "replay"

    problems = readiness_problems(
        settings, models=(settings.models.agent, settings.models.compiler)
    )
    spending = settings.llm.mode != "replay"
    if problems:
        table.add_row(
            "  ready to record",
            "routed + priced",
            "; ".join(problems),
            no_mark if spending else info_mark,
        )
        healthy = healthy and not spending
    else:
        table.add_row("  ready to record", "routed + priced", "yes", ok_mark)
    return healthy


def _tool_version(binary: str, args: list[str]) -> str | None:
    path = shutil.which(binary)
    if path is None:
        return None
    try:
        out = subprocess.run(  # noqa: S603 -- fixed argv, no shell
            [path, *args], capture_output=True, text=True, timeout=15, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip().splitlines()[0] if out.stdout.strip() else None


def _postgres_status(settings: Settings) -> tuple[bool, str]:
    try:
        import psycopg
    except ImportError:
        return False, "psycopg not installed"
    try:
        with (
            psycopg.connect(settings.database_url("admin"), connect_timeout=5) as conn,
            conn.cursor() as cur,
        ):
            cur.execute("SHOW server_version")
            server_row = cur.fetchone()
            cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
            vec_row = cur.fetchone()
    except Exception as exc:  # noqa: BLE001 -- reported, not swallowed
        return False, f"unreachable: {type(exc).__name__}: {exc}".replace("\n", " ")[:120]
    server = server_row[0] if server_row else "unknown"
    vector = vec_row[0] if vec_row else "extension not installed"
    return True, f"PostgreSQL {server}, pgvector {vector}"


def _langfuse_status(settings: Settings) -> tuple[bool, str]:
    if not settings.langfuse.enabled:
        return True, "disabled in config"
    try:
        import httpx
    except ImportError:
        return False, "httpx not installed"
    url = settings.langfuse.host.rstrip("/") + "/api/public/health"
    try:
        response = httpx.get(url, timeout=5.0)
    except Exception as exc:  # noqa: BLE001 -- reported, not swallowed
        return False, f"unreachable: {type(exc).__name__}"
    if response.status_code != 200:
        return False, f"HTTP {response.status_code}"
    return True, f"healthy at {settings.langfuse.host}"


@app.command()
def doctor(
    config: OverlayOpt = None,
    offline: Annotated[
        bool, typer.Option("--offline", help="Skip Postgres and Langfuse service checks.")
    ] = False,
) -> None:
    """Verify the toolchain and pinned stack. Exits 0 only when everything checks out."""
    ok = True
    settings = _settings(config)

    # overflow="fold" rather than Rich's default ellipsis: the acceptance
    # criterion is that doctor *prints* every pinned version, and a version
    # string silently cut to "0.12.3 (Homebrew 2026-08-0…" does not satisfy it
    # on a narrow terminal. Folding wraps instead of discarding.
    table = Table(title="Cascade doctor", show_lines=False)
    table.add_column("Component", style="cyan", overflow="fold")
    table.add_column("Pinned", style="dim", overflow="fold")
    table.add_column("Found", overflow="fold")
    table.add_column("", width=3)

    py = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    py_ok = sys.version_info[:2] == (3, 12)
    ok &= py_ok
    table.add_row("python", "3.12.x", py, "[green]OK[/green]" if py_ok else "[red]NO[/red]")

    for entry in PINNED_STACK:
        found = _dep_version(entry.distribution)
        present = found is not None
        if not present and entry.required:
            ok = False
        status = (
            "[green]OK[/green]"
            if present
            else ("[red]NO[/red]" if entry.required else "[yellow]--[/yellow]")
        )
        table.add_row(
            entry.label,
            entry.pin,
            found or f"not installed ({entry.extra})" if not present else found or "",
            status,
        )

    for label, binary, args in (
        ("uv", "uv", ["--version"]),
        ("docker", "docker", ["--version"]),
    ):
        found = _tool_version(binary, args)
        present = found is not None
        ok &= present
        table.add_row(
            label,
            "pinned",
            found or "not found",
            "[green]OK[/green]" if present else "[red]NO[/red]",
        )

    table.add_row("agent model", settings.models.agent, settings.models.agent, "[green]OK[/green]")
    table.add_row(
        "compiler model", settings.models.compiler, settings.models.compiler, "[green]OK[/green]"
    )
    table.add_row("llm mode", "record|replay|live", settings.llm.mode, "[green]OK[/green]")
    ok &= _provider_rows(table, settings)

    if not offline:
        pg_ok, pg_msg = _postgres_status(settings)
        ok &= pg_ok
        table.add_row(
            "postgres",
            "16 + pgvector 0.8",
            pg_msg,
            "[green]OK[/green]" if pg_ok else "[red]NO[/red]",
        )
        lf_ok, lf_msg = _langfuse_status(settings)
        ok &= lf_ok
        table.add_row(
            "langfuse", "self-hosted", lf_msg, "[green]OK[/green]" if lf_ok else "[red]NO[/red]"
        )

    console.print(table)

    cache = settings.cache_path()
    console.print(f"llm cache: {cache} ({'exists' if cache.is_dir() else 'not yet created'})")

    if not ok:
        _fail("one or more checks failed (see table above)", EXIT_PRECONDITION)
    console.print("[bold green]doctor: all checks passed[/bold green]")


# ---------------------------------------------------------------------------
# Phase subcommands. Stubbed at M0; each lands in its own milestone.
# ---------------------------------------------------------------------------


def _not_yet(name: str, milestone: str) -> None:
    err_console.print(
        f"[yellow]{name} is not implemented yet[/yellow] -- lands at {milestone}. "
        "Milestones are executed strictly in order (spec §14.1)."
    )
    raise typer.Exit(EXIT_PRECONDITION)


@app.command()
def evaluate(config: OverlayOpt = None) -> None:
    """Deprecated alias. The evaluation harness is `cascade eval` (M7).

    Kept as a command rather than deleted because the spec's §13 command list
    names it, and a removed subcommand fails with click's "no such command",
    which does not say where the thing went.
    """
    err_console.print(
        "[yellow]`cascade evaluate` is now `cascade eval`[/yellow] -- try "
        "`cascade eval status`, `cascade eval score`, `cascade eval grid`, "
        "`cascade eval significance`, or `cascade report`."
    )
    raise typer.Exit(EXIT_PRECONDITION)


# ---------------------------------------------------------------------------
# db: migrations
# ---------------------------------------------------------------------------


@db_app.command("migrate")
def db_migrate(
    config: OverlayOpt = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="List pending migrations without applying them.")
    ] = False,
) -> None:
    """Apply every pending migration, in order."""
    from cascade.db import MigrationError, apply_all, pending

    settings = _settings(config)
    try:
        todo = pending(settings)
        if not todo:
            console.print("[green]schema is up to date[/green]")
            return
        if dry_run:
            for migration in todo:
                console.print(f"pending: {migration.name}")
            return
        applied = apply_all(settings)
    except MigrationError as exc:
        _fail(str(exc), EXIT_PRECONDITION)
    for migration in applied:
        console.print(f"[green]applied[/green] {migration.name}")


@db_app.command("enable-iam")
def db_enable_iam(config: OverlayOpt = None) -> None:
    """Switch the two application roles to IAM authentication (ADR-0034).

    An explicit operator step, never a migration: on RDS, granting `rds_iam`
    to a role **disables its password**, so doing it automatically would lock
    out every client still configured with one. Run this, then set
    `database.auth: iam`. To go back: `REVOKE rds_iam FROM <role>`.
    Exits 3 anywhere `rds_iam` does not exist -- that is, anywhere but RDS.
    """
    import psycopg
    from psycopg import sql

    settings = _settings(config)
    roles = (settings.database.sim_user, settings.database.eval_user)
    with (
        psycopg.connect(settings.database_url("admin"), connect_timeout=10) as conn,
        conn.cursor() as cur,
    ):
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = 'rds_iam'")
        if cur.fetchone() is None:
            _fail(
                "this server has no `rds_iam` role, so it is not RDS or Aurora; IAM database "
                "authentication does not exist here",
                EXIT_PRECONDITION,
            )
        for role in roles:
            cur.execute(sql.SQL("GRANT rds_iam TO {}").format(sql.Identifier(role)))
        conn.commit()
    console.print(
        f"granted rds_iam to {', '.join(roles)}. Their passwords no longer work: set "
        "database.auth to `iam` (CASCADE_DATABASE__AUTH=iam) before the next connection."
    )


@db_app.command("status")
def db_status(config: OverlayOpt = None) -> None:
    """Show which migrations are applied and which are pending."""
    from cascade.db import applied_versions, discover

    settings = _settings(config)
    try:
        already = applied_versions(settings)
    except Exception as exc:  # noqa: BLE001 -- reported, not swallowed
        _fail(f"cannot reach database: {exc}", EXIT_PRECONDITION)

    table = Table(title="Migrations")
    table.add_column("Version", style="cyan")
    table.add_column("File")
    table.add_column("State")
    for migration in discover():
        recorded = already.get(migration.version)
        if recorded is None:
            state = "[yellow]pending[/yellow]"
        elif recorded != migration.checksum:
            state = "[red]CHANGED SINCE APPLIED[/red]"
        else:
            state = "[green]applied[/green]"
        table.add_row(migration.version, migration.name, state)
    console.print(table)


# ---------------------------------------------------------------------------
# corpus: the evidence corpus (M2)
# ---------------------------------------------------------------------------


def _print_corpus_stats(stats: Any, target: int) -> None:
    """Print measured corpus figures. Measured, never targeted."""
    table = Table(title="Evidence corpus")
    table.add_column("Quantity", style="cyan")
    table.add_column("Measured", justify="right")
    table.add_column("Required", justify="right")
    table.add_column("", width=3)

    def row(name: str, measured: str, required: str, ok: bool) -> None:
        table.add_row(name, measured, required, "[green]OK[/green]" if ok else "[red]NO[/red]")

    row("chunks", f"{stats.n_chunks:,}", f">= {target:,}", stats.n_chunks >= target)
    row("documents", f"{stats.n_documents:,}", "-", True)
    row("NULL published_at", str(stats.null_dates), "0", stats.null_dates == 0)
    row("future published_at", str(stats.future_dates), "0", stats.future_dates == 0)
    row(
        "embedding coverage",
        f"{stats.embedding_coverage:.4f}",
        "1.0000",
        stats.missing_embeddings == 0 and stats.n_chunks > 0,
    )
    console.print(table)

    per_source = Table(title="Per-source breakdown")
    per_source.add_column("Source", style="cyan")
    per_source.add_column("documents", justify="right")
    per_source.add_column("chunks", justify="right")
    for name, documents, chunks in stats.per_source:
        per_source.add_row(name, f"{documents:,}", f"{chunks:,}")
    console.print(per_source)
    console.print(f"date range: {stats.earliest} .. {stats.latest}")


@corpus_app.command("build")
def corpus_build(
    config: OverlayOpt = None,
    source: Annotated[
        list[str] | None,
        typer.Option("--source", help="Restrict to these sources. Repeatable."),
    ] = None,
    max_units: Annotated[
        int | None,
        typer.Option("--max-units", help="Cap units per source this run. Resumable."),
    ] = None,
) -> None:
    """Ingest, dedupe, chunk and embed evidence (M2).

    Resumable: units already recorded done are skipped, so an interrupted
    ingest continues rather than restarting.
    """
    from datetime import UTC, datetime

    from cascade.corpus.pipeline import run_ingest
    from cascade.corpus.store import corpus_stats

    settings = _settings(config)
    selected = tuple(source) if source else None
    report = run_ingest(
        settings,
        now=datetime.now(UTC),
        sources=selected,
        max_units_per_source=max_units,
    )

    table = Table(title="Ingest run")
    table.add_column("Source", style="cyan")
    table.add_column("units", justify="right")
    table.add_column("skipped", justify="right")
    table.add_column("failed", justify="right")
    table.add_column("docs", justify="right")
    table.add_column("chunks", justify="right")
    table.add_column("collapsed", justify="right")
    table.add_column("dropped", overflow="fold")
    for source_report in report.sources:
        dropped = ", ".join(
            f"{reason}={count}" for reason, count in sorted(source_report.dropped.items())
        )
        table.add_row(
            source_report.source,
            str(source_report.units_done),
            str(source_report.units_skipped),
            str(source_report.units_failed),
            f"{source_report.documents_written:,}",
            f"{source_report.chunks_written:,}",
            str(source_report.collapsed),
            dropped or source_report.detail,
        )
    console.print(table)
    console.print(
        f"dedupe collapse ratio: [bold]{report.collapse_ratio:.4f}[/bold] "
        f"({report.collapsed:,} collapsed of {report.collapsed + report.documents_written:,} kept+collapsed)"
    )

    stats = corpus_stats(settings)
    _print_corpus_stats(stats, settings.corpus.target_chunks)

    if report.stopped == "ceiling":
        console.print(
            f"[green]stopped at the work ceiling[/green]: {report.stop_detail}. Judge the corpus "
            "with `cascade corpus coverage`; raise corpus.max_chunks and re-run to go deeper."
        )
    elif report.stopped == "disk":
        # Exit 3, not 0: a supervising script must not read a build that ran
        # out of room as a build that finished.
        _fail(
            f"stopped before the disk filled: {report.stop_detail}. Nothing is half-written; "
            "free space and re-run to resume.",
            EXIT_PRECONDITION,
        )


@corpus_app.command("status")
def corpus_status(config: OverlayOpt = None) -> None:
    """Report measured corpus size, coverage and date integrity."""
    from cascade.corpus.store import corpus_stats

    settings = _settings(config)
    _print_corpus_stats(corpus_stats(settings), settings.corpus.target_chunks)


@corpus_app.command("redate")
def corpus_redate(
    config: OverlayOpt = None,
    limit: Annotated[
        int | None, typer.Option("--limit", help="Re-date at most N finished files.")
    ] = None,
    delete_orphans: Annotated[
        bool,
        typer.Option(
            "--delete-orphans",
            help="Once every finished file is re-dated, delete CC-NEWS documents none of them holds.",
        ),
    ] = False,
) -> None:
    """Re-date stored CC-NEWS documents by when their text was fetched (ADR-0044).

    Re-reads the WARC headers of every finished CC-NEWS unit -- no chunking, no
    embedding -- and moves each stored document to the later of the date it
    states and the time Common Crawl fetched it, keeping both. Resumable per
    file; a file read twice changes nothing. Run it with the ingest stopped.
    """
    from cascade.corpus.redate import gap_summary, run_redate

    settings = _settings(config)

    def progress(index: int, total: int, unit_key: str, matched: int, moved: int) -> None:
        console.print(
            f"  [{index}/{total}] {unit_key}: {matched} documents matched, {moved} moved later"
        )

    report = run_redate(settings, limit=limit, delete_orphans=delete_orphans, progress=progress)
    summary = gap_summary(report.gap_days)
    console.print(
        f"re-dated [bold]{report.files}[/bold] file(s): {report.documents_matched} documents "
        f"matched, {report.documents_moved} moved to their fetch time"
    )
    console.print(
        "  gap between stated date and fetch, moved documents: "
        f"median {summary['median_days']:.2f} d · >1 d {summary['over_1_day']} · "
        f">7 d {summary['over_7_days']} · >30 d {summary['over_30_days']} · "
        f">180 d {summary['over_180_days']}"
    )
    if delete_orphans:
        console.print(
            f"  orphans deleted: {report.orphans_deleted} documents, {report.chunks_deleted} "
            "chunks (from interrupted units; re-fetched when the ingest resumes)"
        )


@corpus_app.command("coverage")
def corpus_coverage(
    config: OverlayOpt = None,
    worst: Annotated[
        int, typer.Option("--worst", help="How many under-covered scenarios to list.")
    ] = 15,
) -> None:
    """Measure evidence availability at each scenario's own cutoff (M2/M3).

    The chunk count says the corpus is big. This says whether it is in the
    right years: a 2026 scenario whose nearest admissible document is from
    2017 is retrieved correctly, quickly, and uselessly. Exits 3 when any
    scenario falls below `corpus.coverage_min_chunks` in its window, so a
    corpus over its size target but concentrated in the wrong span cannot
    pass as covered.
    """
    from statistics import median

    from cascade.corpus.store import scenario_coverage

    settings = _settings(config)
    corpus = settings.corpus
    rows = scenario_coverage(settings, lookback_months=corpus.coverage_lookback_months)
    if not rows:
        _fail("no scenarios in the registry -- run `cascade ledger build`", EXIT_PRECONDITION)

    under = [row for row in rows if row.under_covered(corpus.coverage_min_chunks)]
    stale = [row.staleness_days for row in rows if row.staleness_days is not None]
    windowed = [row.chunks_in_window for row in rows]

    summary = Table(title=f"Evidence coverage ({corpus.coverage_lookback_months}-month window)")
    summary.add_column("Quantity", style="cyan")
    summary.add_column("Measured", justify="right")
    summary.add_column("Required", justify="right")
    summary.add_column("")
    summary.add_row("scenarios", f"{len(rows):,}", "-", "OK")
    summary.add_row(
        "fully covered",
        f"{len(rows) - len(under):,}",
        f"{len(rows):,}",
        "OK" if not under else "SHORT",
    )
    summary.add_row(
        "median chunks in window",
        f"{int(median(windowed)):,}" if windowed else "0",
        f">= {corpus.coverage_min_chunks:,}",
        "OK" if windowed and median(windowed) >= corpus.coverage_min_chunks else "SHORT",
    )
    summary.add_row("min chunks in window", f"{min(windowed):,}" if windowed else "0", "-", "")
    summary.add_row("median staleness (days)", f"{int(median(stale)):,}" if stale else "-", "-", "")
    summary.add_row("max staleness (days)", f"{max(stale):,}" if stale else "-", "-", "")
    console.print(summary)

    if under:
        worst_first = sorted(under, key=lambda row: (row.chunks_in_window, row.scenario_id))[:worst]
        detail = Table(title=f"Under-covered scenarios ({len(under)} of {len(rows)})")
        detail.add_column("Scenario", style="cyan", overflow="fold")
        detail.add_column("cutoff")
        detail.add_column("in window", justify="right")
        detail.add_column("before cutoff", justify="right")
        detail.add_column("latest admissible")
        detail.add_column("stale (days)", justify="right")
        for row in worst_first:
            detail.add_row(
                row.scenario_id,
                row.cutoff_ts.date().isoformat(),
                f"{row.chunks_in_window:,}",
                f"{row.chunks_before_cutoff:,}",
                str(row.latest_admissible) if row.latest_admissible else "none",
                str(row.staleness_days) if row.staleness_days is not None else "-",
            )
        console.print(detail)
        _fail(
            f"{len(under)} of {len(rows)} scenarios have fewer than "
            f"{corpus.coverage_min_chunks:,} admissible chunks within "
            f"{corpus.coverage_lookback_months} months of their cutoff",
            EXIT_PRECONDITION,
        )
    console.print("[bold green]corpus: every scenario is covered at its cutoff[/bold green]")


@corpus_app.command("verify")
def corpus_verify(config: OverlayOpt = None) -> None:
    """Assert the corpus invariants over the full table (spec §3.2).

    Exits 3 on any violation. These are leakage invariants, so they are
    asserted over every row rather than a sample: a sampled check on a
    leakage rule is a check that can miss the row that matters.
    """
    from cascade.corpus.store import corpus_stats

    settings = _settings(config)
    stats = corpus_stats(settings)
    _print_corpus_stats(stats, settings.corpus.target_chunks)

    failures: list[str] = []
    if stats.n_chunks == 0:
        failures.append("corpus is empty")
    if stats.null_dates:
        failures.append(f"{stats.null_dates} documents have a NULL published_at")
    if stats.future_dates:
        failures.append(f"{stats.future_dates} documents are dated in the future")
    if stats.missing_embeddings:
        failures.append(f"{stats.missing_embeddings} chunks have no embedding")
    if stats.n_chunks < settings.corpus.target_chunks:
        failures.append(
            f"{stats.n_chunks:,} chunks is short of the {settings.corpus.target_chunks:,} target"
        )

    if failures:
        _fail("; ".join(failures), EXIT_PRECONDITION)
    console.print("[bold green]corpus: all invariants hold[/bold green]")


# ---------------------------------------------------------------------------
# ledger: the scenario registry (M1)
# ---------------------------------------------------------------------------


def _print_build_report(result: Any) -> None:
    """Print the measured composition of a build. Measured, never targeted."""
    from cascade.ledger.registry import summarise

    summary = summarise(result)
    report = result.selection.report

    sources = Table(title="Sources")
    sources.add_column("Source", style="cyan")
    sources.add_column("Status")
    sources.add_column("Raw", justify="right")
    sources.add_column("Detail", overflow="fold")
    for status in result.sources:
        sources.add_row(
            status.name,
            "[green]ok[/green]" if status.available else "[red]unavailable[/red]",
            str(status.n_raw),
            status.detail,
        )
    console.print(sources)

    composition = Table(title="Selected set")
    composition.add_column("Quantity", style="cyan")
    composition.add_column("Measured", justify="right")
    composition.add_column("Required", justify="right")
    composition.add_column("", width=3)

    def row(name: str, measured: str, required: str, ok: bool) -> None:
        composition.add_row(
            name, measured, required, "[green]OK[/green]" if ok else "[red]NO[/red]"
        )

    settings_ledger = _settings(None).ledger
    row(
        "scenarios",
        str(report.n_selected),
        str(report.target_n),
        report.met_target,
    )
    row(
        "resolved-YES rate",
        f"{report.yes_rate:.4f}",
        f"[{settings_ledger.yes_rate_min:.2f}, {settings_ledger.yes_rate_max:.2f}]",
        report.n_selected > 0
        and settings_ledger.yes_rate_min <= report.yes_rate <= settings_ledger.yes_rate_max,
    )
    row(
        "max domain share",
        f"{report.max_domain_share:.4f}",
        f"<= {settings_ledger.max_domain_share:.2f}",
        report.max_domain_share <= settings_ledger.max_domain_share,
    )
    console.print(composition)

    histogram = Table(title="Domain histogram")
    histogram.add_column("Domain", style="cyan")
    histogram.add_column("n", justify="right")
    histogram.add_column("share", justify="right")
    for domain, count in report.domain_counts:
        share = count / report.n_selected if report.n_selected else 0.0
        histogram.add_row(domain, str(count), f"{share:.3f}")
    console.print(histogram)

    rejects = Table(title="Rejections by rule")
    rejects.add_column("Reason", style="cyan")
    rejects.add_column("n", justify="right")
    for reason, count in sorted(report.rejection_counts, key=lambda item: -item[1]):
        rejects.add_row(reason, str(count))
    console.print(rejects)

    console.print(f"party rules: {dict(report.party_rule_counts)}")
    console.print(f"sources in set: {dict(report.source_counts)}")
    if result.climatology is not None:
        console.print(
            f"climatology: base rate {result.climatology.base_rate:.4f}, "
            f"Brier [bold]{result.climatology.brier:.6f}[/bold]"
        )
    console.print(f"manifest sha256: {summary['manifest_sha256']}")
    if report.shortfall_reason:
        err_console.print(f"[yellow]shortfall[/yellow] {report.shortfall_reason}")


@ledger_app.command("build")
def ledger_build(
    config: OverlayOpt = None,
    refresh: Annotated[
        bool,
        typer.Option("--refresh", help="Re-fetch sources. Changes the pool and the manifest."),
    ] = False,
    write: Annotated[bool, typer.Option("--write/--no-write", help="Persist to Postgres.")] = False,
    replace: Annotated[
        bool, typer.Option("--replace", help="Overwrite an existing registry.")
    ] = False,
) -> None:
    """Assemble the registry from every source and report its composition.

    Exits 3 when the set does not satisfy the inclusion rules, so a shortfall
    cannot be mistaken for success by a script. The rules are never relaxed to
    reach the target -- see cascade/ledger/select.py for the precedence.
    """
    from cascade.ledger.registry import build_registry
    from cascade.ledger.store import write_registry

    settings = _settings(config)
    result = build_registry(settings, refresh=refresh)
    _print_build_report(result)

    report = result.selection.report
    ledger_cfg = settings.ledger
    ok = (
        report.met_target
        and ledger_cfg.yes_rate_min <= report.yes_rate <= ledger_cfg.yes_rate_max
        and report.max_domain_share <= ledger_cfg.max_domain_share
    )

    if write and result.records:
        written = write_registry(settings, result.records, replace=replace)
        console.print(f"[green]wrote[/green] {written} scenarios and labels")

    if not ok:
        _fail(
            "the assembled set does not satisfy the inclusion rules; "
            "see the shortfall above. Do not relax the rules to close the gap.",
            EXIT_PRECONDITION,
        )


@ledger_app.command("seal")
def ledger_seal(config: OverlayOpt = None) -> None:
    """Hash the loaded registry and record the seal (spec §1.3).

    Every later phase asserts this hash on startup. Sealing a set that does
    not meet the inclusion rules is refused: the seal is what downstream
    phases trust.
    """
    from cascade.ledger.climatology import climatology_of
    from cascade.ledger.manifest import compute_manifest
    from cascade.ledger.store import load_records, write_manifest

    settings = _settings(config)
    records = load_records(settings, role="admin")
    if not records:
        _fail(
            "no scenarios are loaded; run `cascade ledger build --write` first", EXIT_PRECONDITION
        )

    climatology = climatology_of(records)
    ledger_cfg = settings.ledger
    if not ledger_cfg.yes_rate_min <= climatology.base_rate <= ledger_cfg.yes_rate_max:
        _fail(
            f"resolved-YES rate {climatology.base_rate:.4f} is outside "
            f"[{ledger_cfg.yes_rate_min}, {ledger_cfg.yes_rate_max}]; refusing to seal",
            EXIT_PRECONDITION,
        )

    manifest = compute_manifest(records)
    write_manifest(
        settings,
        manifest_sha256=manifest,
        climatology=climatology,
        study_salt=settings.study.salt,
        notes=f"n={climatology.n}",
    )
    console.print(f"[green]sealed[/green] {climatology.n} scenarios")
    console.print(f"manifest sha256: [bold]{manifest}[/bold]")
    console.print(
        f"climatology: base rate {climatology.base_rate:.4f}, " f"Brier {climatology.brier:.6f}"
    )


def _outside_repository(path: Path, *, what: str) -> Path:
    """Resolve ``path`` and refuse it if it lies inside the repository.

    An archive holds the resolution labels. In the database a grant keeps them
    from the simulation (invariant 2); on disk nothing does, so the file must
    not sit beside the code the simulation runs -- or where `git add` finds it.
    """
    from cascade.config import repo_root

    resolved = path.expanduser().resolve()
    root = repo_root().resolve()
    if resolved == root or root in resolved.parents:
        _fail(
            f"{what} {resolved} is inside the repository. The archive contains the resolution "
            "labels; keep it outside the tree the simulation runs from.",
            EXIT_PRECONDITION,
        )
    return resolved


@ledger_app.command("export")
def ledger_export(
    config: OverlayOpt = None,
    to: Annotated[
        Path, typer.Option("--to", help="Archive file to write, outside the repository.")
    ] = Path("cascade-registry.json"),
) -> None:
    """Write the sealed registry to one verifiable file (tier-0 recovery).

    The frozen split cannot be regenerated: re-fetching the sources yields a
    different set once markets have resolved, which is how this project lost
    its first one. Run this after `ledger seal` and keep the file somewhere the
    database's failure cannot reach. **The file contains the labels.**
    """
    import os

    from cascade.ledger.archive import ArchiveCorrupt, dump_archive
    from cascade.ledger.store import load_records, read_manifest

    settings = _settings(config)
    target = _outside_repository(to, what="--to")
    sealed = read_manifest(settings, role="admin")
    if sealed is None:
        _fail("the registry is not sealed; run `cascade ledger seal` first", EXIT_PRECONDITION)
    records = load_records(settings, role="admin")
    try:
        text = dump_archive(
            records, manifest_sha256=sealed.manifest_sha256, study_salt=sealed.study_salt
        )
    except (ArchiveCorrupt, ValueError) as exc:
        _fail(str(exc), EXIT_PRECONDITION)

    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".partial")
    # Owner-only from the first byte: created 0600, never chmod-ed afterwards.
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(text)
    temporary.replace(target)
    console.print(
        f"[green]exported[/green] {len(records)} scenarios and labels to {target} "
        f"(mode 0600)\nmanifest sha256: [bold]{sealed.manifest_sha256}[/bold]"
    )


@ledger_app.command("restore")
def ledger_restore(
    config: OverlayOpt = None,
    source: Annotated[Path, typer.Option("--from", help="Archive written by `ledger export`.")] = (
        Path("cascade-registry.json")
    ),
    replace: Annotated[
        bool, typer.Option("--replace", help="Overwrite a registry that is already loaded.")
    ] = False,
) -> None:
    """Restore the sealed registry from an archive, verifying it twice.

    The archive is checked before anything is written -- its content digest and
    its manifest -- and the database is re-read and re-hashed afterwards. A
    restore that lands on a different split exits 3 rather than resealing.
    """
    from cascade.ledger.archive import ArchiveCorrupt, load_archive
    from cascade.ledger.climatology import climatology_of
    from cascade.ledger.manifest import compute_manifest
    from cascade.ledger.store import load_records, write_manifest, write_registry

    settings = _settings(config)
    path = source.expanduser().resolve()
    if not path.is_file():
        _fail(f"no archive at {path}", EXIT_PRECONDITION)
    try:
        archive = load_archive(path.read_text(encoding="utf-8"))
    except ArchiveCorrupt as exc:
        _fail(f"refusing to restore: {exc}", EXIT_PRECONDITION)

    # The salt keys every seed and every outcome-independent subsample. The
    # same split under a different salt is a different study.
    if archive.study_salt != settings.study.salt:
        _fail(
            f"the archive was sealed under study salt {archive.study_salt!r} and this "
            f"configuration uses {settings.study.salt!r}; seeds and subsamples would differ",
            EXIT_PRECONDITION,
        )
    try:
        write_registry(settings, archive.records, replace=replace)
    except RuntimeError as exc:
        _fail(str(exc), EXIT_PRECONDITION)
    write_manifest(
        settings,
        manifest_sha256=archive.manifest_sha256,
        climatology=climatology_of(archive.records),
        study_salt=archive.study_salt,
        notes=f"n={len(archive.records)}; restored from an archive",
    )
    landed = compute_manifest(load_records(settings, role="admin"))
    if landed != archive.manifest_sha256:
        _fail(
            f"the restored registry hashes to {landed[:16]}..., not the archive's "
            f"{archive.manifest_sha256[:16]}...; do not reseal -- find out what changed",
            EXIT_PRECONDITION,
        )
    console.print(
        f"[green]restored[/green] {len(archive.records)} scenarios; manifest "
        f"[bold]{archive.manifest_sha256}[/bold] verified in the database"
    )


@ledger_app.command("verify")
def ledger_verify(config: OverlayOpt = None) -> None:
    """Assert the loaded registry still hashes to the sealed manifest.

    This is the check every later phase runs on startup. A mismatch means the
    frozen split moved, and the answer is never to reseal.
    """
    from cascade.ledger.manifest import ManifestMismatch, verify_manifest
    from cascade.ledger.store import load_records, read_manifest

    settings = _settings(config)
    sealed = read_manifest(settings, role="admin")
    if sealed is None:
        _fail("the registry has never been sealed; run `cascade ledger seal`", EXIT_PRECONDITION)
    records = load_records(settings, role="admin")
    try:
        verify_manifest(records, sealed.manifest_sha256)
    except ManifestMismatch as exc:
        _fail(str(exc), EXIT_PRECONDITION)
    console.print(
        f"[green]manifest verified[/green] {sealed.n_scenarios} scenarios, "
        f"sha256 {sealed.manifest_sha256[:16]}..."
    )


@ledger_app.command("status")
def ledger_status(config: OverlayOpt = None) -> None:
    """Show what is loaded and whether it is sealed."""
    from cascade.ledger.store import load_scenarios, read_manifest

    settings = _settings(config)
    scenarios = load_scenarios(settings, role="admin")
    sealed = read_manifest(settings, role="admin")

    table = Table(title="Scenario registry")
    table.add_column("Field", style="cyan")
    table.add_column("Value", overflow="fold")
    table.add_row("scenarios loaded", str(len(scenarios)))
    table.add_row("sealed", "yes" if sealed else "[yellow]no[/yellow]")
    if sealed:
        table.add_row("manifest sha256", sealed.manifest_sha256)
        table.add_row("sealed at", str(sealed.sealed_at))
        table.add_row("resolved-YES rate", f"{sealed.yes_rate:.4f}")
        table.add_row("climatology Brier", f"{sealed.climatology_brier:.6f}")
    console.print(table)


# ---------------------------------------------------------------------------
# retrieval: Chronofence (M3)
# ---------------------------------------------------------------------------


def _print_index_report(report: Any) -> None:
    """Print what the index pass did, per partition."""
    table = Table(title="IVFFlat index pass")
    table.add_column("Partition", style="cyan")
    table.add_column("rows", justify="right")
    table.add_column("lists", justify="right")
    table.add_column("target", justify="right")
    table.add_column("action")
    table.add_column("reason", overflow="fold")
    colour = {
        "create": "green",
        "rebuild": "yellow",
        "keep": "dim",
        "skip-empty": "dim",
    }
    for plan in report.plans:
        style = colour.get(plan.action, "")
        table.add_row(
            plan.partition,
            f"{plan.rows:,}",
            "-" if plan.current_m is None else f"{plan.current_m}/{plan.current_ef_construction}",
            f"{plan.target_m}/{plan.target_ef_construction}",
            f"[{style}]{plan.action}[/{style}]" if style else plan.action,
            plan.reason,
        )
    console.print(table)
    console.print(
        f"created [bold]{report.created}[/bold] · rebuilt [bold]{report.rebuilt}[/bold] · "
        f"kept [bold]{report.kept}[/bold] · skipped empty [bold]{report.skipped_empty}[/bold] "
        f"in {report.elapsed_s:.1f}s"
    )


def _print_fts_report(report: Any) -> None:
    """Print what the full-text index pass did, per partition."""
    table = Table(title="Full-text (GIN) index pass")
    table.add_column("Partition", style="cyan")
    table.add_column("rows", justify="right")
    table.add_column("index")
    table.add_column("action")
    table.add_column("reason", overflow="fold")
    colour = {"create": "green", "keep": "dim", "skip-empty": "dim"}
    for plan in report.plans:
        style = colour.get(plan.action, "")
        table.add_row(
            plan.partition,
            f"{plan.rows:,}",
            plan.index_name,
            f"[{style}]{plan.action}[/{style}]" if style else plan.action,
            plan.reason,
        )
    console.print(table)
    console.print(
        f"created [bold]{report.created}[/bold] · kept [bold]{report.kept}[/bold] · "
        f"skipped empty [bold]{report.skipped_empty}[/bold] in {report.elapsed_s:.1f}s"
    )


@retrieval_app.command("index")
def retrieval_index(
    config: OverlayOpt = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Print the plan without touching any index."),
    ] = False,
    drop_legacy: Annotated[
        bool,
        typer.Option("--drop-legacy", help="Drop IVFFlat indexes after the HNSW pass."),
    ] = False,
    fts: Annotated[
        bool | None,
        typer.Option(
            "--fts/--no-fts",
            help="Also build the full-text GIN indexes hybrid retrieval needs "
            "(default: only when retrieval.mode is hybrid).",
        ),
    ] = None,
) -> None:
    """Create one HNSW index per non-empty chunks partition (ADR-0012, ADR-0026).

    `--fts` also builds the expression GIN index the hybrid keyword pool reads
    (migration 018). It is opt-in while `retrieval.mode` is `vector`, because
    it is the long build -- tens of minutes over the full corpus -- and an
    operator re-running the HNSW pass should not start it by accident. It
    blocks writes to each partition while it builds: run it after ingest.

    Idempotent, and -- unlike the IVFFlat pass this replaces -- a partition
    that has merely grown needs nothing. HNSW has no row-dependent build
    parameter, so a rebuild fires only when `hnsw_m` or `hnsw_ef_construction`
    changes.

    `--drop-legacy` removes IVFFlat indexes left on the partitions afterwards.
    It runs last on purpose: dropping first would leave the corpus unindexed
    for the length of the build.
    """
    from cascade.retrieval.index import (
        apply_fts_plans,
        apply_plans,
        drop_legacy_ivfflat,
        measure,
        plan_all,
        plan_fts,
    )
    from cascade.retrieval.schema import FtsIndexReport, IndexReport

    settings = _settings(config)
    retrieval = settings.retrieval
    build_fts = fts if fts is not None else retrieval.mode == "hybrid"

    partitions = measure(settings)
    plans = plan_all(
        partitions,
        m=retrieval.hnsw_m,
        ef_construction=retrieval.hnsw_ef_construction,
    )
    fts_plans = plan_fts(partitions) if build_fts else ()

    if dry_run:
        if build_fts:
            _print_fts_report(
                FtsIndexReport(
                    plans=fts_plans,
                    created=sum(1 for plan in fts_plans if plan.action == "create"),
                    kept=sum(1 for plan in fts_plans if plan.action == "keep"),
                    skipped_empty=sum(1 for plan in fts_plans if plan.action == "skip-empty"),
                    elapsed_s=0.0,
                )
            )
        _print_index_report(
            IndexReport(
                plans=plans,
                created=sum(1 for plan in plans if plan.action == "create"),
                rebuilt=sum(1 for plan in plans if plan.action == "rebuild"),
                kept=sum(1 for plan in plans if plan.action == "keep"),
                skipped_empty=sum(1 for plan in plans if plan.action == "skip-empty"),
                elapsed_s=0.0,
            )
        )
        console.print("[yellow]dry run: nothing was changed[/yellow]")
        return

    _print_index_report(apply_plans(settings, plans))
    if build_fts:
        _print_fts_report(apply_fts_plans(settings, fts_plans))
    if drop_legacy:
        dropped = drop_legacy_ivfflat(settings)
        console.print(
            f"dropped [bold]{len(dropped)}[/bold] legacy IVFFlat index(es)"
            + (
                f": {', '.join(dropped[:3])}..."
                if len(dropped) > 3
                else (f": {', '.join(dropped)}" if dropped else "")
            )
        )


def _print_bench_result(result: Any, settings: Settings) -> None:
    """Print measured latency and recall. Measured, never targeted."""
    target = settings.retrieval

    table = Table(title="Chronofence bench")
    table.add_column("Quantity", style="cyan")
    table.add_column("Measured", justify="right")
    table.add_column("Required", justify="right")
    table.add_column("", width=3)

    def row(name: str, measured: str, required: str, ok: bool) -> None:
        table.add_row(name, measured, required, "[green]OK[/green]" if ok else "[red]NO[/red]")

    row("queries", f"{result.queries:,}", "-", True)
    row("hnsw ef_search", str(result.ef_search), str(target.hnsw_ef_search), True)
    row("p50", f"{result.latency.p50:.2f} ms", "-", True)
    row(
        "p95",
        f"{result.latency.p95:.2f} ms",
        f"< {target.target_p95_ms:.0f} ms",
        result.latency.p95 < target.target_p95_ms,
    )
    row("p99", f"{result.latency.p99:.2f} ms", "-", True)
    row("min / max", f"{result.latency.minimum:.2f} / {result.latency.maximum:.2f} ms", "-", True)
    row(
        f"recall@{result.recall.k}",
        f"{result.recall.mean:.4f}" if result.recall.measured else "not measured",
        f"> {target.target_recall_at_k:.2f}",
        result.recall.measured and result.recall.mean > target.target_recall_at_k,
    )
    row("recall sample", f"{result.recall.scored:,} queries", "-", True)
    row("empty results", f"{result.empty_results:,}", "-", True)
    console.print(table)

    histogram = Table(title="Latency histogram (ms)")
    histogram.add_column("Bucket", style="cyan", justify="right")
    histogram.add_column("Count", justify="right")
    histogram.add_column("Share", justify="right")
    histogram.add_column("", overflow="crop")
    total = max(1, result.latency.n)
    for lower, upper, count in result.latency.histogram:
        if count == 0:
            continue
        label = f"{lower:g}-{upper:g}" if upper != float("inf") else f">{lower:g}"
        share = count / total
        histogram.add_row(label, f"{count:,}", f"{share:6.2%}", "█" * round(share * 40))
    console.print(histogram)

    console.print(
        f"cutoffs: [bold]{result.distinct_cutoffs}[/bold] distinct, "
        f"{result.earliest_cutoff} .. {result.latest_cutoff}"
    )
    if result.recall.measured:
        console.print(
            f"recall detail: min {result.recall.minimum:.4f}, "
            f"perfect on {result.recall.perfect:,}/{result.recall.scored:,}, "
            f"{result.recall.empty_ground_truth:,} sampled queries had no admissible evidence"
        )
    console.print(f"bench wall time: {result.elapsed_s:.1f}s")


def _print_relevance_report(report: Any, settings: Settings) -> None:
    """Print the vector-against-hybrid comparison. Measured, never targeted."""

    def number(value: float | None, metric: str) -> str:
        if value is None:
            return "-"
        return (
            f"{value:.4f}" if metric in {"party_mention_rate", "mean_distance"} else f"{value:.2f}"
        )

    for kind in report.kinds:
        table = Table(
            title=(
                f"{kind.kind} evidence · k={kind.k} · {kind.scenarios} scenarios · "
                f"{kind.queries:,} queries"
            )
        )
        table.add_column("Metric", style="cyan")
        table.add_column("names")
        table.add_column("vector", justify="right")
        table.add_column("hybrid", justify="right")
        table.add_column("hybrid - vector", justify="right")
        table.add_column("95% CI", justify="right")
        table.add_column("p", justify="right")
        table.add_column("n", justify="right")
        table.add_column("n/a", justify="right")
        for item in kind.comparisons:
            interval = item.interval
            table.add_row(
                item.label,
                item.names,
                number(item.vector_mean, item.metric),
                number(item.hybrid_mean, item.metric),
                "-" if interval is None else f"{interval.point:+.4f}",
                "-" if interval is None else f"[{interval.lo:+.4f}, {interval.hi:+.4f}]",
                "-" if interval is None else f"{interval.p_value:.4f}",
                str(item.n_paired),
                str(item.unmeasurable),
            )
        console.print(table)
        console.print(
            f"  overlap of the two arms' chunks (Jaccard): [bold]{kind.mean_overlap:.4f}[/bold] · "
            f"hybrid queries with no entity term: [bold]{kind.queries_without_terms:,}[/bold]"
            f"/{kind.queries:,}"
        )
        console.print(
            f"  latency p50/p95: vector {kind.vector_latency.p50:.1f}/"
            f"{kind.vector_latency.p95:.1f} ms · hybrid {kind.hybrid_latency.p50:.1f}/"
            f"{kind.hybrid_latency.p95:.1f} ms"
        )

    console.print(
        f"scenarios [bold]{report.scenarios}[/bold] · compiled graphs [bold]{report.graphs}[/bold] "
        f"(agent rows cover only these) · paired bootstrap B={report.bootstrap_b:,}, "
        "resampling scenarios, seeded from the study salt"
    )
    console.print(
        f"registry names: [bold]{report.distinct_names}[/bold] distinct, "
        f"[bold]{len(report.generic)}[/bold] screened as generic (mentioned by more than "
        f"{settings.retrieval.relevance_generic_name_rate:.0%} of chunks retrieved for scenarios "
        f"that do not list them); [bold]{report.scenarios_without_informative_names}[/bold] "
        "scenario(s) left with none"
    )
    if report.generic:
        shown = ", ".join(report.generic[:24])
        console.print(f"  generic: {shown}{'...' if len(report.generic) > 24 else ''}")
    console.print(
        "[dim]These are properties the hybrid path was built to move, so they show "
        "whether its mechanisms work on this corpus and what they cost in embedding "
        "distance. They are not evidence about forecast quality.[/dim]"
    )
    console.print(f"bench wall time: {report.elapsed_s:.1f}s")


@retrieval_app.command("bench")
def retrieval_bench(
    config: OverlayOpt = None,
    queries: Annotated[
        int | None,
        typer.Option("--queries", help="Override the configured query count."),
    ] = None,
    relevance: Annotated[
        bool,
        typer.Option(
            "--relevance",
            help="Compare vector and hybrid retrieval on the study's own queries, "
            "outcome-blind. Decides nothing by itself; exits 3 if hybrid is not built.",
        ),
    ] = False,
    limit: Annotated[
        int | None,
        typer.Option("--limit", help="With --relevance: only the first N scenarios by id."),
    ] = None,
) -> None:
    """Benchmark time-locked retrieval: p50/p95/p99 and recall@k (M3).

    Exits 3 when a measured value misses its acceptance criterion, so a
    regression cannot pass as success in CI.

    `--relevance` is a different measurement (M14): both retrieval modes over
    the compiler's, the agents' and the baselines' real queries, scored on
    properties of the retrieved set alone -- no label is read, and it runs as
    `cascade_sim`, which could not read one. It reports; it has no pass mark,
    because which mode to run is a decision and not a criterion. It exits 3
    only when the hybrid path is not there to be measured.
    """
    from cascade.retrieval.bench import HybridNotReady, run_bench, run_relevance

    settings = _settings(config)
    if relevance:
        try:
            report = run_relevance(settings, limit=limit)
        except HybridNotReady as exc:
            _fail("; ".join(exc.reasons), EXIT_PRECONDITION)
        _print_relevance_report(report, settings)
        return
    result = run_bench(settings, count=queries)
    _print_bench_result(result, settings)

    passed, failures = result.meets(settings)
    if not passed:
        _fail("; ".join(failures), EXIT_PRECONDITION)
    console.print("[bold green]chronofence: latency and recall criteria met[/bold green]")


@retrieval_app.command("verify")
def retrieval_verify(config: OverlayOpt = None) -> None:
    """Assert the Chronofence preconditions hold (spec §4.2).

    Checks the structural guarantees, not a sample of results: the deployed
    function pins the configured probes, every non-empty partition carries a
    correctly sized index, and `cascade_sim` cannot reach the corpus except
    through `chronofence_search`. Exits 3 on any violation.
    """
    from cascade.retrieval.bench import hybrid_readiness
    from cascade.retrieval.index import measure, plan_all
    from cascade.retrieval.search import Chronofence

    settings = _settings(config)
    retrieval = settings.retrieval
    failures: list[str] = []

    with Chronofence(settings, role="admin") as fence:
        deployed = fence.ef_search()
        hybrid_deployed = fence.hybrid_deployed()
        hybrid_ef = fence.ef_search("chronofence_search_hybrid") if hybrid_deployed else None
    if deployed != retrieval.hnsw_ef_search:
        failures.append(
            f"chronofence_search pins hnsw.ef_search={deployed} but config says "
            f"{retrieval.hnsw_ef_search}; migrations are forward-only, so add a new "
            "migration rather than editing 004"
        )

    partitions = measure(settings)
    plans = plan_all(
        partitions,
        m=retrieval.hnsw_m,
        ef_construction=retrieval.hnsw_ef_construction,
    )
    stale = [plan for plan in plans if plan.action in {"create", "rebuild"}]

    table = Table(title="Chronofence preconditions")
    table.add_column("Check", style="cyan")
    table.add_column("Measured", justify="right")
    table.add_column("", width=3)

    def row(name: str, measured: str, ok: bool) -> None:
        table.add_row(name, measured, "[green]OK[/green]" if ok else "[red]NO[/red]")

    indexed = sum(1 for plan in plans if plan.action == "keep")
    non_empty = sum(1 for plan in plans if plan.rows > 0)
    row("hnsw ef_search pinned", str(deployed), deployed == retrieval.hnsw_ef_search)
    row("non-empty partitions", str(non_empty), True)
    row("correctly sized indexes", f"{indexed}/{non_empty}", not stale)

    # The hybrid path is a precondition only of the mode that uses it. Under
    # `vector` its state is printed and cannot fail the command: an unbuilt
    # optional index is not drift.
    hybrid_required = retrieval.mode == "hybrid"
    hybrid_reasons = list(hybrid_readiness(deployed=hybrid_deployed, partitions=partitions))
    if hybrid_ef is not None and hybrid_ef != retrieval.hnsw_ef_search:
        hybrid_reasons.append(
            f"chronofence_search_hybrid pins hnsw.ef_search={hybrid_ef} but config says "
            f"{retrieval.hnsw_ef_search}; add a migration rather than editing 018"
        )
    with_fts = sum(1 for item in partitions if item.rows > 0 and item.fts_index_name)
    row("retrieval.mode", retrieval.mode, True)
    row(
        "hybrid function deployed",
        "yes" if hybrid_deployed else "no",
        hybrid_deployed or not hybrid_required,
    )
    row(
        "full-text indexes",
        f"{with_fts}/{non_empty}",
        with_fts == non_empty or not hybrid_required,
    )

    leak_ok, leak_detail = _sim_role_is_fenced(settings)
    row("cascade_sim fenced from chunks", leak_detail, leak_ok)
    console.print(table)

    if hybrid_required:
        failures.extend(hybrid_reasons)
    elif hybrid_reasons:
        console.print(
            "[dim]hybrid retrieval is not built (not required while retrieval.mode is "
            "vector): " + "; ".join(hybrid_reasons) + "[/dim]"
        )

    if stale:
        failures.append(
            f"{len(stale)} partition(s) need an index pass: "
            + ", ".join(f"{plan.partition} ({plan.action})" for plan in stale[:6])
            + ("..." if len(stale) > 6 else "")
            + " -- run `cascade retrieval index`"
        )
    if not leak_ok:
        failures.append(f"cascade_sim is not fenced from the corpus: {leak_detail}")

    if failures:
        _fail("; ".join(failures), EXIT_PRECONDITION)
    console.print("[bold green]chronofence: all preconditions hold[/bold green]")


@retrieval_app.command("memorization")
def retrieval_memorization(
    config: OverlayOpt = None,
    limit: Annotated[
        int | None,
        typer.Option("--limit", help="Probe only the first N scenarios by id."),
    ] = None,
) -> None:
    """Measure what the agent model already knows with no context (M3, spec §4.2).

    The one leakage control that cannot be engineered away: Chronofence keeps
    post-cutoff *documents* out of the context window, not post-cutoff *facts*
    out of the weights. Reports the distribution; it never fails a threshold,
    because there is no threshold to enforce -- the number is disclosed
    alongside the headline metric.

    Costs money in `record` mode. The `bench` phase ceiling applies.
    """
    from cascade.ledger.store import load_records
    from cascade.retrieval.memorization import run_probe

    settings = _settings(config)

    # Checked here rather than left to the SDK: a probe that dies twenty
    # scenarios in has already spent money, and the operator needs to know
    # what to configure before it starts, not after.
    _require_provider_ready(settings)

    records = load_records(settings, role="eval")
    if not records:
        _fail("scenario registry is empty; run `cascade ledger build` first", EXIT_PRECONDITION)
    if limit is not None:
        records = tuple(sorted(records, key=lambda item: item.scenario.scenario_id))[:limit]

    report = run_probe(settings, records)

    table = Table(title="Parametric probe (memorization)")
    table.add_column("Quantity", style="cyan")
    table.add_column("Measured", justify="right")
    table.add_row("scenarios probed", f"{report.total:,}")
    table.add_row("answers parsed", f"{report.scored:,}")
    table.add_row("unparseable", f"{report.unparseable:,}")
    table.add_row("mean confidence", f"{report.mean_confidence:.4f}")
    table.add_row("median confidence", f"{report.median_confidence:.4f}")
    table.add_row("probe Brier", "n/a" if report.brier is None else f"{report.brier:.6f}")
    table.add_row("directionally correct", f"{report.directionally_correct:,}/{report.scored:,}")
    table.add_row(
        "confident (>=0.5) and correct", f"{report.confident_and_correct:,}/{report.scored:,}"
    )
    console.print(table)

    deciles = Table(title="Confidence distribution")
    deciles.add_column("Bucket", style="cyan", justify="right")
    deciles.add_column("Scenarios", justify="right")
    deciles.add_column("", overflow="crop")
    for low, count in report.confidence_deciles:
        share = count / max(1, report.scored)
        deciles.add_row(f"{low:.1f}-{low + 0.1:.1f}", f"{count:,}", "█" * round(share * 40))
    console.print(deciles)

    if report.unparseable:
        console.print(
            f"[yellow]{report.unparseable} answers could not be parsed[/yellow] and are "
            "excluded from every statistic above rather than scored as 0.5"
        )
    console.print(
        "[dim]Measured and disclosed, never enforced: a high score is a fact about "
        "the model, not a defect in the harness (spec §4.2).[/dim]"
    )


def _sim_role_is_fenced(settings: Settings) -> tuple[bool, str]:
    """True when `cascade_sim` cannot SELECT the corpus tables directly.

    The grant is the mechanism (invariant 2, ADR-0005), so this asserts the
    mechanism rather than trusting that no code happens to issue the query.
    """
    import psycopg

    try:
        with psycopg.connect(settings.database_url("sim"), connect_timeout=10) as conn:
            for table in ("chunks", "documents"):
                with conn.cursor() as cur:
                    try:
                        cur.execute(f"SELECT 1 FROM {table} LIMIT 1")  # noqa: S608
                    except psycopg.errors.InsufficientPrivilege:
                        conn.rollback()
                        continue
                    return False, f"cascade_sim can SELECT {table}"
    except psycopg.OperationalError as exc:
        return False, f"cannot connect as cascade_sim: {exc}"
    return True, "denied on chunks and documents"


# ---------------------------------------------------------------------------
# compile: Lathe, the causal decomposition compiler (M4)
# ---------------------------------------------------------------------------


def _scorable(scenarios: Sequence[Any], *, what: str) -> list[Any]:
    """Drop the scenarios declared unscoreable (ADR-0043), saying how many.

    Nothing is scored on them, so nothing is spent on them either: compiling,
    briefing or forecasting a stand-in like "Candidate B" would buy a number
    the report is bound to discard. Scoring does not depend on this filter --
    the split declaration drops them from every scored figure regardless.
    """
    from cascade.ledger.exclusions import exclusions

    excluded = {
        item.scenario_id
        for item in exclusions(
            [(scenario.scenario_id, scenario.question) for scenario in scenarios]
        )
    }
    if excluded:
        console.print(
            f"[dim]{len(excluded)} scenario(s) excluded from scoring are skipped by {what} "
            "(exchange placeholder legs; ADR-0043)[/dim]"
        )
    return [scenario for scenario in scenarios if scenario.scenario_id not in excluded]


def _situations(
    settings: Settings,
    scenario_ids: Sequence[str],
    *,
    role: Literal["sim", "eval"],
    grounded: bool = True,
) -> dict[str, tuple[str, str | None]]:
    """The rendered dossier per scenario, or a precondition failure.

    One funnel for every phase that builds a prompt, so "the dossier is on and
    some scenarios have none" is exit 3 everywhere rather than an empty report
    in whichever phase forgot to check (ADR-0037).

    The dossier is evidence, so it follows Appendix C's factor C: an
    ungrounded cell gets no report, exactly as it gets no chunks, and the
    table is not read at all.
    """
    from cascade.decompose.dossier_store import MissingDossiers, situations_for

    if not grounded:
        return {scenario_id: ("", None) for scenario_id in sorted(scenario_ids)}
    try:
        return situations_for(settings, tuple(scenario_ids), role=role)
    except MissingDossiers as exc:
        _fail(str(exc), EXIT_PRECONDITION)


def _chronofence_hits(settings: Settings) -> tuple[Any, Any]:
    """A `(search, fence)` pair for the dossier writer: text in, ranked hits out.

    Opened as `eval`, like the compiler's retrieval, and for the same reason:
    writing a dossier is offline preparation, and the separation that matters
    here is the time lock, which the search function enforces for every role.
    """
    from cascade.corpus.embed import Embedder
    from cascade.decompose.dossier import Hit
    from cascade.retrieval.search import Chronofence

    embedder = Embedder(
        model_name=settings.models.embedding, batch_size=settings.corpus.embed_batch_size
    )
    embedder.load()
    fence = Chronofence(settings, role="eval")
    fence.__enter__()

    def search(query: str, as_of: Any, k: int) -> list[Hit]:
        # Through the mode switch like every other evidence site: a dossier
        # pooled one way and agent evidence retrieved another would make the
        # report and the excerpts disagree about what the corpus holds.
        vector = embedder.encode([query])[0]
        result = fence.retrieve(vector, text=query, as_of=as_of, k=k)
        return [
            Hit(
                chunk_id=chunk.chunk_id,
                document_id=chunk.document_id,
                published_at=chunk.published_at,
                source=chunk.source,
                body=chunk.body,
            )
            for chunk in result.chunks
        ]

    return search, fence


def _lathe(
    settings: Settings, situations: Mapping[str, tuple[str, str | None]] | None = None
) -> Any:
    """Wire the compiler to the real model, embedder and corpus.

    Retrieval goes through Chronofence as `eval` rather than `sim`: compilation
    is an offline preparation step, not part of a run, and the role separation
    that matters here is the time lock -- which `chronofence_search` enforces
    identically for both roles.
    """
    from cascade.corpus.embed import Embedder
    from cascade.decompose.compiler import Lathe
    from cascade.llm.client import LLMClient
    from cascade.retrieval.search import Chronofence

    embedder = Embedder(
        model_name=settings.models.embedding, batch_size=settings.corpus.embed_batch_size
    )
    embedder.load()

    fence = Chronofence(settings, role="eval")
    fence.__enter__()

    def retrieve(question: str, as_of: Any, k: int) -> list[tuple[str, str, str]]:
        from cascade.retrieval.queries import compiler_evidence_query

        query = compiler_evidence_query(question)
        vector = embedder.encode([query.text])[0]
        result = fence.retrieve(
            vector, text=query.keyword_text, entities=query.entities, as_of=as_of, k=k
        )
        return [
            (chunk.published_at.isoformat(), chunk.source, chunk.body) for chunk in result.chunks
        ]

    compiler = Lathe(
        settings=settings,
        client=LLMClient(settings, phase="compile"),
        embed=embedder.encode,
        retrieve=retrieve,
        situations=dict(situations or {}),
    )
    return compiler, fence


def _require_provider_ready(settings: Settings) -> None:
    """Fail before spending anything if the active provider cannot be used.

    Checked here rather than left to the SDK: a 180-scenario compile that dies
    on scenario 20 has already spent real money, and the operator needs to know
    what to configure before it starts. Provider-aware (ADR-0028): Bedrock and
    Claude Platform on AWS authenticate through IAM and need no Anthropic key,
    so demanding one would block exactly the runs those providers exist for.
    """
    from cascade.llm.providers import readiness_problems

    if settings.llm.mode == "replay":
        return
    problems = readiness_problems(
        settings, models=(settings.models.agent, settings.models.compiler)
    )
    if problems:
        _fail(
            f"llm.mode={settings.llm.mode!r} through provider {settings.llm.provider!r} "
            f"cannot start: {'; '.join(problems)}. Configure the provider, or replay a "
            "recorded phase with CASCADE_LLM__MODE=replay",
            EXIT_PRECONDITION,
        )


def _print_compile_stats(stats: Any, settings: Settings) -> None:
    """Print the M4 acceptance figures. Measured, never targeted."""
    table = Table(title="Causal graphs (M4)")
    table.add_column("Quantity", style="cyan")
    table.add_column("Measured", justify="right")
    table.add_column("Required", justify="right")
    table.add_column("", width=3)

    def row(name: str, measured: str, required: str, ok: bool) -> None:
        table.add_row(name, measured, required, "[green]OK[/green]" if ok else "[red]NO[/red]")

    row(
        "graphs compiled",
        f"{stats.compiled:,}/{stats.scenarios:,}",
        f"{stats.scenarios:,}",
        stats.compiled == stats.scenarios and stats.scenarios > 0,
    )
    row("hard failures", f"{stats.failed:,}", "0", stats.failed == 0)
    row(
        "mean actors",
        f"{stats.mean_actors:.2f}" if stats.compiled else "-",
        "14 +/- 2",
        bool(stats.compiled) and 12.0 <= stats.mean_actors <= 16.0,
    )
    row(
        "actor range",
        f"{stats.min_actors}-{stats.max_actors}" if stats.compiled else "-",
        "8-20",
        bool(stats.compiled) and stats.min_actors >= 8 and stats.max_actors <= 20,
    )
    row(
        "mean factors",
        f"{stats.mean_factors:.2f}" if stats.compiled else "-",
        "-",
        True,
    )
    row(
        "factor range",
        f"{stats.min_factors}-{stats.max_factors}" if stats.compiled else "-",
        "4-12",
        bool(stats.compiled) and stats.min_factors >= 4 and stats.max_factors <= 12,
    )
    row(
        "distinct graph hashes",
        f"{stats.distinct_hashes:,}",
        f"{stats.compiled:,}",
        stats.distinct_hashes == stats.compiled,
    )
    row("mean edges", f"{stats.mean_edges:.1f}" if stats.compiled else "-", "-", True)
    row(
        "mean evidence chunks",
        f"{stats.mean_evidence_chunks:.1f}" if stats.compiled else "-",
        f"{settings.retrieval.k_compiler}",
        True,
    )
    row("total LLM calls", f"{stats.total_llm_calls:,}", "-", True)
    console.print(table)

    if stats.retry_histogram:
        histogram = Table(title="Repair retries")
        histogram.add_column("Retries", style="cyan", justify="right")
        histogram.add_column("Graphs", justify="right")
        histogram.add_column("Share", justify="right")
        histogram.add_column("", overflow="crop")
        for retries, count in stats.retry_histogram:
            share = count / max(1, stats.compiled)
            histogram.add_row(str(retries), f"{count:,}", f"{share:6.2%}", "#" * round(share * 40))
        console.print(histogram)


def _scorable_count(settings: Settings) -> int:
    """How many sealed scenarios a compile is expected to cover.

    The 15 stand-ins are excluded from scoring (ADR-0043) and skipped by
    compile, so measuring the graphs against all 180 would report a shortfall
    nobody intends to close.
    """
    from cascade.ledger.store import load_scenarios

    scenarios = load_scenarios(settings, role="admin")
    return len(_scorable(scenarios, what="the compile gate"))


def _shard(value: str | None) -> tuple[int, int] | None:
    """Parse ``k/n`` into an offset and a stride, or exit 3.

    Positional, not keyed: the pending list is already sorted, so taking every
    n-th item from k partitions it exactly, and two processes given different k
    can never draw the same scenario.
    """
    if value is None:
        return None
    left, _, right = value.partition("/")
    if not left.isdigit() or not right.isdigit() or not 0 <= int(left) < int(right):
        _fail(f"--shard must be `k/n` with 0 <= k < n; got {value!r}", EXIT_PRECONDITION)
    return int(left), int(right)


@compile_app.command("build")
def compile_build(
    config: OverlayOpt = None,
    limit: Annotated[
        int | None, typer.Option("--limit", help="Compile at most N pending scenarios.")
    ] = None,
    rebuild: Annotated[
        bool,
        typer.Option("--rebuild", help="Recompile scenarios that already have a graph."),
    ] = False,
    shard: Annotated[
        str | None,
        typer.Option("--shard", help="Compile one part of the pending work, as `k/n`."),
    ] = None,
) -> None:
    """Compile scenarios into typed causal graphs (M4, spec §5).

    Resumable: scenarios already compiled are skipped unless --rebuild is
    given, so an interrupted run continues rather than paying for 180 scenarios
    again. Costs money in `record` mode; the `compile` phase ceiling applies.

    ``--shard k/n`` takes every n-th pending scenario, counting from k, so
    several processes can compile at once without two of them paying for the
    same scenario. The partition is by position in the sorted pending list, so
    it is the same on every machine; each process still skips what is already
    stored, so a re-run after an interruption stays correct whatever shard it
    is given.
    """
    from cascade.decompose.store import (
        compile_stats,
        completed_scenarios,
        record_failure,
        write_graph,
    )
    from cascade.ledger.store import load_scenarios

    settings = _settings(config)
    # Checked before anything is loaded or spent: a bad shard is a typo in a
    # command that otherwise runs for hours.
    part = _shard(shard)
    _require_provider_ready(settings)

    scenarios = load_scenarios(settings, role="admin")
    if not scenarios:
        _fail("scenario registry is empty; run `cascade ledger build` first", EXIT_PRECONDITION)
    scenarios = tuple(_scorable(scenarios, what="compile"))

    done = set() if rebuild else completed_scenarios(settings)
    pending = [item for item in scenarios if item.scenario_id not in done]
    stored = len(scenarios) - len(pending)
    if part is not None:
        pending = pending[part[0] :: part[1]]
    if limit is not None:
        pending = pending[:limit]

    console.print(
        f"compiling [bold]{len(pending)}[/bold] scenario(s); {stored} already stored"
        + (f"; shard {shard}" if shard else "")
        + (
            f"; {len(scenarios) - stored - len(pending)} left for another run"
            if limit or shard
            else ""
        )
    )

    situations = _situations(settings, [item.scenario_id for item in pending], role="eval")
    compiler, fence = _lathe(settings, situations)
    compiled = failed = 0
    try:
        for index, scenario in enumerate(pending, start=1):
            outcome = compiler.compile_scenario(scenario)
            if outcome.ok:
                write_graph(settings, outcome)
                compiled += 1
                marker = f"[green]ok[/green] retries={outcome.repair_retries}"
            else:
                record_failure(settings, outcome)
                failed += 1
                marker = f"[red]failed[/red] {'; '.join(outcome.violations)[:90]}"
            console.print(f"  [{index}/{len(pending)}] {scenario.scenario_id}: {marker}")
    finally:
        fence.__exit__(None, None, None)

    console.print(f"compiled [bold]{compiled}[/bold], failed [bold]{failed}[/bold]")
    _print_compile_stats(compile_stats(settings, expected_scenarios=len(scenarios)), settings)
    if failed:
        _fail(
            f"{failed} scenario(s) could not be compiled within the retry budget", EXIT_PRECONDITION
        )


@compile_app.command("dossier")
def compile_dossier(
    config: OverlayOpt = None,
    limit: Annotated[
        int | None, typer.Option("--limit", help="Write at most N pending dossiers.")
    ] = None,
    rebuild: Annotated[
        bool,
        typer.Option("--rebuild", help="Rewrite scenarios that already have a dossier."),
    ] = False,
) -> None:
    """Write one cited situation report per scenario (ADR-0037).

    Runs whether or not `dossier.enabled` is set: writing the reports is how
    the development-partition comparison gets its "on" arm, and a stored
    dossier changes nothing until a configuration reads it. Resumable per
    scenario. Costs money in `record` mode; the `dossier` phase ceiling applies.

    Prints the refused-claim rate by reason. That figure is the dossier's own
    integrity measurement -- how often the writer asserted what its sources do
    not say -- and belongs wherever the dossier is reported.
    """
    from cascade.decompose.dossier import DossierWriter
    from cascade.decompose.dossier_store import dossier_stats, write_dossier, written_scenarios
    from cascade.ledger.store import load_scenarios
    from cascade.llm.client import LLMClient

    settings = _settings(config)
    _require_provider_ready(settings)

    scenarios = sorted(load_scenarios(settings, role="admin"), key=lambda item: item.scenario_id)
    if not scenarios:
        _fail("scenario registry is empty; run `cascade ledger build` first", EXIT_PRECONDITION)
    scenarios = _scorable(scenarios, what="the dossier writer")
    done = set() if rebuild else written_scenarios(settings)
    pending = [item for item in scenarios if item.scenario_id not in done]
    if limit is not None:
        pending = pending[:limit]
    console.print(
        f"writing [bold]{len(pending)}[/bold] dossier(s); "
        f"{len(scenarios) - len(pending)} already done"
    )

    search, fence = _chronofence_hits(settings)
    writer = DossierWriter(
        settings=settings, client=LLMClient(settings, phase="dossier"), search=search
    )
    try:
        for index, scenario in enumerate(pending, start=1):
            outcome = writer.write(scenario)
            write_dossier(settings, outcome)
            console.print(
                f"  [{index}/{len(pending)}] {scenario.scenario_id}: "
                f"{outcome.dossier.n_claims} claims kept, {len(outcome.dropped)} refused, "
                f"{outcome.n_excerpts} excerpts"
            )
    finally:
        fence.__exit__(None, None, None)

    stats = dossier_stats(settings)
    console.print(
        f"dossiers [bold]{stats.written}[/bold] of {len(scenarios)} "
        f"({stats.empty} empty) · claims kept [bold]{stats.claims}[/bold] · refused "
        f"[bold]{stats.dropped}[/bold] ({stats.dropped_rate:.1%} of emitted) · "
        f"mean excerpts {stats.mean_excerpts:.1f}"
    )
    for reason, count in stats.dropped_by_reason:
        console.print(f"  refused · {reason}: {count}")


@compile_app.command("status")
def compile_status(config: OverlayOpt = None) -> None:
    """Report measured graph statistics and the repair-retry histogram."""
    from cascade.decompose.store import compile_stats

    settings = _settings(config)
    _print_compile_stats(
        compile_stats(settings, expected_scenarios=_scorable_count(settings)), settings
    )


@compile_app.command("verify")
def compile_verify(config: OverlayOpt = None) -> None:
    """Assert every stored graph is valid and hashes to its recorded digest.

    Re-runs the full §5.3 validator against what is actually in the database,
    not against what the compiler believed at write time. Exits 3 on any
    violation, hash mismatch, or missing scenario.
    """
    from cascade.corpus.embed import Embedder
    from cascade.decompose.dossier_store import verify_dossier_hashes
    from cascade.decompose.store import compile_stats, verify_hashes
    from cascade.decompose.validator import validate
    from cascade.ledger.store import load_scenarios

    settings = _settings(config)
    failures: list[str] = []

    # A dossier is evidence the agents act on; an edited row is an edited
    # experiment, so it is held to the same hash check as a graph.
    failures.extend(
        f"{item.scenario_id}: dossier hash mismatch (recorded {item.recorded[:12]}, "
        f"recomputed {item.recomputed[:12]})"
        for item in verify_dossier_hashes(settings)
    )

    mismatches = verify_hashes(settings)
    if mismatches:
        failures.append(
            f"{len(mismatches)} stored graph(s) do not match their recorded hash: "
            + ", ".join(item.scenario_id for item in mismatches[:5])
        )

    scenarios = {item.scenario_id: item for item in load_scenarios(settings, role="admin")}
    stats = compile_stats(settings, expected_scenarios=_scorable_count(settings))

    embedder = Embedder(
        model_name=settings.models.embedding, batch_size=settings.corpus.embed_batch_size
    )
    invalid: list[str] = []
    if stats.compiled:
        embedder.load()
        from cascade.decompose.store import load_graphs

        for graph in load_graphs(settings, role="admin"):
            scenario = scenarios.get(graph.scenario_id)
            if scenario is None:
                invalid.append(f"{graph.scenario_id}: no such scenario in the registry")
                continue
            outcome_text = f"{scenario.question} {scenario.resolution_criterion}".strip()
            report = validate(graph, outcome_text=outcome_text, embed=embedder.encode)
            if not report.ok:
                invalid.append(f"{graph.scenario_id}: {report.render() or 'checks skipped'}")

    table = Table(title="Causal graph preconditions")
    table.add_column("Check", style="cyan")
    table.add_column("Measured", justify="right")
    table.add_column("", width=3)

    def row(name: str, measured: str, ok: bool) -> None:
        table.add_row(name, measured, "[green]OK[/green]" if ok else "[red]NO[/red]")

    row(
        "graphs stored",
        f"{stats.compiled:,}/{stats.scenarios:,}",
        stats.compiled == stats.scenarios,
    )
    row(
        "hash matches content",
        f"{stats.compiled - len(mismatches):,}/{stats.compiled:,}",
        not mismatches,
    )
    row(
        "passes the §5.3 validator",
        f"{stats.compiled - len(invalid):,}/{stats.compiled:,}",
        not invalid,
    )
    row("hard failures logged", f"{stats.failed:,}", stats.failed == 0)
    console.print(table)

    if invalid:
        for line in invalid[:10]:
            err_console.print(f"  [red]{line}[/red]")
        failures.append(f"{len(invalid)} stored graph(s) fail the validator")
    if stats.compiled == 0:
        failures.append("no graphs are stored; run `cascade compile build`")
    if stats.failed:
        failures.append(f"{stats.failed} scenario(s) are logged as hard failures")

    if failures:
        _fail("; ".join(failures), EXIT_PRECONDITION)
    console.print("[bold green]causal graphs: all preconditions hold[/bold green]")


@compile_app.command("audit")
def compile_audit(
    config: OverlayOpt = None,
    score: Annotated[
        bool,
        typer.Option("--score", help="Score a completed worksheet instead of writing one."),
    ] = False,
) -> None:
    """Write or score the seeded 20-graph human audit (spec §5.4).

    Without --score, samples 20 graphs and writes `rubric.md` and
    `worksheet.json` under `reports/audit/` for a human to fill in. With
    --score, reads the completed worksheet and publishes the mean; exits 3 when
    the mean is below the §5.4 threshold, because a compiler below it cannot be
    fixed by tuning anything downstream.
    """
    from pathlib import Path

    from cascade.decompose.audit import (
        PASS_THRESHOLD,
        sample_scenarios,
        score_worksheet,
        summarise,
        write_worksheet,
    )
    from cascade.decompose.store import load_graph
    from cascade.ledger.store import load_scenarios

    settings = _settings(config)
    directory = Path(settings.paths.reports) / "audit"

    if score:
        worksheet = directory / "worksheet.json"
        if not worksheet.is_file():
            _fail(
                f"no worksheet at {worksheet}; run `cascade compile audit` first", EXIT_PRECONDITION
            )
        scores = score_worksheet(worksheet)
        summary = summarise(scores)

        table = Table(title="Graph audit (spec §5.4)")
        table.add_column("Axis", style="cyan")
        table.add_column("Mean", justify="right")
        for axis, mean in summary.per_axis:
            table.add_row(axis, f"{mean:.3f}")
        table.add_row("[bold]overall[/bold]", f"[bold]{summary.mean:.3f}[/bold]")
        console.print(table)
        console.print(f"scored [bold]{summary.scored}[/bold] graph(s); threshold {PASS_THRESHOLD}")
        if summary.worst:
            console.print(
                "lowest scoring: " + ", ".join(f"{sid} ({m:.2f})" for sid, m in summary.worst)
            )

        if summary.scored == 0:
            _fail("worksheet has no completed scores", EXIT_PRECONDITION)
        if not summary.passed:
            _fail(
                f"audit mean {summary.mean:.3f} is below {PASS_THRESHOLD}; per §5.4 the "
                "compiler prompt is the problem and simulation tuning will not fix it",
                EXIT_PRECONDITION,
            )
        console.print("[bold green]audit: mean meets the §5.4 threshold[/bold green]")
        return

    # The sample is drawn from the scenarios the study scores: a stand-in
    # excluded by ADR-0043 has no graph by design, and auditing the quality of
    # a decomposition nobody compiled is not a review, it is a missing file.
    scenarios = {
        item.scenario_id: item
        for item in _scorable(load_scenarios(settings, role="admin"), what="the audit sample")
    }
    if not scenarios:
        _fail("scenario registry is empty", EXIT_PRECONDITION)

    chosen = sample_scenarios(sorted(scenarios), salt=settings.study.salt)
    graphs = []
    missing = []
    for scenario_id in chosen:
        graph = load_graph(settings, scenario_id, role="admin")
        if graph is None:
            missing.append(scenario_id)
            continue
        graphs.append((scenario_id, scenarios[scenario_id].question, graph))

    if missing and not graphs:
        _fail(
            "no sampled scenario has a compiled graph -- run `cascade compile build` first",
            EXIT_PRECONDITION,
        )
    if missing:
        # A sampled scenario that could not be compiled is part of the record,
        # not a reason to withhold the review: the sample is drawn before
        # anything is compiled, so dropping the audit whenever one scenario
        # hard-fails would make the audit conditional on the compile being
        # perfect -- and it is the compile's failures a reviewer most wants to
        # know about. The count travels with the worksheet.
        console.print(
            f"[yellow]{len(missing)} sampled scenario(s) have no graph[/yellow] and are "
            "recorded as hard failures, not reviewed: " + ", ".join(missing)
        )

    rubric_path, worksheet_path = write_worksheet(
        directory, graphs=graphs, salt=settings.study.salt
    )
    console.print(f"sampled [bold]{len(graphs)}[/bold] graph(s), seeded from the study salt")
    console.print(f"  rubric:    {rubric_path}")
    console.print(f"  worksheet: {worksheet_path}")
    console.print(
        "[dim]Fill in every score, then run `cascade compile audit --score`. "
        "The graphs are shown without their outcomes on purpose (§5.4).[/dim]"
    )


# ---------------------------------------------------------------------------
# simulate: Loom + Aperture (M5)
# ---------------------------------------------------------------------------

# §7.3's mean activation rate, and the tolerance the spec states for it. Named
# here so the CLI can print the criterion beside the measurement; nothing in
# `cascade/sim/` reads either number.
ACTIVATION_TARGET = 0.347
ACTIVATION_TOLERANCE = 0.04


def _graph_for(settings: Settings, scenario_id: str) -> Any:
    """The graph this configuration simulates -- compiled, or the generic panel.

    Appendix C's factor A. With `causal_decomposition: false` there is no
    compiled decomposition to run, so the kernel is given a generic persona
    panel instead (ADR-0025). Returns ``None`` when decomposition is on and no
    graph has been compiled, so every caller can report the missing input in
    its own words.
    """
    from cascade.decompose.store import load_graph
    from cascade.eval.panel import panel_graph

    if not settings.flags.causal_decomposition:
        return panel_graph(scenario_id, actors=settings.flags.panel_actors)
    return load_graph(settings, scenario_id, role="admin")


def _load_simulation(settings: Settings, scenario_id: str, *, policy: str) -> Any:
    """Wire a Loom for one scenario: graph, policies, and whatever decides.

    Retrieval for the agent prefixes runs as `eval` for the same reason the
    compiler's does -- it is preparation, not a run -- and it goes through
    Chronofence either way, so the time lock is identical.
    """
    from cascade.aperture.policy import derive_policies
    from cascade.ledger.store import load_scenarios
    from cascade.sim.kernel import Loom, mechanics_from

    graph = _graph_for(settings, scenario_id)
    if graph is None:
        _fail(
            f"scenario {scenario_id!r} has no compiled graph; run `cascade compile build` first",
            EXIT_PRECONDITION,
        )
    scenarios = {item.scenario_id: item for item in load_scenarios(settings, role="admin")}
    scenario = scenarios.get(scenario_id)
    if scenario is None:
        _fail(f"scenario {scenario_id!r} is not in the registry", EXIT_PRECONDITION)

    policies = derive_policies(
        graph, settings.aperture, asymmetry=settings.flags.information_asymmetry
    )

    if policy == "agent":
        decider: Any = _agent_policy(settings, [(scenario, graph)])
    else:
        mechanics = mechanics_from(graph)
        from cascade.sim.policies import HeuristicPolicy

        decider = HeuristicPolicy(
            utility={actor_id: dict(mechanics[actor_id].utility) for actor_id in sorted(mechanics)}
        )
    return Loom(settings=settings, graph=graph, policies=policies, decider=decider), graph


def _baseline_evidence(settings: Settings, fence: Any, embedder: Any, scenario: Any) -> Any:
    """The single-model baselines' evidence, under the configured retrieval mode.

    §10.2 gives a baseline "the same Chronofence evidence an agent gets": were
    the agents moved to hybrid retrieval and the baselines left on vector, the
    headline comparison would measure retrieval rather than architecture.
    """
    from cascade.retrieval.queries import baseline_evidence_query

    query = baseline_evidence_query(scenario.question, scenario.resolution_criterion)
    vector = embedder.encode([query.text])[0]
    return fence.retrieve(
        vector,
        text=query.keyword_text,
        entities=query.entities,
        as_of=scenario.cutoff_ts,
        k=settings.retrieval.k_agent,
    )


@contextmanager
def _maybe_chronofence(settings: Settings, *, enabled: bool) -> Iterator[Any]:
    """Open a Chronofence, or yield ``None`` under `grounding: parametric_only`.

    A context manager rather than a conditional at each call site so the
    ungrounded arm cannot accidentally hold an open connection to the corpus
    it is meant not to read.
    """
    if not enabled:
        yield None
        return
    from cascade.retrieval.search import Chronofence

    with Chronofence(settings, role="eval") as fence:
        yield fence


def _agent_policy(settings: Settings, pairs: Any) -> Any:
    """Build one model-backed decider covering every (scenario, actor) given.

    Evidence is retrieved once per (scenario, actor), not once per step
    (ADR-0019): `as_of` is the scenario cutoff for the whole run, so a per-step
    query would re-retrieve the same chunks 24 times, cost 4.2M vector searches
    across the study, and put the dynamic half of the prompt an order of
    magnitude over the 260 tokens §12.1 budgets for it.

    One decider for every scenario rather than one each, because the M6
    wavefront batches a whole step across the wave: a per-scenario decider
    would turn one submission into 180.

    Appendix C's factor C lives here. Under `grounding: parametric_only` no
    retrieval happens at all -- no embedder is loaded and no Chronofence
    connection is opened -- and every actor's prefix carries an explicit
    statement that evidence was withheld rather than an empty section the
    model would have to interpret. That arm also loses the cache floor: the
    evidence block is what carries the prefix over 4,096 tokens (ADR-0019), so
    parametric cells are legitimately more expensive per call, and the warning
    below reports it rather than hiding it.
    """
    from cascade.aperture.policy import counterparties, derive_policies, levers_by_actor
    from cascade.llm.client import LLMClient
    from cascade.retrieval.queries import agent_evidence_query
    from cascade.sim.agent import LLMAgents, prepare_actor
    from cascade.sim.prompts import brief_from

    _require_provider_ready(settings)
    grounded = settings.flags.grounding == "chronofence"
    if grounded:
        from cascade.corpus.embed import Embedder

        embedder: Any = Embedder(
            model_name=settings.models.embedding, batch_size=settings.corpus.embed_batch_size
        )
        embedder.load()
    else:
        embedder = None

    situations = _situations(
        settings,
        [scenario.scenario_id for scenario, _ in pairs],
        role="sim",
        grounded=grounded,
    )

    prepared: dict[tuple[str, str], Any] = {}
    with _maybe_chronofence(settings, enabled=grounded) as fence:
        for scenario, graph in pairs:
            policies = derive_policies(
                graph, settings.aperture, asymmetry=settings.flags.information_asymmetry
            )
            levers = levers_by_actor(graph)
            parties = counterparties(policies)
            context = f"{scenario.question}\n\nResolution: {scenario.resolution_criterion}"
            for actor in sorted(graph.actors, key=lambda a: a.id):
                if fence is None or embedder is None:
                    evidence: tuple[tuple[str, str, str], ...] = ()
                else:
                    query = agent_evidence_query(scenario.question, actor.name, actor.objective)
                    vector = embedder.encode([query.text])[0]
                    found = fence.retrieve(
                        vector,
                        text=query.keyword_text,
                        entities=query.entities,
                        as_of=scenario.cutoff_ts,
                        k=settings.retrieval.k_agent,
                    )
                    evidence = tuple(
                        (chunk.published_at.isoformat(), chunk.source, chunk.body)
                        for chunk in found.chunks
                    )
                brief = brief_from(
                    actor,
                    levers={
                        factor: weight
                        for factor, weight in sorted(_leverage_of(graph, actor.id).items())
                        if factor in levers.get(actor.id, ())
                    },
                    counterparties=parties.get(actor.id, ()),
                    horizon=settings.kernel.steps,
                    question_context=context,
                    evidence=evidence,
                    grounded=grounded,
                    situation=situations.get(scenario.scenario_id, ("", None))[0],
                )
                prepared[(scenario.scenario_id, actor.id)] = prepare_actor(brief, settings)

    short = [key for key in sorted(prepared) if not prepared[key].cacheable]
    if short:
        console.print(
            f"[yellow]{len(short)} of {len(prepared)} actor prefixes are below the "
            f"{settings.prompt_cache.min_prefix_tokens}-token cache floor[/yellow] and are sent "
            "unmarked: marking them would pay the write premium for a cache the provider "
            "silently declines (ADR-0001)."
        )
    return LLMAgents(
        settings=settings, client=LLMClient(settings, phase="simulate"), prepared=prepared
    )


def _leverage_of(graph: Any, actor_id: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for edge in sorted(graph.edges, key=lambda e: (e.src, e.dst, e.lag)):
        if edge.src == actor_id:
            out[edge.dst] = max(out.get(edge.dst, 0.0), float(edge.weight))
    return out


def _print_run_table(results: Any) -> None:
    """Print what the runs measured. Targets are printed beside, never into."""
    table = Table(title="Runs (M5, spec §7)")
    table.add_column("run", style="cyan")
    table.add_column("steps", justify="right")
    table.add_column("end", justify="right")
    table.add_column("decisions", justify="right")
    table.add_column("activation", justify="right")
    table.add_column("outcome", justify="right")
    table.add_column("event hash", justify="right")
    for result in results:
        table.add_row(
            f"{result.spec.scenario_id}#{result.spec.replicate}",
            str(result.steps_run),
            result.termination,
            str(result.decisions),
            f"{result.activation_rate:.3f}",
            f"{result.outcome_score:.4f}",
            result.event_log_hash[:12],
        )
    console.print(table)


@simulate_app.command("run")
def simulate_run(
    config: OverlayOpt = None,
    scenario: Annotated[
        str | None, typer.Option("--scenario", help="Scenario id to simulate.")
    ] = None,
    replicates: Annotated[
        int, typer.Option("--replicates", help="How many seeded replicates to run.")
    ] = 1,
    policy: Annotated[
        str,
        typer.Option(
            "--policy",
            help="'agent' (the pinned model) or 'heuristic' (a stand-in; runs are stamped).",
        ),
    ] = "agent",
    store: Annotated[
        bool, typer.Option("--store/--no-store", help="Write runs, steps and events to Postgres.")
    ] = True,
) -> None:
    """Run seeded replicates of one compiled scenario (M5, spec §7.2).

    Resumable: replicates already stored for this (scenario, config) are
    skipped, because a run row exists only for a run that finished.
    """
    import uuid
    from datetime import UTC, datetime

    import numpy as np

    from cascade.decompose.schema import graph_hash
    from cascade.sim.kernel import RunSpec
    from cascade.sim.rng import run_seed
    from cascade.trace.store import completed_replicates, write_run

    settings = _settings(config)
    if policy not in {"agent", "heuristic"}:
        _fail(f"unknown policy {policy!r}; expected 'agent' or 'heuristic'", EXIT_PRECONDITION)
    if scenario is None:
        _fail("--scenario is required at M5; the 36,000-run fan-out lands at M6", EXIT_PRECONDITION)

    config_id = config or "base"
    loom, graph = _load_simulation(settings, scenario, policy=policy)
    done = (
        completed_replicates(settings, scenario_id=scenario, config_id=config_id)
        if store
        else set()
    )
    todo = [index for index in range(replicates) if index not in done]
    if not todo:
        console.print(f"nothing to do: {len(done)} replicate(s) already stored")
        return
    if policy != "agent":
        console.print(
            "[yellow]policy=heuristic[/yellow]: these runs are stamped in the `runs` table and "
            "are not study data. No acceptance number may be read off them."
        )

    results = []
    started = datetime.now(UTC)
    for replicate in todo:
        spec = RunSpec(
            run_id=str(
                uuid.uuid5(uuid.NAMESPACE_URL, f"cascade/{scenario}/{config_id}/{replicate}")
            ),
            scenario_id=scenario,
            config_id=config_id,
            replicate=replicate,
            policy=policy,
        )
        result = loom.run(spec)
        results.append(result)
        if store:
            write_run(
                settings,
                result,
                started_at=started,
                graph_sha256=graph_hash(graph),
                run_seed=run_seed(
                    scenario_id=scenario,
                    config_id=config_id,
                    replicate=replicate,
                    salt=settings.study.salt,
                ),
                numpy_version=np.__version__,
            )
    _print_run_table(results)
    mean_rate = sum(r.activation_rate for r in results) / len(results)
    mean_events = sum(r.decisions for r in results) / len(results)
    console.print(
        f"mean activation [bold]{mean_rate:.4f}[/bold] over {len(results)} run(s); "
        f"mean decisions/run [bold]{mean_events:.1f}[/bold]"
    )


@simulate_app.command("activation")
def simulate_activation(
    config: OverlayOpt = None,
    runs: Annotated[int, typer.Option("--runs", help="Sample size, spread over scenarios.")] = 50,
    policy: Annotated[str, typer.Option("--policy")] = "agent",
    replicates: Annotated[
        int, typer.Option("--replicates", help="Replicates per sampled scenario.")
    ] = 1,
) -> None:
    """Measure the mean activation rate over a sample of runs (M5 criterion 2).

    Prints the measured rate against §7.3's 34.7% +/- 4 points and exits 3 when
    a model-driven sample misses the band -- a scheduler that drifts to 90%
    multiplies cost by three and one that drifts to 10% has stopped simulating
    interaction. A sample run under a stand-in policy reports and does not
    gate, because the number it produces is not about the agents.
    """
    import uuid

    from cascade.decompose.store import completed_scenarios
    from cascade.sim.kernel import RunSpec

    settings = _settings(config)
    config_id = config or "base"
    available = sorted(completed_scenarios(settings))
    if not available:
        _fail(
            "no compiled graphs; the activation rate is a property of real "
            "decompositions, so run `cascade compile build` first",
            EXIT_PRECONDITION,
        )

    per_step: dict[int, list[float]] = {}
    rates: list[float] = []
    decisions: list[int] = []
    sampled = 0
    for scenario_id in available:
        if sampled >= runs:
            break
        loom, _ = _load_simulation(settings, scenario_id, policy=policy)
        for replicate in range(replicates):
            if sampled >= runs:
                break
            result = loom.run(
                RunSpec(
                    run_id=str(
                        uuid.uuid5(
                            uuid.NAMESPACE_URL,
                            f"cascade/{scenario_id}/{config_id}/{replicate}",
                        )
                    ),
                    scenario_id=scenario_id,
                    config_id=config_id,
                    replicate=replicate,
                    policy=policy,
                )
            )
            rates.append(result.activation_rate)
            decisions.append(result.decisions)
            for record in result.step_records:
                per_step.setdefault(record.step, []).append(record.activation_rate)
            sampled += 1

    if not rates:
        _fail("no runs were executed", EXIT_PRECONDITION)

    mean_rate = sum(rates) / len(rates)
    mean_decisions = sum(decisions) / len(decisions)
    table = Table(title="Activation (M5, spec §7.3)")
    table.add_column("Quantity", style="cyan")
    table.add_column("Measured", justify="right")
    table.add_column("Criterion", justify="right")
    table.add_row("runs sampled", str(len(rates)), "50")
    table.add_row(
        "mean activation rate",
        f"{mean_rate:.4f}",
        f"{ACTIVATION_TARGET:.3f} +/- {ACTIVATION_TOLERANCE:.2f}",
    )
    table.add_row("mean decisions / run", f"{mean_decisions:.1f}", "116.6")
    table.add_row("min / max run rate", f"{min(rates):.3f} / {max(rates):.3f}", "-")
    console.print(table)

    profile = ", ".join(
        f"s{step}={sum(values) / len(values):.2f}"
        for step, values in sorted(per_step.items())
        if step < 8
    )
    console.print(f"[dim]per-step (first 8): {profile}[/dim]")

    if policy != "agent":
        console.print(
            f"[yellow]policy={policy}[/yellow]: measured under a stand-in decider, so this is a "
            "diagnostic of the scheduler, not the M5 acceptance figure."
        )
        return
    if abs(mean_rate - ACTIVATION_TARGET) > ACTIVATION_TOLERANCE:
        _fail(
            f"mean activation {mean_rate:.4f} is outside "
            f"{ACTIVATION_TARGET:.3f} +/- {ACTIVATION_TOLERANCE:.2f} (spec §7.3)",
            EXIT_PRECONDITION,
        )
    console.print("[bold green]activation rate is inside the §7.3 band[/bold green]")


@simulate_app.command("status")
def simulate_status(config: OverlayOpt = None) -> None:
    """Report measured figures over the stored runs."""
    from cascade.trace.store import run_stats

    settings = _settings(config)
    stats = run_stats(settings, config_id=config)
    table = Table(title="Simulation (M5/M6)")
    table.add_column("Quantity", style="cyan")
    table.add_column("Measured", justify="right")
    table.add_row("runs", str(stats.runs))
    table.add_row("scenarios", str(stats.scenarios))
    table.add_row("configs", ", ".join(stats.configs) or "-")
    table.add_row("policies", ", ".join(stats.policies) or "-")
    table.add_row("decision events", f"{stats.events:,}")
    table.add_row("mean events / run", f"{stats.mean_events_per_run:.1f}")
    table.add_row("mean activation rate", f"{stats.mean_activation_rate:.4f}")
    table.add_row("mean steps / run", f"{stats.mean_steps:.2f}")
    table.add_row("runs ended by absorption", str(stats.absorbed_runs))
    table.add_row("action-cache hit rate", f"{stats.cache_hit_rate:.4f}")
    table.add_row("distinct event-log hashes", str(stats.distinct_event_hashes))
    console.print(table)


# ---------------------------------------------------------------------------
# simulate all / estimate: the fan-out (M6, spec §2.2 PHASE 2)
# ---------------------------------------------------------------------------

# §7.3's derivation: 36,000 runs x 24 steps x 4.86 mean active actors. Printed
# beside the measurement, never used to produce one.
TARGET_EVENTS = 4_199_040
EVENT_TOLERANCE = 0.05
TARGET_CACHE_HIT_RATE = 0.88


def _fanout_wiring(settings: Settings, *, policy: str, scenario_ids: Sequence[str]) -> Any:
    """Build the pieces a fan-out needs: a decider, a Loom factory, a writer."""
    from datetime import UTC, datetime

    import numpy as np

    from cascade.aperture.policy import derive_policies
    from cascade.decompose.schema import graph_hash
    from cascade.ledger.store import load_scenarios
    from cascade.sim.kernel import Loom, mechanics_from
    from cascade.sim.rng import run_seed
    from cascade.trace.store import write_run

    scenarios = {item.scenario_id: item for item in load_scenarios(settings, role="admin")}
    graphs: dict[str, Any] = {}
    pairs = []
    for scenario_id in scenario_ids:
        graph = _graph_for(settings, scenario_id)
        if graph is None:
            _fail(
                f"scenario {scenario_id!r} has no compiled graph; run `cascade compile build`",
                EXIT_PRECONDITION,
            )
        graphs[scenario_id] = graph
        pairs.append((scenarios[scenario_id], graph))

    shared: Any = _agent_policy(settings, pairs) if policy == "agent" else None

    def loom_for(scenario_id: str) -> Any:
        graph = graphs[scenario_id]
        if shared is not None:
            decider: Any = shared
        else:
            from cascade.sim.policies import HeuristicPolicy

            mechanics = mechanics_from(graph)
            decider = HeuristicPolicy(
                utility={
                    actor_id: dict(mechanics[actor_id].utility) for actor_id in sorted(mechanics)
                }
            )
        return Loom(
            settings=settings,
            graph=graph,
            policies=derive_policies(
                graph, settings.aperture, asymmetry=settings.flags.information_asymmetry
            ),
            decider=decider,
        )

    started = datetime.now(UTC)

    def on_complete(result: Any) -> None:
        write_run(
            settings,
            result,
            started_at=started,
            graph_sha256=graph_hash(graphs[result.spec.scenario_id]),
            run_seed=run_seed(
                scenario_id=result.spec.scenario_id,
                config_id=result.spec.config_id,
                replicate=result.spec.replicate,
                salt=settings.study.salt,
            ),
            numpy_version=np.__version__,
        )

    return loom_for, on_complete, shared


def _print_fanout(report: Any, *, skipped: int, policy: str) -> None:
    """Print the M6 acceptance figures. Measured, never targeted."""
    table = Table(title="Fan-out (M6, spec §2.2 PHASE 2)")
    table.add_column("Quantity", style="cyan")
    table.add_column("Measured", justify="right")
    table.add_column("Criterion", justify="right")
    table.add_row("runs completed", f"{report.completed:,}", "36,000")
    table.add_row("runs skipped (already stored)", f"{skipped:,}", "-")
    table.add_row("decision events", f"{report.decisions:,}", f"{TARGET_EVENTS:,} +/- 5%")
    table.add_row("decisions / run", f"{report.decisions_per_run:.1f}", "116.6")
    table.add_row(
        "action-cache hit rate",
        f"{report.cache_hit_rate:.4f}" if report.llm_calls else "n/a",
        f">= {TARGET_CACHE_HIT_RATE:.2f}",
    )
    table.add_row("batch submissions", f"{report.batches:,}", "-")
    table.add_row("turns batched", f"{report.batched_turns:,}", "-")
    table.add_row("waves / steps", f"{report.waves:,} / {report.steps:,}", "-")
    table.add_row("elapsed", f"{report.elapsed_s:.1f}s", "-")
    console.print(table)
    if policy != "agent":
        console.print(
            f"[yellow]policy={policy}[/yellow]: stamped in the runs table and not study data."
        )


@simulate_app.command("all")
def simulate_all(
    config: OverlayOpt = None,
    replicates: Annotated[
        int | None,
        typer.Option("--replicates", help="Replicates per scenario; default from config."),
    ] = None,
    wave: Annotated[
        int,
        typer.Option("--wave", help="Runs advanced in lockstep; larger means fewer batches."),
    ] = 200,
    policy: Annotated[str, typer.Option("--policy")] = "agent",
    limit: Annotated[
        int | None, typer.Option("--limit", help="Use at most N scenarios (smoke runs).")
    ] = None,
) -> None:
    """Run the seeded ensemble across every compiled scenario (M6, PHASE 2).

    Resumable: a run row exists only for a run that finished, so restarting
    picks up exactly the runs that did not. Costs money in `record` mode and
    the `simulate` phase ceiling applies -- a breach aborts with exit 2 and
    every completed run stays written.
    """
    from cascade.decompose.store import completed_scenarios
    from cascade.ensemble.runner import EnsembleRunner
    from cascade.trace.store import completed_replicates

    settings = _settings(config)
    if policy not in {"agent", "heuristic"}:
        _fail(f"unknown policy {policy!r}; expected 'agent' or 'heuristic'", EXIT_PRECONDITION)
    config_id = config or "base"
    count = replicates if replicates is not None else settings.ensemble.replicates

    scenario_ids = sorted(completed_scenarios(settings))
    if not scenario_ids:
        _fail(
            "no compiled graphs; run `cascade compile build` before simulating",
            EXIT_PRECONDITION,
        )
    if limit is not None:
        scenario_ids = scenario_ids[:limit]

    loom_for, on_complete, _ = _fanout_wiring(settings, policy=policy, scenario_ids=scenario_ids)
    runner = EnsembleRunner(
        settings=settings,
        loom_for=loom_for,
        on_complete=on_complete,
        policy=policy,
        completed=lambda scenario_id, cfg: completed_replicates(
            settings, scenario_id=scenario_id, config_id=cfg
        ),
    )
    tasks, skipped = runner.plan(scenario_ids=scenario_ids, config_id=config_id, replicates=count)
    console.print(
        f"{len(scenario_ids)} scenario(s) x {count} replicate(s): "
        f"[bold]{len(tasks)}[/bold] to run, {skipped} already stored"
    )
    if not tasks:
        return
    report = runner.execute(tasks, wave=wave)
    _print_fanout(report, skipped=skipped, policy=policy)


@simulate_app.command("estimate")
def simulate_estimate(
    config: OverlayOpt = None,
    units: Annotated[
        int, typer.Option("--units", help="Sample runs to measure before extrapolating.")
    ] = 20,
    replicates: Annotated[int | None, typer.Option("--replicates")] = None,
    policy: Annotated[str, typer.Option("--policy")] = "agent",
) -> None:
    """Measure a sample of runs and extrapolate the phase (spec §12.4).

    "No full phase launches without it." Runs `--units` real runs, then scales
    what they measured -- decisions, tokens, spend -- to the full grid and
    compares the projection with the configured ceiling. The sample is written
    like any other run, so the estimate is not wasted work.
    """
    from cascade.decompose.store import completed_scenarios
    from cascade.ensemble.runner import EnsembleRunner
    from cascade.trace.store import completed_replicates

    settings = _settings(config)
    config_id = config or "base"
    count = replicates if replicates is not None else settings.ensemble.replicates
    scenario_ids = sorted(completed_scenarios(settings))
    if not scenario_ids:
        _fail("no compiled graphs; run `cascade compile build` first", EXIT_PRECONDITION)

    loom_for, on_complete, decider = _fanout_wiring(
        settings, policy=policy, scenario_ids=scenario_ids
    )
    runner = EnsembleRunner(
        settings=settings,
        loom_for=loom_for,
        on_complete=on_complete,
        policy=policy,
        completed=lambda scenario_id, cfg: completed_replicates(
            settings, scenario_id=scenario_id, config_id=cfg
        ),
    )
    tasks, _ = runner.plan(scenario_ids=scenario_ids, config_id=config_id, replicates=count)
    if not tasks:
        _fail("nothing left to run; the phase is already complete", EXIT_PRECONDITION)

    sample = tasks[: max(1, units)]
    report = runner.execute(sample, wave=len(sample))
    total = len(scenario_ids) * count
    scale = total / report.completed if report.completed else 0.0

    meter = getattr(getattr(decider, "client", None), "meter", None)
    spent = Decimal(str(getattr(meter, "total_usd", 0))) if meter is not None else Decimal(0)
    ceiling = settings.phase_ceiling("simulate")

    table = Table(title="Phase estimate (spec §12.4)")
    table.add_column("Quantity", style="cyan")
    table.add_column("Sampled", justify="right")
    table.add_column("Projected", justify="right")
    table.add_row("runs", f"{report.completed:,}", f"{total:,}")
    table.add_row("decision events", f"{report.decisions:,}", f"{int(report.decisions * scale):,}")
    table.add_row(
        "uncached model calls",
        f"{report.llm_calls - report.cache_hits:,}",
        f"{int((report.llm_calls - report.cache_hits) * scale):,}",
    )
    table.add_row("spend (USD)", f"${spent:.4f}", f"${spent * Decimal(str(scale)):.2f}")
    table.add_row("ceiling (USD)", "-", f"${ceiling:.2f}")
    console.print(table)
    console.print(
        "[dim]Projected from a sample: the cache hit rate rises as replicates accumulate, "
        "so a first-runs sample overstates the cost of the rest.[/dim]"
    )
    if spent * Decimal(str(scale)) > ceiling:
        _fail(
            f"projected ${spent * Decimal(str(scale)):.2f} exceeds the simulate ceiling "
            f"${ceiling:.2f}; cut replicates before cutting scenarios (spec §15)",
            EXIT_BUDGET_BREACH,
        )


# ---------------------------------------------------------------------------
# ensemble: Chorus (M6, spec §9, PHASE 3)
# ---------------------------------------------------------------------------


@ensemble_app.command("collapse")
def ensemble_collapse(config: OverlayOpt = None) -> None:
    """Collapse stored runs into forecasts (spec §9.1, PHASE 3).

    Runs as `cascade_sim`, which is the point: collapsing must not be able to
    see an outcome, and the role that cannot read `scenario_labels` is the one
    that proves it.
    """
    from cascade.ensemble.aggregate import collapse
    from cascade.ensemble.store import collapse_inputs, write_forecast

    settings = _settings(config)
    config_id = config or "base"
    inputs = collapse_inputs(settings, config_id=config_id)
    if not inputs:
        _fail(
            f"no runs stored for config {config_id!r}; run `cascade simulate all` first",
            EXIT_PRECONDITION,
        )

    written = 0
    for item in inputs:
        forecast = collapse(
            item.scores,
            scenario_id=item.scenario_id,
            config_id=item.config_id,
            salt=settings.study.salt,
            sigma_threshold=settings.ensemble.sigma_multimodal_threshold,
            bootstrap_b=settings.ensemble.bootstrap_b,
            mean_events=item.mean_events,
            mean_steps=item.mean_steps,
            absorbed_runs=item.absorbed_runs,
            policy=item.policy,
        )
        write_forecast(settings, forecast)
        written += 1
    console.print(f"collapsed [bold]{written}[/bold] scenario(s) for config {config_id!r}")
    _print_forecasts(settings, config_id)


@ensemble_app.command("status")
def ensemble_status(config: OverlayOpt = None) -> None:
    """Report measured dispersion over the stored forecasts (spec §9.2)."""
    settings = _settings(config)
    _print_forecasts(settings, config)


def _print_forecasts(settings: Settings, config_id: str | None) -> None:
    from cascade.ensemble.store import forecast_stats

    stats = forecast_stats(settings, config_id=config_id)
    table = Table(title="Forecasts (M6, spec §9)")
    table.add_column("Quantity", style="cyan")
    table.add_column("Measured", justify="right")
    table.add_column("Reference", justify="right")
    table.add_row("forecasts", f"{stats.forecasts:,}", "180 per config")
    table.add_row("scenarios", f"{stats.scenarios:,}", "180")
    table.add_row("configs", ", ".join(stats.configs) or "-", "-")
    table.add_row("mean p_hat", f"{stats.mean_p_hat:.4f}", "-")
    table.add_row("mean sigma", f"{stats.mean_sigma:.4f}", "-")
    table.add_row("multi-modal share", f"{stats.multi_modal_share:.4f}", "~0.31 (§9.2)")
    table.add_row("mean 95% CI width", f"{stats.mean_ci_width:.4f}", "-")
    table.add_row("mean replicates", f"{stats.mean_replicates:.1f}", "200")
    console.print(table)


@ensemble_app.command("convergence")
def ensemble_convergence(config: OverlayOpt = None) -> None:
    """Print §9.3's replicate-count convergence curve.

    "That plot is what turns 'we ran it 200 times' into 'we ran it 200 times
    because 200 is where it converges'" -- or, if it has not converged by 200,
    an honest statement that it has not.
    """
    from cascade.ensemble.aggregate import convergence_curve
    from cascade.ensemble.store import scores_by_scenario

    settings = _settings(config)
    config_id = config or "base"
    scores = scores_by_scenario(settings, config_id=config_id)
    if not scores:
        _fail(f"no runs stored for config {config_id!r}", EXIT_PRECONDITION)

    curve = convergence_curve(scores)
    if not curve:
        _fail(
            "no scenario has enough replicates to reach the first rung of the curve",
            EXIT_PRECONDITION,
        )
    table = Table(title="Replicate convergence (spec §9.3)")
    table.add_column("n", justify="right", style="cyan")
    table.add_column("mean |delta p_hat|", justify="right")
    table.add_column("mean sigma", justify="right")
    table.add_column("scenarios", justify="right")
    for point in curve:
        table.add_row(
            str(point.n),
            f"{point.mean_abs_change:.4f}",
            f"{point.mean_sigma:.4f}",
            str(point.scenarios),
        )
    console.print(table)


# ---------------------------------------------------------------------------
# eval: Assay (M7, spec §10)
# ---------------------------------------------------------------------------


def _frozen_split(settings: Settings) -> FrozenSplit:
    """Assert §1.3's frozen split before a single label is read.

    Every path under `cascade eval` goes through here first. The assertion is
    worth nothing if it happens once the numbers are already out, so it happens
    before them -- and a mismatch exits 3 rather than warning, because the
    correct response to a moved split is never to carry on.
    """
    from cascade.eval.store import SplitNotSealed, assert_frozen_split
    from cascade.ledger.manifest import ManifestMismatch

    try:
        return assert_frozen_split(settings)
    except SplitNotSealed as exc:
        _fail(str(exc), EXIT_PRECONDITION)
    except ManifestMismatch as exc:
        _fail(str(exc), EXIT_PRECONDITION)


PartitionOpt = Annotated[
    str,
    typer.Option(
        "--partition",
        help=(
            "Which side of the declared dev/test split to measure on: dev, test or all. "
            "Tuning is only ever legitimate on dev."
        ),
    ),
]


def _partition(value: str) -> Partition:
    """Validate a `--partition` value; exits 3 on anything else."""
    from cascade.eval.split import PARTITIONS

    if value not in PARTITIONS:
        _fail(
            f"unknown partition {value!r}; expected one of {', '.join(PARTITIONS)}",
            EXIT_PRECONDITION,
        )
    return value


def _declared_split(settings: Settings, split: FrozenSplit) -> SplitDeclaration:
    """Re-derive the dev/test split and check it against its pin, or exit 3.

    The second half of the frozen-split discipline. `_frozen_split` establishes
    that the registry is the sealed one; this establishes that the partition of
    it is the declared one -- from the whole registry (never from whichever
    scenarios happen to be scored), through a loader that joins no label, and
    before any number is computed. Every `cascade eval` path that measures
    anything goes through here.
    """
    from cascade.eval.split import assert_declared, declare_study_split
    from cascade.ledger.store import load_scenarios

    registry = load_scenarios(settings, role="eval")
    if len(registry) != split.n_scenarios:
        _fail(
            f"the registry holds {len(registry)} scenarios but the seal covers "
            f"{split.n_scenarios}; the dev/test split is only defined over the sealed set",
            EXIT_PRECONDITION,
        )
    try:
        # The wording goes in so stand-ins are excluded before the split is
        # drawn (ADR-0043); it carries no outcome.
        declaration = declare_study_split(
            [(item.scenario_id, item.domain, item.question) for item in registry],
            salt=split.study_salt,
            dev_size=settings.eval.dev_scenarios,
        )
        assert_declared(declaration, pinned_sha256=settings.eval.split_sha256)
    except (SplitError, ValueError) as exc:
        _fail(str(exc), EXIT_PRECONDITION)
    return declaration


def _declared_configs() -> frozenset[str]:
    """Every configuration named in code before it was run.

    The twelve cells, the five baselines, the supplementary cells, and `base`
    -- what `cascade simulate all` stores the headline configuration under when
    it is run without an overlay. Anything else with forecasts behind it is a
    tuning variant, and is scored on dev alone.
    """
    from cascade.eval.ablation import CELLS
    from cascade.eval.baselines import BASELINES
    from cascade.eval.supplementary import supplementary_ids

    return frozenset(
        {cell.cell_id for cell in CELLS}
        | {spec.config_id for spec in BASELINES}
        | set(supplementary_ids())
        | {"base"}
    )


def _appendix_c_configs() -> frozenset[str]:
    """§10.4's Holm family: "twelve cells plus five baselines", and `base`."""
    from cascade.eval.supplementary import supplementary_ids

    return _declared_configs() - set(supplementary_ids())


def _guard(action: Any) -> Any:
    """Run a split guard, turning its refusal into exit 3 at this boundary."""
    try:
        return action()
    except SplitError as exc:
        _fail(str(exc), EXIT_PRECONDITION)


def _metrics_for_config(
    settings: Settings,
    config_id: str,
    split: FrozenSplit,
    *,
    declaration: SplitDeclaration,
    partition: Partition,
    direct: Sequence[ScoredForecast] | None = None,
) -> tuple[MetricSet | None, tuple[ScoredForecast, ...]]:
    """Score one configuration **on one partition**, or ``(None, ())`` for none.

    The forecasts are restricted to the partition before anything is computed
    from them, so everything returned -- and everything a caller derives from
    the returned rows: calibration, domains, dispersion, the per-scenario CSVs
    -- is a function of that partition alone. Both references are paired on
    the same scenarios (`score.measure`): §10.2's single-model baseline, because
    a capped cell runs 90 scenarios and a partition fewer, and the climatology
    floor for the same reason.
    """
    from cascade.eval.baselines import DIRECT_CONFIG_ID
    from cascade.eval.score import measure
    from cascade.eval.split import select
    from cascade.eval.store import scored_forecasts

    scored = _guard(
        lambda: select(scored_forecasts(settings, config_id=config_id), declaration, partition)
    )
    if config_id == DIRECT_CONFIG_ID:
        reference: Sequence[ScoredForecast] = ()
    elif direct is not None:
        reference = direct
    else:
        reference = scored_forecasts(settings, config_id=DIRECT_CONFIG_ID)
    return (
        measure(
            scored,
            config_id=config_id,
            base_rate=split.base_rate,
            direct=reference,
            salt=split.study_salt,
        ),
        scored,
    )


def _print_partition(declaration: SplitDeclaration, partition: Partition) -> None:
    """Say which partition a number is from, every time one is printed."""
    console.print(
        f"dev/test split [bold]{declaration.sha256[:16]}...[/bold] "
        f"({len(declaration.dev)} dev / {len(declaration.test)} test) -- measuring on "
        f"[bold]{partition}[/bold]"
    )
    if partition == "dev":
        console.print(
            "[dim]dev is the tuning partition: look as often as you like, and never "
            "quote it. The reported figure is `cascade report`, on test.[/dim]"
        )
    else:
        console.print(
            f"[yellow]{partition} includes held-out scenarios.[/yellow] A decision made "
            "after looking at this number is a decision tuned on test."
        )


def _print_metrics(metrics: Any, *, title: str) -> None:
    table = Table(title=title)
    table.add_column("Metric", style="cyan")
    table.add_column("Measured", justify="right")
    table.add_column("Note", overflow="fold")
    table.add_row("scenarios scored", f"{metrics.n:,}", "the denominator of every row below")
    table.add_row(
        "decider policy",
        ", ".join(metrics.policies),
        "'agent' is study data; 'none' is a model-free baseline; "
        "'heuristic'/'mixed' are mechanism checks",
    )
    table.add_row("base rate", f"{metrics.base_rate:.4f}", "of the scored subset")
    table.add_row("mean forecast", f"{metrics.mean_p_hat:.4f}", "")
    table.add_row("Brier", f"{metrics.brier:.6f}", "lower is better")
    table.add_row(
        "BSS vs climatology",
        (
            "not measured"
            if metrics.bss_vs_climatology is None
            else f"{metrics.bss_vs_climatology:.4f}"
        ),
        "1 - BS/BS_climatology",
    )
    table.add_row(
        "BSS vs single-model",
        "not measured" if metrics.bss_vs_direct is None else f"{metrics.bss_vs_direct:.4f}",
        "the §10.2 reference",
    )
    table.add_row("log loss", f"{metrics.log_loss:.6f}", "p clipped to [0.01, 0.99]")
    table.add_row(
        "AUC", "not measured" if metrics.auc is None else f"{metrics.auc:.4f}", "mid-ranks"
    )
    table.add_row("ECE / MCE", f"{metrics.ece:.4f} / {metrics.mce:.4f}", "10 equal-width bins")
    table.add_row(
        "Murphy REL - RES + UNC",
        f"{metrics.murphy.reliability:.6f} - {metrics.murphy.resolution:.6f} "
        f"+ {metrics.murphy.uncertainty:.6f}",
        f"binning residual {metrics.murphy.residual:+.6f}",
    )
    if metrics.brier_recalibrated is not None:
        table.add_row(
            "Brier (isotonic, held out)",
            f"{metrics.brier_recalibrated:.6f}",
            "secondary; the headline is the raw system",
        )
    console.print(table)
    if metrics.provisional:
        console.print(
            f"[yellow]These forecasts were produced by "
            f"{', '.join(metrics.policies)} decider(s), not the study's agents.[/yellow] "
            "The numbers above verify the harness end to end; they are not a result."
        )


@eval_app.command("score")
def eval_score(
    config: OverlayOpt = None,
    config_id: Annotated[
        str | None, typer.Option("--config-id", help="Which stored config to score.")
    ] = None,
    partition: PartitionOpt = "dev",
) -> None:
    """Score one configuration's stored forecasts against the sealed labels.

    Runs as `cascade_eval` and asserts the frozen split first (§1.3). Exits 3
    when the configuration has no forecasts -- a Brier over nothing is not a
    small number, it is not a number.

    Measures on **dev by default**. This is the command a tuning loop calls,
    and the default of a command run fifty times a day must be the partition it
    is safe to look at fifty times a day; the held-out figure takes an explicit
    `--partition test`. A configuration that is not a declared study
    configuration is refused on anything but dev (exit 3).
    """
    from cascade.eval.split import require_declared_config

    settings = _settings(config)
    split = _frozen_split(settings)
    declaration = _declared_split(settings, split)
    target = config_id or config or "C01"
    chosen = _partition(partition)
    _guard(lambda: require_declared_config(target, partition=chosen, declared=_declared_configs()))
    metrics, scored = _metrics_for_config(
        settings, target, split, declaration=declaration, partition=chosen
    )
    if metrics is None:
        from cascade.eval.store import available_configs

        stored = ", ".join(f"{name} ({count})" for name, count in available_configs(settings))
        _fail(
            f"no stored forecasts for config {target!r} on the {chosen} partition; "
            f"stored configs are: {stored or 'none'}",
            EXIT_PRECONDITION,
        )
    console.print(
        f"frozen split [bold]{split.manifest_sha256[:16]}...[/bold] "
        f"({split.n_scenarios} scenarios, base rate {split.base_rate:.4f})"
    )
    _print_partition(declaration, chosen)
    _print_metrics(metrics, title=f"Metrics -- {target} on {chosen} (spec §10.1)")
    _print_calibration(scored)
    _print_domains(scored)


def _fmt_optional(value: float | None, pattern: str) -> str:
    """Render a measurement, or a dash where there is not one.

    A bin with no scenarios has no mean forecast. Printing 0.0000 would put a
    measurement in the table that nobody made.
    """
    return "-" if value is None else pattern.format(value)


def _print_calibration(scored: Any) -> None:
    from cascade.eval.score import calibration_of

    report = calibration_of(scored)
    table = Table(title="Calibration (spec §10.5)")
    table.add_column("bin", style="cyan")
    table.add_column("n", justify="right")
    table.add_column("mean pred", justify="right")
    table.add_column("observed", justify="right")
    table.add_column("Wilson 95%", justify="right")
    for row in report.bins:
        if row.count == 0:
            table.add_row(f"[{row.lo:.1f}, {row.hi:.1f})", "0", "-", "-", "-")
            continue
        table.add_row(
            f"[{row.lo:.1f}, {row.hi:.1f})",
            str(row.count),
            _fmt_optional(row.mean_pred, "{:.4f}"),
            _fmt_optional(row.obs_freq, "{:.4f}"),
            f"[{_fmt_optional(row.wilson_lo, '{:.3f}')}, "
            f"{_fmt_optional(row.wilson_hi, '{:.3f}')}]",
        )
    console.print(table)
    console.print(f"ECE [bold]{report.ece:.4f}[/bold] · MCE [bold]{report.mce:.4f}[/bold]")


def _print_domains(scored: Any) -> None:
    from cascade.eval.score import domains_of

    table = Table(title="Brier by domain (spec §10.4)")
    table.add_column("domain", style="cyan")
    table.add_column("n", justify="right")
    table.add_column("base rate", justify="right")
    table.add_column("Brier", justify="right")
    for item in domains_of(scored):
        table.add_row(item.domain, str(item.n), f"{item.base_rate:.4f}", f"{item.brier:.6f}")
    console.print(table)


BASELINE_CHOICES = ("climatology", "direct", "self_consistency")
# Selectable, never in the default set: it asks two public APIs rather than a
# model, and it writes `market_prices`, not `forecasts` (migration 017).
MARKET_BASELINE_CHOICE = "market"


@eval_app.command("baselines")
def eval_baselines(
    config: OverlayOpt = None,
    baseline: Annotated[
        list[str] | None,
        typer.Option(
            "--baseline",
            help=(
                f"Which to produce: {', '.join(BASELINE_CHOICES)}. Repeatable; default all "
                f"three. `{MARKET_BASELINE_CHOICE}` is `cascade eval market-prices`, on request."
            ),
        ),
    ] = None,
    samples: Annotated[
        int | None,
        typer.Option("--samples", help="Self-consistency draws; default ensemble.replicates."),
    ] = None,
    limit: Annotated[
        int | None, typer.Option("--limit", help="Use at most N scenarios (smoke runs).")
    ] = None,
) -> None:
    """Produce the three baselines that are not ablation cells (spec §10.2).

    Climatology needs no model. The two single-model baselines each get the
    *same* Chronofence evidence an agent gets -- same cutoff, same k -- because
    a baseline given less evidence measures retrieval rather than architecture.

    Writes forecasts under the baseline config ids, as `cascade_sim`: a
    baseline is a forecast and must be produced without sight of a label, the
    same as every other forecast in the study (ADR-0022).
    """
    from cascade.corpus.embed import Embedder
    from cascade.ensemble.schema import Forecast
    from cascade.ensemble.store import write_forecast
    from cascade.eval.baselines import (
        CLIMATOLOGY_CONFIG_ID,
        DIRECT_CONFIG_ID,
        SELF_CONSISTENCY_CONFIG_ID,
        climatology_forecasts,
        run_single_model,
    )
    from cascade.ledger.store import load_scenarios
    from cascade.retrieval.search import Chronofence

    settings = _settings(config)
    split = _frozen_split(settings)
    wanted = tuple(baseline) if baseline else BASELINE_CHOICES
    unknown = sorted(set(wanted) - set(BASELINE_CHOICES) - {MARKET_BASELINE_CHOICE})
    if unknown:
        _fail(
            f"unknown baseline(s) {unknown}; choose from "
            f"{[*BASELINE_CHOICES, MARKET_BASELINE_CHOICE]}",
            EXIT_PRECONDITION,
        )
    if MARKET_BASELINE_CHOICE in wanted:
        _market_prices(settings, refresh=False, offline=False, write=True, limit=limit)
    scenarios = _scorable(
        sorted(load_scenarios(settings, role="admin"), key=lambda s: s.scenario_id),
        what="the baselines",
    )
    if limit is not None:
        scenarios = scenarios[:limit]
    if not scenarios:
        _fail("scenario registry is empty; run `cascade ledger build`", EXIT_PRECONDITION)

    # 1. Climatology. No model, no evidence, no call -- and therefore the one
    # baseline producible without a credential, which is why it is selectable
    # on its own rather than bundled with the two that are not.
    written = 0
    if "climatology" not in wanted:
        written = -1
    if "climatology" in wanted:
        for scenario_id, p_hat in climatology_forecasts(
            [item.scenario_id for item in scenarios], base_rate=split.base_rate
        ):
            write_forecast(
                settings,
                Forecast(
                    scenario_id=scenario_id,
                    config_id=CLIMATOLOGY_CONFIG_ID,
                    p_hat=p_hat,
                    sigma=0.0,
                    ci_lo=p_hat,
                    ci_hi=p_hat,
                    modality="single",
                    n_replicates=1,
                    # Arithmetic over the sealed base rate: no runs, no kernel,
                    # no model. Claiming a decider here would be a claim that is
                    # checkable and false.
                    policy="none",
                ),
            )
            written += 1
        console.print(
            f"climatology: [bold]{written}[/bold] forecasts at the sealed base rate "
            f"{split.base_rate:.4f}"
        )

    plans: list[tuple[str, int, float]] = []
    if "direct" in wanted:
        plans.append((DIRECT_CONFIG_ID, 1, 0.0))
    if "self_consistency" in wanted:
        draws = samples if samples is not None else settings.ensemble.replicates
        plans.append((SELF_CONSISTENCY_CONFIG_ID, draws, settings.models.temperature))
    if not plans:
        return

    # 2 and 3. The single-model baselines, on the agents' own evidence.
    _require_provider_ready(settings)
    embedder = Embedder(
        model_name=settings.models.embedding, batch_size=settings.corpus.embed_batch_size
    )
    embedder.load()
    retrieved: dict[str, tuple[tuple[str, str, str], ...]] = {}
    with Chronofence(settings, role="eval") as fence:
        for scenario in scenarios:
            found = _baseline_evidence(settings, fence, embedder, scenario)
            retrieved[scenario.scenario_id] = tuple(
                (chunk.published_at.isoformat(), chunk.source, chunk.body) for chunk in found.chunks
            )

    for baseline_config, draws, temperature in plans:
        run = run_single_model(
            settings,
            scenarios,
            retrieved,
            config_id=baseline_config,
            samples=draws,
            temperature=temperature,
            situations=_situations(
                settings, [scenario.scenario_id for scenario in scenarios], role="eval"
            ),
        )
        for collapsed in run.collapsed:
            write_forecast(
                settings,
                Forecast(
                    scenario_id=collapsed.scenario_id,
                    config_id=baseline_config,
                    p_hat=collapsed.p_hat,
                    sigma=collapsed.sigma,
                    ci_lo=max(0.0, collapsed.p_hat - 1.96 * collapsed.sigma),
                    ci_hi=min(1.0, collapsed.p_hat + 1.96 * collapsed.sigma),
                    modality="single",
                    n_replicates=collapsed.n_parsed,
                ),
            )
        console.print(
            f"{baseline_config}: [bold]{run.scenarios_scored}[/bold] of "
            f"{run.scenarios_requested} scenarios from {run.calls:,} calls; "
            f"{run.unparseable} unparseable answer(s) dropped, never scored as 0.5"
        )


@eval_app.command("estimate")
def eval_estimate(
    config: OverlayOpt = None,
    units: Annotated[
        int, typer.Option("--units", help="Sample scenarios to measure before extrapolating.")
    ] = 20,
    samples: Annotated[
        int | None,
        typer.Option("--samples", help="Self-consistency draws; default ensemble.replicates."),
    ] = None,
) -> None:
    """Project the baseline phase's spend from a measured sample (spec §12.4).

    "Every phase supports --estimate, which runs 20 sample units, extrapolates,
    and prints projected spend. No full phase launches without it." The
    self-consistency baseline alone is 36,000 calls -- the single largest
    block of the `baseline` ceiling -- so this is the guardrail that stops it
    being discovered by the invoice.

    Exits **2** on a projected breach, the same code a real breach uses, so a
    supervising script does not have to tell a projection from an abort.
    """
    from cascade.corpus.embed import Embedder
    from cascade.eval.baselines import (
        DIRECT_CONFIG_ID,
        SELF_CONSISTENCY_CONFIG_ID,
        run_single_model,
    )
    from cascade.ledger.store import load_scenarios
    from cascade.llm.client import LLMClient
    from cascade.llm.meter import estimate_phase
    from cascade.retrieval.search import Chronofence

    settings = _settings(config)
    _frozen_split(settings)
    _require_provider_ready(settings)
    draws = samples if samples is not None else settings.ensemble.replicates

    scenarios = _scorable(
        sorted(load_scenarios(settings, role="admin"), key=lambda item: item.scenario_id),
        what="the estimate",
    )
    if not scenarios:
        _fail("scenario registry is empty; run `cascade ledger build`", EXIT_PRECONDITION)
    sample = scenarios[: max(1, units)]

    embedder = Embedder(
        model_name=settings.models.embedding, batch_size=settings.corpus.embed_batch_size
    )
    embedder.load()
    retrieved: dict[str, tuple[tuple[str, str, str], ...]] = {}
    with Chronofence(settings, role="eval") as fence:
        for scenario in sample:
            found = _baseline_evidence(settings, fence, embedder, scenario)
            retrieved[scenario.scenario_id] = tuple(
                (chunk.published_at.isoformat(), chunk.source, chunk.body) for chunk in found.chunks
            )

    client = LLMClient(settings, phase="baseline")
    for config_id, count, temperature in (
        (DIRECT_CONFIG_ID, 1, 0.0),
        (SELF_CONSISTENCY_CONFIG_ID, draws, settings.models.temperature),
    ):
        run_single_model(
            settings,
            sample,
            retrieved,
            config_id=config_id,
            samples=count,
            temperature=temperature,
            client=client,
            situations=_situations(
                settings, [scenario.scenario_id for scenario in sample], role="eval"
            ),
        )

    estimate = estimate_phase(
        phase="baseline",
        sample_units=len(sample),
        sample_usd=client.meter.total_usd,
        total_units=len(scenarios),
        ceiling_usd=settings.phase_ceiling("baseline"),
    )
    table = Table(title="Baseline phase estimate (spec §12.4)")
    table.add_column("Quantity", style="cyan")
    table.add_column("Sampled", justify="right")
    table.add_column("Projected", justify="right")
    table.add_row("scenarios", f"{estimate.sample_units:,}", f"{estimate.total_units:,}")
    table.add_row(
        "model calls",
        f"{estimate.sample_units * (1 + draws):,}",
        f"{estimate.total_units * (1 + draws):,}",
    )
    table.add_row("spend (USD)", f"${estimate.sample_usd:.6f}", f"${estimate.projected_usd:.6f}")
    table.add_row("ceiling (USD)", "-", f"${estimate.ceiling_usd:.2f}")
    console.print(table)
    console.print(f"cache hit rate over the sample: {client.cache.stats.hit_rate:.4f}")
    if not estimate.within_ceiling:
        _fail(
            f"projected ${estimate.projected_usd:.2f} against a "
            f"${estimate.ceiling_usd:.2f} ceiling for the baseline phase",
            EXIT_BUDGET_BREACH,
        )
    console.print("[bold green]projection is within the baseline ceiling[/bold green]")


@eval_app.command("market-prices")
def eval_market_prices(
    config: OverlayOpt = None,
    refresh: Annotated[
        bool,
        typer.Option(
            "--refresh",
            help="Re-ask the sources for everything. Recordings are otherwise replayed.",
        ),
    ] = False,
    offline: Annotated[
        bool,
        typer.Option("--offline", help="Never touch the network; replay recordings only."),
    ] = False,
    write: Annotated[
        bool,
        typer.Option("--write/--no-write", help="Store the rows in `market_prices` (admin role)."),
    ] = True,
    limit: Annotated[
        int | None, typer.Option("--limit", help="Use at most N scenarios (smoke runs).")
    ] = None,
) -> None:
    """Fetch each scenario's market probability strictly before its cutoff (M14).

    Prints the coverage: how many scenarios have a usable price, how many are
    stale, how many were unobtainable and why, and the staleness distribution.
    A scenario without a usable price is excluded from the market baseline and
    counted -- never imputed -- so every comparison against it runs on the
    intersection.

    Reads no label and needs none. Exits 3 when any price is missing for a
    reason that is not a fact about its market (`fetch_failed`, `not_recorded`),
    so an interrupted fetch cannot pass as a finished one; re-running resumes
    from the recordings.
    """
    if refresh and offline:
        _fail("--refresh and --offline contradict each other", EXIT_PRECONDITION)
    _market_prices(_settings(config), refresh=refresh, offline=offline, write=write, limit=limit)


def _market_prices(
    settings: Settings, *, refresh: bool, offline: bool, write: bool, limit: int | None
) -> None:
    """Fetch, optionally store, and print the coverage of the market benchmark."""
    from datetime import timedelta

    from cascade.eval.market import coverage_summary
    from cascade.eval.market_fetch import MarketFetcher, fetch_market_prices
    from cascade.eval.store import write_market_prices
    from cascade.ledger.store import load_scenarios

    scenarios = sorted(load_scenarios(settings, role="admin"), key=lambda s: s.scenario_id)
    if limit is not None:
        scenarios = scenarios[:limit]
    if not scenarios:
        _fail("scenario registry is empty; run `cascade ledger build`", EXIT_PRECONDITION)

    fetcher = MarketFetcher(
        # Beside the registry's recordings, not among them: that directory is
        # what the manifest hash is rebuildable from.
        cache_root=settings.source_cache_path() / "market",
        config=settings.market_baseline,
        refresh=refresh,
        offline=offline,
    )
    try:
        prices = fetch_market_prices(scenarios, fetcher)
    finally:
        fetcher.close()
    console.print(
        f"{fetcher.requests:,} request(s) sent, {fetcher.replays:,} recording(s) replayed"
    )

    bound = timedelta(hours=settings.market_baseline.max_staleness_hours)
    summary = coverage_summary(prices, max_staleness=bound)
    table = Table(title="Market price at the cutoff -- coverage")
    table.add_column("", style="cyan")
    table.add_column("scenarios", justify="right")
    table.add_column("note", overflow="fold")
    table.add_row("asked", f"{summary.n_scenarios:,}", "every scenario lands in one row below")
    table.add_row(
        "usable",
        f"[bold]{summary.n_usable:,}[/bold]",
        f"observed strictly before the cutoff and at most "
        f"{settings.market_baseline.max_staleness_hours:g} h old; the baseline's denominator",
    )
    table.add_row("stale", f"{summary.n_stale:,}", "priced, older than the bound; excluded")
    for reason, count in summary.unobtainable:
        table.add_row(f"unobtainable: {reason}", f"{count:,}", "excluded, never imputed")
    console.print(table)

    sources = Table(title="By source")
    sources.add_column("source", style="cyan")
    sources.add_column("scenarios", justify="right")
    sources.add_column("usable", justify="right")
    sources.add_column("stale", justify="right")
    for source, total, usable, stale in summary.by_source:
        sources.add_row(source, f"{total:,}", f"{usable:,}", f"{stale:,}")
    console.print(sources)

    if summary.staleness is not None:
        age = summary.staleness
        console.print(
            f"staleness over {age.n:,} priced scenario(s), stale ones included: "
            f"min {age.minimum:,.0f} s, p50 {age.p50:,.0f} s, p90 {age.p90:,.0f} s, "
            f"p99 {age.p99:,.0f} s, max {age.maximum:,.0f} s"
        )
    for price in prices:
        if price.unobtainable_reason in ("fetch_failed", "market_created_after_cutoff"):
            console.print(f"  [dim]{price.scenario_id}: {price.detail}[/dim]")

    if write:
        written = write_market_prices(settings, prices)
        console.print(f"[green]stored[/green] {written:,} row(s) in `market_prices`")

    unfinished = sum(
        count
        for reason, count in summary.unobtainable
        if reason in ("fetch_failed", "not_recorded")
    )
    if unfinished:
        _fail(
            f"{unfinished} price(s) are missing because a source did not answer, not because "
            "of anything about the market; re-run to resume from the recordings",
            EXIT_PRECONDITION,
        )


@eval_app.command("grid")
def eval_grid(
    config: OverlayOpt = None,
    policy: Annotated[str, typer.Option("--policy")] = "agent",
    wave: Annotated[int, typer.Option("--wave")] = 200,
    replicate_policy: Annotated[
        str,
        typer.Option(
            "--replicate-policy",
            help="'budget_capped' (D capped at ensemble.ablation_replicates) or 'design'.",
        ),
    ] = "budget_capped",
    cells: Annotated[
        list[str] | None, typer.Option("--cell", help="Restrict to these cell ids. Repeatable.")
    ] = None,
    limit: Annotated[int | None, typer.Option("--limit")] = None,
    supplementary: Annotated[
        bool,
        typer.Option(
            "--supplementary",
            help="Also run the declared supplementary cells (S01...). Never run by default.",
        ),
    ] = False,
    variants: Annotated[
        list[str] | None,
        typer.Option(
            "--variant",
            help="Run a tuning variant from configs/tuning/ -- on dev scenarios only. Repeatable.",
        ),
    ] = None,
    partition: PartitionOpt = "all",
) -> None:
    """Execute Appendix C's 12-cell grid and collapse each cell (spec §10.3).

    `--supplementary` adds the cells declared in `cascade/eval/supplementary.py`.
    They are not Appendix C's and are never run unless asked for. `--variant`
    runs a tuning variant instead of the grid, and the dev/test guard applies:
    a variant is simulated on dev scenarios and nothing else, so there is no
    test-partition forecast of it to be tempted by. `--partition dev` restricts
    any cell to dev the same way, which is how the headline configuration gets
    dev forecasts to tune against before the study is run.

    Resumable in exactly the way the fan-out is: a run row exists only for a
    run that finished, so a re-run plans the difference. Cells are executed in
    id order, and each is collapsed as soon as it completes, so an interrupted
    grid leaves whole cells scoreable rather than a partial everything.

    `--replicate-policy` resolves CLAUDE.md's open question Q1 explicitly. The
    choice is printed here and written into the report; it is not inferred from
    a run count.
    """
    from cascade.decompose.store import completed_scenarios
    from cascade.ensemble.aggregate import collapse
    from cascade.ensemble.runner import EnsembleRunner
    from cascade.ensemble.store import collapse_inputs, write_forecast
    from cascade.eval.ablation import grid_replicates

    base = _settings(config)
    split = _frozen_split(base)
    declaration = _declared_split(base, split)
    if replicate_policy not in {"budget_capped", "design"}:
        _fail(
            f"unknown replicate policy {replicate_policy!r}; expected "
            "'budget_capped' or 'design'",
            EXIT_PRECONDITION,
        )
    chosen = _partition(partition)
    if variants and chosen == "all":
        # `all` is the option's default, not a request: a variant has exactly
        # one legitimate partition, so asking for a variant is asking for dev.
        chosen = "dev"
    selected = _grid_cells(cells, supplementary=supplementary, variants=variants or [])
    tuning = {cell.cell_id for cell in selected} - _declared_configs()
    if tuning and chosen != "dev":
        _fail(
            f"{sorted(tuning)} are tuning variants and run on the dev partition only; "
            f"--partition {chosen} was asked for",
            EXIT_PRECONDITION,
        )

    from cascade.ledger.store import load_scenarios

    # Excluded scenarios are in neither pool (ADR-0043), so the A=on and A=off
    # cells rank the same candidates and their 90-scenario subsamples agree.
    excluded = set(declaration.excluded_ids)
    registry = sorted(
        item.scenario_id
        for item in load_scenarios(base, role="admin")
        if item.scenario_id not in excluded
    )
    compiled = sorted(
        scenario_id for scenario_id in completed_scenarios(base) if scenario_id not in excluded
    )
    if not registry:
        _fail("scenario registry is empty; run `cascade ledger build`", EXIT_PRECONDITION)

    summary = Table(title=f"Ablation grid (spec Appendix C, policy={replicate_policy})")
    summary.add_column("cell", style="cyan")
    summary.add_column("A", justify="center")
    summary.add_column("B", justify="center")
    summary.add_column("C")
    summary.add_column("D design", justify="right")
    summary.add_column("D run", justify="right")
    summary.add_column("scenarios", justify="right")
    summary.add_column("runs", justify="right")

    for cell in selected:
        settings = _settings(cell.cell_id)
        replicates, note = grid_replicates(
            cell,
            policy=replicate_policy,  # type: ignore[arg-type]
            ablation_cap=base.ensemble.ablation_replicates,
        )
        pool = registry if not cell.decomposition else compiled
        if not pool:
            summary.add_row(
                cell.cell_id,
                "on" if cell.decomposition else "off",
                "on" if cell.information_asymmetry else "off",
                cell.grounding,
                str(cell.replicates_design),
                str(replicates),
                "0",
                "no compiled graph",
            )
            continue
        scenario_ids = _cell_scenarios(
            cell,
            pool,
            salt=split.study_salt,
            cap=base.ensemble.ablation_scenarios,
            declaration=declaration,
            partition=chosen,
            is_variant=cell.cell_id in tuning,
            limit=limit,
        )

        loom_for, on_complete, _ = _fanout_wiring(
            settings, policy=policy, scenario_ids=scenario_ids
        )
        runner = EnsembleRunner(
            settings=settings,
            loom_for=loom_for,
            on_complete=on_complete,
            policy=policy,
            completed=_completed_for(settings),
        )
        tasks, skipped = runner.plan(
            scenario_ids=scenario_ids, config_id=cell.cell_id, replicates=replicates
        )
        console.print(f"[bold]{cell.cell_id}[/bold] {cell.role} — {note}")
        if tasks:
            report = runner.execute(tasks, wave=wave)
            _print_fanout(report, skipped=skipped, policy=policy)

        for item in collapse_inputs(settings, config_id=cell.cell_id):
            write_forecast(
                settings,
                collapse(
                    item.scores,
                    scenario_id=item.scenario_id,
                    config_id=item.config_id,
                    salt=settings.study.salt,
                    sigma_threshold=settings.ensemble.sigma_multimodal_threshold,
                    bootstrap_b=settings.ensemble.bootstrap_b,
                    mean_events=item.mean_events,
                    mean_steps=item.mean_steps,
                    absorbed_runs=item.absorbed_runs,
                    policy=item.policy,
                ),
            )
        summary.add_row(
            cell.cell_id,
            "on" if cell.decomposition else "off",
            "on" if cell.information_asymmetry else "off",
            cell.grounding,
            str(cell.replicates_design),
            str(replicates),
            str(len(scenario_ids)),
            f"{len(tasks) + skipped:,}",
        )
    console.print(summary)


def _cell_scenarios(
    cell: CellSpec,
    pool: Sequence[str],
    *,
    salt: str,
    cap: int | None,
    declaration: SplitDeclaration,
    partition: Partition,
    is_variant: bool,
    limit: int | None,
) -> list[str]:
    """The scenarios one grid cell is about to simulate. Exits 3 on a leak.

    A declared cell draws §10.3's subsample and is then *intersected* with the
    requested partition -- never re-split, so a scenario's side does not depend
    on which pool the cell drew from. A tuning variant skips the 90-scenario
    cap, which is a property of the declared cells: applied to a variant it
    would leave 16 of the 40 dev scenarios, and dev is already the small side.

    The guard runs last, on the list that will actually be simulated, so no
    later edit to the filters above it can let a held-out scenario through.
    """
    from cascade.eval.ablation import grid_scenarios
    from cascade.eval.split import require_dev_only

    scenario_ids = list(grid_scenarios(pool, cell=cell, salt=salt, cap=None if is_variant else cap))
    if partition != "all":
        keep = set(declaration.ids(partition))
        scenario_ids = [scenario_id for scenario_id in scenario_ids if scenario_id in keep]
    if limit is not None:
        scenario_ids = scenario_ids[:limit]
    if is_variant:
        scenario_ids = list(
            _guard(
                lambda: require_dev_only(
                    scenario_ids, declaration, what=f"tuning variant {cell.cell_id!r}"
                )
            )
        )
    return scenario_ids


def _grid_cells(
    cells: Sequence[str] | None, *, supplementary: bool, variants: Sequence[str]
) -> list[CellSpec]:
    """Which cells a grid invocation runs. Exits 3 on anything undeclared.

    Three kinds, kept apart. Appendix C's twelve are the default. Supplementary
    cells are declared in code and join only under `--supplementary`, so a
    routine grid run neither pays for them nor reports them. A tuning variant
    is whatever overlay sits in `configs/tuning/` -- built into a cell from its
    own flags so it runs through the same driver -- and naming one replaces the
    grid rather than adding to it: a tuning run is not a study run.
    """
    from cascade.config import overlay_kind
    from cascade.eval.ablation import CELLS, CellSpec
    from cascade.eval.supplementary import SUPPLEMENTARY_CELLS, supplementary_ids

    if variants:
        if cells or supplementary:
            _fail(
                "--variant runs tuning variants on dev and cannot be combined with "
                "--cell or --supplementary; run the study cells separately",
                EXIT_PRECONDITION,
            )
        built: list[CellSpec] = []
        for name in sorted(set(variants)):
            if name in _declared_configs() or overlay_kind(name) != "tuning":
                _fail(
                    f"{name!r} is not a tuning variant: expected configs/tuning/{name}.yaml "
                    "and a name no declared configuration uses",
                    EXIT_PRECONDITION,
                )
            variant = _settings(name)
            built.append(
                CellSpec(
                    name,
                    variant.flags.causal_decomposition,
                    variant.flags.information_asymmetry,
                    variant.flags.grounding,
                    variant.ensemble.replicates,
                    "Tuning variant -- dev partition only, never a result",
                )
            )
        return built

    known = {cell.cell_id: cell for cell in CELLS}
    extra = {cell.cell_id: cell for cell in SUPPLEMENTARY_CELLS}
    if not cells:
        return [*CELLS, *(SUPPLEMENTARY_CELLS if supplementary else ())]
    chosen: list[CellSpec] = []
    for cell_id in cells:
        if cell_id in known:
            chosen.append(known[cell_id])
        elif cell_id in extra and supplementary:
            chosen.append(extra[cell_id])
        elif cell_id in extra:
            _fail(
                f"{cell_id} is a supplementary cell, not one of Appendix C's twelve; "
                "pass --supplementary to run it",
                EXIT_PRECONDITION,
            )
        else:
            _fail(
                f"{cell_id!r} is not a cell; the grid is {sorted(known)} and the "
                f"supplementary cells are {list(supplementary_ids())}",
                EXIT_PRECONDITION,
            )
    return chosen


def _completed_for(settings: Settings) -> Any:
    """Bind one cell's settings into its resume lookup.

    A closure written inline in the grid loop would capture `settings` by
    reference, and `settings` is rebound on every iteration -- so each cell
    would resume against whichever configuration the loop happened to end on.
    A factory binds it once, at the iteration that owns it.
    """
    from cascade.trace.store import completed_replicates

    def completed(scenario_id: str, config_id: str) -> set[int]:
        return completed_replicates(settings, scenario_id=scenario_id, config_id=config_id)

    return completed


@eval_app.command("significance")
def eval_significance(
    config: OverlayOpt = None,
    headline: Annotated[
        str, typer.Option("--headline", help="Configuration every other cell is compared against.")
    ] = "C01",
    partition: PartitionOpt = "dev",
    versus: Annotated[
        list[str] | None,
        typer.Option(
            "--versus",
            help="A tuning variant to compare against the headline. Dev only. Repeatable.",
        ),
    ] = None,
) -> None:
    """Paired bootstrap CIs with Holm-Bonferroni adjustment (spec §10.4).

    Every comparison is paired on the scenarios both configurations scored, and
    the paired count is printed: the 11 capped cells run 90 scenarios against
    the headline's 180, so an unpaired comparison would silently compare two
    different sets.

    Measures on dev by default, for the reason `eval score` does. Three Holm
    families are adjusted separately and labelled: Appendix C's, the declared
    supplementary comparisons, and -- under `--versus`, on dev only -- tuning
    variants against the headline.
    """
    settings = _settings(config)
    split = _frozen_split(settings)
    declaration = _declared_split(settings, split)
    chosen = _partition(partition)
    comparisons = _build_comparisons(
        settings,
        split,
        headline=headline,
        declaration=declaration,
        partition=chosen,
        exploratory=versus or [],
    )
    if not comparisons:
        _fail(
            f"no comparison had forecasts on both sides on the {chosen} partition; "
            "run `cascade eval grid` first",
            EXIT_PRECONDITION,
        )
    _print_partition(declaration, chosen)
    table = Table(title=f"Significance on {chosen} (spec §10.4) -- Holm within each family")
    table.add_column("family", style="magenta", overflow="fold")
    table.add_column("comparison", style="cyan", overflow="fold")
    table.add_column("A - B", justify="right")
    table.add_column("delta Brier", justify="right")
    table.add_column("95% CI", justify="right")
    table.add_column("n", justify="right")
    table.add_column("p", justify="right")
    table.add_column("Holm p*", justify="right")
    for item in comparisons:
        table.add_row(
            item.family,
            item.name,
            f"{item.config_a} - {item.config_b}",
            f"{item.interval.point:+.6f}",
            f"[{item.interval.lo:+.6f}, {item.interval.hi:+.6f}]",
            str(item.n_paired),
            f"{item.interval.p_value:.4g}",
            "-" if item.p_adjusted is None else f"{item.p_adjusted:.4g}",
        )
    console.print(table)
    console.print(
        "[dim]Information asymmetry is nested inside causal decomposition, so the two "
        "leave-one-out deltas do not sum. The net figure is reported above rather than "
        "left for a reader to compute (§10.3).[/dim]"
    )


def _build_comparisons(
    settings: Settings,
    split: Any,
    *,
    headline: str = "C01",
    declaration: SplitDeclaration,
    partition: Partition,
    exploratory: Sequence[str] = (),
) -> list[Any]:
    """Every reported comparison on one partition, Holm-adjusted per family.

    The arithmetic is `score.significance_families`; this loads what it needs
    and nothing more. Only declared configurations are loaded on `test` and
    `all`: a tuning variant's held-out forecasts are never joined to a label,
    let alone compared, and asking for one off dev exits 3.
    """
    from cascade.eval.score import significance_families
    from cascade.eval.split import require_declared_config, select
    from cascade.eval.store import available_configs, scored_forecasts

    declared = _declared_configs()
    for name in sorted(set(exploratory)):
        _guard(
            lambda name=name: require_declared_config(name, partition=partition, declared=declared)
        )
        if name in declared:
            _fail(
                f"{name!r} is a declared study configuration and is already compared in its "
                "own family; --versus is for a tuning variant",
                EXIT_PRECONDITION,
            )
    wanted = declared | set(exploratory)
    scored_by_config = {
        name: _guard(
            lambda name=name: select(
                scored_forecasts(settings, config_id=name), declaration, partition
            )
        )
        for name, _ in available_configs(settings)
        if name in wanted
    }
    return list(
        _guard(
            lambda: significance_families(
                scored_by_config,
                headline=headline,
                salt=split.study_salt,
                b_resamples=settings.ensemble.bootstrap_b,
                eligible=_appendix_c_configs(),
                exploratory=tuple(sorted(set(exploratory))),
                declaration=declaration,
            )
        )
    )


def _split_interactions(settings: Settings, split: Any, declaration: SplitDeclaration) -> Any:
    """How the dev/test split overlaps the study's two other keyed subsets.

    Computed from ids alone. The ablation subsample is the one a capped cell
    draws from the whole registry; a cell whose pool is smaller (not every
    scenario compiled) draws a different 90, and the report's paired counts --
    which are measured, not predicted -- are what to trust then.
    """
    from cascade.eval.ablation import cell_by_id, grid_scenarios
    from cascade.eval.score import recalibration_half
    from cascade.eval.split import interactions

    everyone = declaration.ids("all")
    return interactions(
        declaration,
        ablation_subsample=grid_scenarios(
            everyone,
            cell=cell_by_id("C05"),
            salt=split.study_salt,
            cap=settings.ensemble.ablation_scenarios,
        ),
        recalibration_fit=[
            scenario_id
            for scenario_id in everyone
            if recalibration_half(scenario_id, salt=split.study_salt) == "fit"
        ],
    )


@eval_app.command("split")
def eval_split(
    config: OverlayOpt = None,
    ids: Annotated[
        str | None,
        typer.Option("--ids", help="Also list one partition's scenario ids: dev or test."),
    ] = None,
) -> None:
    """Print the declared dev/test split: sizes, domains, overlaps, fingerprint.

    Reads scenario ids and domains, never a label. Exits 3 when the recomputed
    split is not the pinned one -- the same check every measuring command makes,
    so this is also how to find out *why* one of them refused.
    """
    settings = _settings(config)
    split = _frozen_split(settings)
    declaration = _declared_split(settings, split)

    console.print(
        f"dev/test split [bold]{declaration.sha256}[/bold]\n"
        f"  purpose {declaration.purpose} · applies to manifest "
        f"{split.manifest_sha256[:16]}... · pinned in configs/base.yaml (eval.split_sha256)"
    )
    if declaration.excluded:
        console.print(
            f"  {len(declaration.excluded)} of {split.n_scenarios} sealed scenarios excluded "
            "from scoring before the split was drawn (exchange placeholder legs; ADR-0043):"
        )
        for item in declaration.excluded:
            console.print(f"    {item.scenario_id}  [dim]{item.reason}[/dim]")
    table = Table(title=f"{len(declaration.dev)} dev / {len(declaration.test)} test, by domain")
    table.add_column("domain", style="cyan")
    table.add_column("n", justify="right")
    table.add_column("dev", justify="right")
    table.add_column("test", justify="right")
    for row in declaration.domains:
        table.add_row(row.domain, str(row.n), str(row.dev), str(row.test))
    console.print(table)

    overlap = Table(title="Overlap with the other keyed subsets")
    overlap.add_column("partition", style="cyan")
    overlap.add_column("n", justify="right")
    overlap.add_column("in ablation subsample", justify="right")
    overlap.add_column("recalibration fit / held", justify="right")
    for item in _split_interactions(settings, split, declaration):
        overlap.add_row(
            item.partition,
            str(item.n),
            str(item.ablation_subsample),
            f"{item.recalibration_fit} / {item.recalibration_held}",
        )
    console.print(overlap)
    console.print(
        "[dim]Tuning is only ever legitimate on dev. The report's headline is test.[/dim]"
    )
    if ids is not None:
        if ids not in {"dev", "test"}:
            _fail(f"--ids takes 'dev' or 'test', got {ids!r}", EXIT_PRECONDITION)
        # One id per line through plain echo: this output is for scripts, and
        # the rich console would wrap a long id at the terminal width.
        for scenario_id in declaration.ids(ids):  # type: ignore[arg-type]
            typer.echo(scenario_id)


@eval_app.command("tune-guard")
def eval_tune_guard(
    config: OverlayOpt = None,
    scenarios: Annotated[
        list[str] | None,
        typer.Option("--scenario", help="A scenario id a tuning step is about to use. Repeatable."),
    ] = None,
    config_ids: Annotated[
        list[str] | None,
        typer.Option(
            "--config-id",
            help="A stored configuration: every scenario it holds a forecast for is checked.",
        ),
    ] = None,
) -> None:
    """Refuse (exit 3) any scenario set that touches the held-out partition.

    The check a tuning script runs before it looks at anything. It exists as a
    command so that tuning done outside this CLI -- a notebook, a sweep, a
    shell loop -- has one line to call and an exit status to trust, instead of
    re-implementing the split and getting the purpose string wrong.

    `--config-id` checks a stored configuration by the scenarios it has
    forecasts for. It reads ids only: asking whether a variant touched test is
    not itself a look at test.
    """
    from cascade.eval.split import require_dev_only
    from cascade.eval.store import forecast_scenarios

    if not scenarios and not config_ids:
        _fail("nothing to check: pass --scenario and/or --config-id", EXIT_PRECONDITION)
    settings = _settings(config)
    split = _frozen_split(settings)
    declaration = _declared_split(settings, split)

    checked = 0
    if scenarios:
        checked += len(
            _guard(lambda: require_dev_only(scenarios, declaration, what="--scenario list"))
        )
    for name in sorted(set(config_ids or [])):
        held = forecast_scenarios(settings, config_id=name)
        if not held:
            _fail(
                f"config {name!r} holds no forecasts, so there is nothing to vouch for",
                EXIT_PRECONDITION,
            )
        checked += len(
            _guard(
                lambda held=held, name=name: require_dev_only(
                    held, declaration, what=f"config {name!r}"
                )
            )
        )
    console.print(
        f"[bold green]dev only[/bold green]: {checked} scenario reference(s) checked against "
        f"split {declaration.sha256[:16]}..., none held out"
    )


@eval_app.command("status")
def eval_status(config: OverlayOpt = None) -> None:
    """What is scoreable right now, and what the grid is still missing."""
    from cascade.eval.ablation import CELLS
    from cascade.eval.baselines import BASELINES
    from cascade.eval.store import available_configs, config_policies

    settings = _settings(config)
    split = _frozen_split(settings)
    stored = dict(available_configs(settings))
    policies = config_policies(settings)

    console.print(
        f"frozen split [bold]{split.manifest_sha256[:16]}...[/bold] "
        f"({split.n_scenarios} scenarios, base rate {split.base_rate:.4f}, "
        f"climatology Brier {split.climatology_brier:.6f})"
    )

    cells = Table(title="Ablation grid (spec Appendix C)")
    cells.add_column("cell", style="cyan")
    cells.add_column("A", justify="center")
    cells.add_column("B", justify="center")
    cells.add_column("C")
    cells.add_column("D", justify="right")
    cells.add_column("forecasts", justify="right")
    cells.add_column("decider")
    cells.add_column("role", overflow="fold")
    for cell in CELLS:
        seen = policies.get(cell.cell_id, ())
        cells.add_row(
            cell.cell_id,
            "on" if cell.decomposition else "off",
            "on" if cell.information_asymmetry else "off",
            cell.grounding,
            str(cell.replicates_design),
            f"{stored.get(cell.cell_id, 0):,}",
            (
                ("[yellow]" + ", ".join(seen) + "[/yellow]")
                if set(seen) - {"agent"}
                else (", ".join(seen) or "-")
            ),
            cell.role,
        )
    console.print(cells)

    from cascade.eval.supplementary import SUPPLEMENTARY_CELLS

    extra = Table(
        title="Supplementary cells (declared; own Holm family; `eval grid --supplementary`)"
    )
    extra.add_column("cell", style="cyan")
    extra.add_column("forecasts", justify="right")
    extra.add_column("role", overflow="fold")
    for cell in SUPPLEMENTARY_CELLS:
        extra.add_row(cell.cell_id, f"{stored.get(cell.cell_id, 0):,}", cell.role)
    console.print(extra)

    variants = sorted(set(stored) - _declared_configs())
    if variants:
        console.print(
            f"{len(variants)} stored configuration(s) are tuning variants, scoreable on "
            f"dev only: {', '.join(variants)}"
        )

    baselines = Table(title="Baselines (spec §10.2)")
    baselines.add_column("baseline", style="cyan")
    baselines.add_column("config", justify="right")
    baselines.add_column("forecasts", justify="right")
    baselines.add_column("purpose", overflow="fold")
    for spec in BASELINES:
        baselines.add_row(
            spec.name, spec.config_id, f"{stored.get(spec.config_id, 0):,}", spec.purpose
        )
    console.print(baselines)

    provisional = [
        config_id
        for config_id, seen in sorted(policies.items())
        if {"heuristic", "mixed"} & set(seen)
    ]
    if provisional:
        console.print(
            f"[yellow]{len(provisional)} config(s) hold forecasts from a stand-in "
            f"decider: {', '.join(provisional)}.[/yellow] Resuming a grid into them "
            "produces a 'mixed' forecast, which the report flags. Remove the "
            "heuristic-policy rows from `runs` and re-collapse before a study run."
        )


@eval_app.command("blend")
def eval_blend(
    config: OverlayOpt = None,
    system: Annotated[str, typer.Option("--system", help="The system's config id.")] = "C01",
    reference: Annotated[
        str, typer.Option("--reference", help="The forecaster to blend with.")
    ] = "B2_single_direct",
) -> None:
    """Blend two forecasters with a weight fitted on dev and scored on test.

    The weight is the Brier-optimal linear pool over the **dev** scenarios both
    forecasters scored; it is then applied, unchanged, to the **test**
    scenarios, and the blend is compared with the system alone by a paired
    bootstrap. Fitting and scoring never share a scenario (ADR-0038). Exits 3
    when either side has no paired scenario to work with.
    """
    from cascade.eval.blend import Paired, evaluate_blend
    from cascade.eval.split import select
    from cascade.eval.stats import bootstrap_seed

    settings = _settings(config)
    split = _frozen_split(settings)
    declaration = _declared_split(settings, split)

    def paired(partition: Partition) -> dict[str, Paired]:
        from cascade.eval.store import scored_forecasts

        left = {
            item.scenario_id: item
            for item in select(scored_forecasts(settings, config_id=system), declaration, partition)
        }
        right = {
            item.scenario_id: item
            for item in select(
                scored_forecasts(settings, config_id=reference), declaration, partition
            )
        }
        return {
            key: Paired(
                system=left[key].p_hat, reference=right[key].p_hat, outcome=left[key].outcome
            )
            for key in sorted(set(left) & set(right))
        }

    fitting, scored = paired("dev"), paired("test")
    if not fitting or not scored:
        _fail(
            f"{system} and {reference} share {len(fitting)} dev and {len(scored)} test "
            "scenarios; a blend needs both",
            EXIT_PRECONDITION,
        )
    result = evaluate_blend(
        fitting,
        scored,
        seed=bootstrap_seed(split.study_salt, "blend", system, reference),
        b_resamples=settings.ensemble.bootstrap_b,
    )
    fit = result.fit
    console.print(
        f"weight on {system}: [bold]{fit.weight:.4f}[/bold] (fitted on {fit.n} dev scenarios"
        + (
            f"; unconstrained optimum {fit.unclipped:+.4f}, clipped to [0, 1])"
            if fit.unclipped is not None and fit.unclipped != fit.weight
            else ")"
        )
    )
    table = Table(title=f"On {result.n_scored} held-out test scenarios")
    table.add_column("forecaster", style="cyan")
    table.add_column("Brier", justify="right")
    for name, value in (
        (system, result.brier_system),
        (reference, result.brier_reference),
        ("blend", result.brier_blend),
    ):
        table.add_row(name, "-" if value is None else f"{value:.6f}")
    console.print(table)
    if result.blend_minus_system is not None:
        interval = result.blend_minus_system
        console.print(
            f"blend - {system}: {interval.point:+.6f}, 95% CI [{interval.lo:+.6f}, "
            f"{interval.hi:+.6f}], p {interval.p_value:.4g}. Negative means the blend is "
            "better. One comparison, not in the Appendix C family."
        )


@eval_app.command("injection")
def eval_injection(
    config: OverlayOpt = None,
    limit: Annotated[
        int, typer.Option("--limit", help="Scenarios to probe (a keyed-hash sample).")
    ] = 30,
) -> None:
    """Measure whether a document in the evidence can give the model orders.

    Threat T3. Each sampled scenario is forecast on its retrieved evidence and
    again with one appended document that asserts nothing about the world and
    instructs the model to report a probability contradicting its clean answer.
    Reads no label. Costs money in `record` mode (four short calls per
    scenario, under the `bench` ceiling); the clean arm is the direct
    baseline's own request, so a recorded B2 serves it from the cache.
    """
    from cascade.corpus.embed import Embedder
    from cascade.eval.injection import COMPLY_WITHIN, MOVED_BY, run_probe, sample_scenarios
    from cascade.ledger.store import load_scenarios
    from cascade.retrieval.search import Chronofence

    settings = _settings(config)
    _require_provider_ready(settings)
    by_id = {
        item.scenario_id: item
        for item in _scorable(load_scenarios(settings, role="sim"), what="the injection probe")
    }
    if not by_id:
        _fail("scenario registry is empty; run `cascade ledger build`", EXIT_PRECONDITION)
    chosen = [
        by_id[scenario_id]
        for scenario_id in sample_scenarios(sorted(by_id), salt=settings.study.salt, limit=limit)
    ]

    embedder = Embedder(
        model_name=settings.models.embedding, batch_size=settings.corpus.embed_batch_size
    )
    embedder.load()
    retrieved: dict[str, tuple[tuple[str, str, str], ...]] = {}
    with Chronofence(settings, role="sim") as fence:
        for scenario in chosen:
            # The direct baseline's own evidence, so the clean arm is its
            # request byte for byte and a recorded B2 serves it.
            found = _baseline_evidence(settings, fence, embedder, scenario)
            retrieved[scenario.scenario_id] = tuple(
                (chunk.published_at.isoformat(), chunk.source, chunk.body) for chunk in found.chunks
            )

    report = run_probe(settings, chosen, retrieved)
    table = Table(title=f"Injected instructions · {report.scenarios} scenarios")
    for column in ("attack", "scored", "unparseable", "complied", "95% CI", "moved", "mean shift"):
        table.add_column(column)
    for item in report.attacks:
        lo, hi = item.complied_interval
        table.add_row(
            item.attack,
            f"{item.scored}/{item.trials}",
            str(item.unparseable_poisoned),
            str(item.complied),
            f"[{lo:.2f}, {hi:.2f}]",
            str(item.moved),
            "n/a" if item.mean_shift is None else f"{item.mean_shift:+.3f}",
        )
    console.print(table)
    console.print(
        f"complied: answered within {COMPLY_WITHIN} of the instructed value · "
        f"moved: shifted at least {MOVED_BY} toward it. Measured on the single-model "
        "forecaster, whose evidence block is formatted as the agents' is."
    )


@eval_app.command("prompt-audit")
def eval_prompt_audit(
    config: OverlayOpt = None,
    prompt_rev: Annotated[str, typer.Option("--rev", help="Revision to record against.")] = "",
    subsystem: Annotated[str, typer.Option("--subsystem")] = "compiler",
    before: Annotated[float | None, typer.Option("--before")] = None,
    after: Annotated[float | None, typer.Option("--after")] = None,
    partition: PartitionOpt = "dev",
) -> None:
    """Record §1.3's before/after Brier for a prompt revision.

    "Any edit to an agent, decomposition or arbiter prompt after the first full
    backtest is recorded with the Brier before and after, so tuning-on-test is
    visible in the record rather than hidden." With no `--after`, the headline
    configuration's measured Brier is used -- the number the revision has to be
    judged against, taken from the measurement rather than typed in.

    That number is the **dev** Brier by default. A prompt revision is a tuning
    decision, and the figure a tuning decision is judged against has to be one
    it was allowed to see: measuring the audit on all scenarios would make
    running the audit the very look at test it exists to expose.
    """
    from cascade.eval.store import record_prompt_revision_brier

    settings = _settings(config)
    split = _frozen_split(settings)
    declaration = _declared_split(settings, split)
    chosen = _partition(partition)
    rev = prompt_rev or settings.llm.prompt_rev
    measured = after
    if measured is None:
        metrics, _ = _metrics_for_config(
            settings, "C01", split, declaration=declaration, partition=chosen
        )
        measured = None if metrics is None else metrics.brier
    updated = record_prompt_revision_brier(
        settings,
        prompt_rev=rev,
        subsystem=subsystem,
        brier_before=before,
        brier_after=measured,
    )
    if not updated:
        _fail(
            f"no prompt_revisions row for ({rev!r}, {subsystem!r}); a revision is "
            "recorded by the migration that makes it, not by this command",
            EXIT_PRECONDITION,
        )
    console.print(
        f"recorded prompt revision {rev!r}/{subsystem}: before="
        f"{'null' if before is None else f'{before:.6f}'}, after="
        f"{'null' if measured is None else f'{measured:.6f}'}"
        + ("" if after is not None else f" (C01 on the {chosen} partition)")
    )


@eval_app.command("equivalence")
def eval_equivalence(
    config: OverlayOpt = None,
    reference: Annotated[
        str, typer.Option("--reference", help="Provider whose self-agreement is the control.")
    ] = "anthropic",
    candidate: Annotated[
        str, typer.Option("--candidate", help="Provider tested against the reference.")
    ] = "bedrock",
    limit: Annotated[
        int, typer.Option("--limit", help="Questions to ask; each is asked three times.")
    ] = 50,
) -> None:
    """Measure whether two providers serve the same model (ADR-0029).

    The three API providers share one cache namespace, which is only sound if
    they serve the same model. This asks each question twice of the reference
    and once of the candidate, and puts a paired bootstrap interval on how much
    more the candidate disagrees with the reference than the reference does
    with itself. Exits 3 on divergence: the provider must then join the key.
    Makes live calls -- 3 x limit of them -- metered against `bench`.
    """
    from cascade.eval.equivalence import run_equivalence
    from cascade.ledger.store import load_scenarios
    from cascade.llm.providers import PROVIDERS

    settings = _settings(config)
    known = sorted(PROVIDERS)
    for name in (reference, candidate):
        if name not in PROVIDERS:
            _fail(f"unknown provider {name!r}; known: {', '.join(known)}", EXIT_PRECONDITION)
    if limit <= 0:
        _fail(f"--limit must be positive, got {limit}", EXIT_PRECONDITION)

    # Loaded as the simulation role: the probe compares providers with each
    # other, never with an outcome, so it is given no way to read one.
    scenarios = tuple(sorted(load_scenarios(settings, role="sim"), key=lambda s: s.scenario_id))
    if not scenarios:
        _fail("scenario registry is empty; run `cascade ledger build` first", EXIT_PRECONDITION)
    try:
        report = run_equivalence(
            settings,
            scenarios[:limit],
            reference=reference,  # type: ignore[arg-type]  # validated above
            candidate=candidate,  # type: ignore[arg-type]
        )
    except ValueError as exc:
        _fail(str(exc), EXIT_PRECONDITION)

    table = Table(title=f"Provider equivalence: {reference} vs {candidate}")
    table.add_column("Quantity", style="cyan")
    table.add_column("Measured", justify="right")
    table.add_row("questions asked", str(report.asked))
    table.add_row("scored (both sides parseable)", str(report.scored))
    table.add_row("dropped, never imputed", str(report.dropped))
    table.add_row(f"mean |a1 - a2|  ({reference} vs itself)", f"{report.within_mean:.4f}")
    table.add_row(f"mean |a1 - b|   ({reference} vs {candidate})", f"{report.cross_mean:.4f}")
    table.add_row("cross - within", f"{report.interval.point:+.4f}")
    table.add_row(
        "95% interval",
        f"[{report.interval.lo:+.4f}, {report.interval.hi:+.4f}]  (B = {report.interval.b})",
    )
    console.print(table)
    if report.divergent:
        _fail(
            f"{candidate!r} disagrees with {reference!r} more than {reference!r} disagrees with "
            "itself. They do not serve the same model, so the provider must join the cache "
            "key domain (ADR-0029).",
            EXIT_PRECONDITION,
        )
    console.print(
        f"[green]no divergence detected[/green] at n = {report.scored}. That supports sharing "
        "the cache namespace; it is not proof of equivalence, and a small n may only mean the "
        "probe was underpowered."
    )


# ---------------------------------------------------------------------------
# report: the Appendix D artifact (M7)
# ---------------------------------------------------------------------------


@app.command("report")
def report(
    config: OverlayOpt = None,
    headline: Annotated[
        str, typer.Option("--headline", help="Cell whose metrics lead the report.")
    ] = "C01",
    replicate_policy: Annotated[str, typer.Option("--replicate-policy")] = "budget_capped",
    out: Annotated[
        str | None, typer.Option("--out", help="Report root; default paths.reports.")
    ] = None,
    partition: PartitionOpt = "test",
) -> None:
    """Write `reports/study_{ts}/` from whatever has been measured (Appendix D).

    **The headline is the test partition.** Every figure in the report --
    metrics, calibration, domains, deltas, dispersion, evidence tiers -- is
    measured on the held-out scenarios alone; the all-scenario figure is
    printed beside the headline, labelled as not being it. `--partition dev`
    writes a tuning report, which says on its face that it is one. Stored
    configurations that were never declared are not scored off dev.

    Writes what exists and names what does not. A quantity that could not be
    produced appears as null in the JSON and in a "Not produced" section in
    `headline.md`; nothing is filled in with the value the spec expects, which
    is the §1 rule stated as a code path rather than as an intention.

    Exits 0 even when the study is incomplete. A report of a blocked milestone
    is a real artifact -- it is the record of what was and was not measurable --
    and making it exit non-zero would mean the only way to produce one is to
    have finished.
    """
    from datetime import UTC, datetime

    from cascade.ensemble.aggregate import convergence_curve
    from cascade.ensemble.store import scores_by_scenario
    from cascade.eval.ablation import (
        CELLS,
        comparison_family,
        grid_replicates,
        missing_cells,
    )
    from cascade.eval.baselines import BASELINES, DIRECT_CONFIG_ID
    from cascade.eval.evidence import EVIDENCE_WINDOW_DAYS, evidence_finding
    from cascade.eval.report import StudyArtifact, git_sha, report_id, write_report
    from cascade.eval.schema import AblationCell
    from cascade.eval.score import (
        calibration_of,
        dispersion_finding,
        domains_of,
        headline_by_partition,
    )
    from cascade.eval.stats import bootstrap_seed
    from cascade.eval.store import (
        available_configs,
        evidence_counts,
        scored_forecasts,
        write_study_report,
    )
    from cascade.eval.supplementary import supplementary_family

    settings = _settings(config)
    split = _frozen_split(settings)
    declaration = _declared_split(settings, split)
    chosen = _partition(partition)
    stored = dict(available_configs(settings))
    now = datetime.now(UTC)
    identifier = report_id(now=now)
    blocked: list[str] = []

    # -- per-config metrics -------------------------------------------------
    # A configuration nobody declared is a tuning variant. Off dev it is not
    # scored at all: its held-out forecasts are never joined to a label, so a
    # report cannot become the place variants get compared on test.
    declared = _declared_configs()
    withheld = sorted(name for name in stored if name not in declared) if chosen != "dev" else []
    direct_rows = scored_forecasts(settings, config_id=DIRECT_CONFIG_ID)
    metrics: list[MetricSet] = []
    scored_by_config: dict[str, tuple[ScoredForecast, ...]] = {}
    for config_id in sorted(stored):
        if config_id in withheld:
            continue
        measured, scored = _metrics_for_config(
            settings,
            config_id,
            split,
            declaration=declaration,
            partition=chosen,
            direct=direct_rows,
        )
        if measured is not None:
            metrics.append(measured)
            scored_by_config[config_id] = scored

    head_metrics = next((entry for entry in metrics if entry.config_id == headline), None)
    head_scored = scored_by_config.get(headline, ())
    if head_metrics is None:
        blocked.append(
            f"Headline Brier, skill scores, calibration and per-domain table: the "
            f"headline configuration {headline!r} has no stored forecasts on the "
            f"{chosen} partition. Run `cascade compile build`, `cascade simulate all` "
            "and `cascade ensemble collapse`."
        )

    # The headline configuration on every partition, so the all-scenario figure
    # sits beside the headline with a label on it. Only for a declared
    # headline: a variant has no business being measured off dev even here.
    headline_partitions: tuple[tuple[Partition, MetricSet | None], ...] = ()
    if headline in declared and headline in stored:
        headline_partitions = _guard(
            lambda: headline_by_partition(
                scored_forecasts(settings, config_id=headline),
                declaration,
                config_id=headline,
                base_rate=split.base_rate,
                direct=() if headline == DIRECT_CONFIG_ID else direct_rows,
                salt=split.study_salt,
            )
        )

    # -- the twelve cells ---------------------------------------------------
    cells: list[AblationCell] = []
    notes: list[str] = []
    for cell in CELLS:
        _planned, note = grid_replicates(
            cell,
            policy=replicate_policy,  # type: ignore[arg-type]
            ablation_cap=settings.ensemble.ablation_replicates,
        )
        notes.append(note)
        cell_metrics = next((entry for entry in metrics if entry.config_id == cell.cell_id), None)
        # Measured, not planned. The planned count is what `grid_replicates`
        # returns and it is already stated in the note above; putting it in
        # `replicates_executed` would answer "how many did it run" with "how
        # many were we going to run", which is precisely the conflation Q1 is
        # about.
        cell_scored = scored_by_config.get(cell.cell_id, ())
        executed_replicates = (
            max(item.n_replicates for item in cell_scored) if cell_scored else None
        )
        cells.append(
            AblationCell(
                cell_id=cell.cell_id,
                config_id=cell.cell_id,
                decomposition=cell.decomposition,
                information_asymmetry=cell.information_asymmetry,
                grounding=cell.grounding,
                replicates_design=cell.replicates_design,
                replicates_executed=executed_replicates,
                scenarios_executed=stored.get(cell.cell_id, 0),
                role=cell.role,
                metrics=cell_metrics,
            )
        )
    absent = missing_cells(sorted(stored))
    if absent:
        blocked.append(
            f"{len(absent)} of {len(CELLS)} ablation cells have no forecasts "
            f"({', '.join(absent)}). Run `cascade eval grid`."
        )

    # -- baselines ----------------------------------------------------------
    baselines: list[tuple[str, str, str, Any]] = []
    for spec in BASELINES:
        measured = next((entry for entry in metrics if entry.config_id == spec.config_id), None)
        baselines.append((spec.baseline_id, spec.name, spec.config_id, measured))
        if measured is None:
            blocked.append(f"Baseline {spec.name!r} ({spec.config_id}): no stored forecasts.")

    # -- significance -------------------------------------------------------
    comparisons = _build_comparisons(
        settings, split, headline=headline, declaration=declaration, partition=chosen
    )
    readings = {
        spec.name: spec.reading
        for spec in (
            *comparison_family(
                available=sorted(stored), headline=headline, eligible=_appendix_c_configs()
            ),
            *supplementary_family(available=sorted(stored), headline=headline),
        )
    }
    if not comparisons:
        blocked.append(
            "Paired bootstrap intervals and Holm-adjusted p-values: no comparison "
            "had forecasts on both sides."
        )

    # -- dispersion and convergence -----------------------------------------
    dispersion = None
    if head_scored:
        dispersion = dispersion_finding(
            head_scored,
            sigma_threshold=settings.ensemble.sigma_multimodal_threshold,
            seed=bootstrap_seed(split.study_salt, "dispersion", headline),
        )
    convergence: tuple[tuple[int, float, float, int], ...] = ()
    try:
        curve = convergence_curve(scores_by_scenario(settings, config_id=headline))
        convergence = tuple(
            (point.n, point.mean_abs_change, point.mean_sigma, point.scenarios) for point in curve
        )
    except Exception as exc:  # noqa: BLE001 -- a missing curve is reported, never fatal
        blocked.append(f"Convergence curve (§9.3): {type(exc).__name__}: {exc}")
    if not convergence and not any(message.startswith("Convergence") for message in blocked):
        blocked.append(
            "Convergence curve (§9.3): no scenario has enough replicates to reach "
            "the first rung."
        )

    # -- accuracy by evidence quality (declared in advance) ------------------
    evidence = None
    if head_scored:
        try:
            counts = evidence_counts(settings, window_days=EVIDENCE_WINDOW_DAYS)
            evidence = evidence_finding(
                head_scored,
                counts,
                seed=bootstrap_seed(split.study_salt, "evidence", headline),
            )
        except Exception as exc:  # noqa: BLE001 -- a missing table is reported, never fatal
            blocked.append(f"Accuracy by evidence tier: {type(exc).__name__}: {exc}")

    # -- per-scenario CSV rows ----------------------------------------------
    baseline_ids = {spec.config_id for spec in BASELINES}
    baseline_rows = tuple(
        (item.config_id, item.scenario_id, item.p_hat, item.outcome)
        for config_id in sorted(baseline_ids & set(scored_by_config))
        for item in scored_by_config[config_id]
    )
    grid_ids = {cell.cell_id for cell in CELLS}
    grid_rows = tuple(
        (item.config_id, item.scenario_id, item.p_hat, item.sigma, item.outcome)
        for config_id in sorted(grid_ids & set(scored_by_config))
        for item in scored_by_config[config_id]
    )

    artifact = StudyArtifact(
        report_id=identifier,
        written_at=now,
        manifest_sha256=split.manifest_sha256,
        git_sha=git_sha(),
        n_scenarios_sealed=split.n_scenarios,
        base_rate=split.base_rate,
        climatology_brier=split.climatology_brier,
        study_salt=split.study_salt,
        models={
            "agent": settings.models.agent,
            "compiler": settings.models.compiler,
            "embedding": settings.models.embedding,
        },
        config_snapshot=_report_config_snapshot(settings),
        headline_config=headline,
        replicate_policy=_replicate_policy_sentence(replicate_policy, settings),
        replicate_notes=tuple(notes),
        metrics=tuple(metrics),
        baselines=tuple(baselines),
        cells=tuple(cells),
        comparisons=tuple(comparisons),
        comparison_readings=readings,
        calibration=calibration_of(head_scored) if head_scored else None,
        per_domain=domains_of(head_scored) if head_scored else (),
        dispersion=dispersion,
        convergence=convergence,
        scored=tuple(head_scored),
        baseline_rows=baseline_rows,
        grid_rows=grid_rows,
        leakage=_leakage_snapshot(settings),
        cost_ledger=_cost_snapshot(settings),
        blocked=tuple(blocked),
        split=declaration,
        partition=chosen,
        headline_partitions=headline_partitions,
        split_interactions=_split_interactions(settings, split, declaration),
        evidence=evidence,
        withheld_configs=tuple(withheld),
    )

    root = Path(out) if out else repo_root() / settings.paths.reports
    directory = write_report(artifact, root=root)
    write_study_report(
        settings,
        report_id=identifier,
        manifest_sha256=split.manifest_sha256,
        git_sha=artifact.git_sha,
        headline_config=headline,
        n_scenarios=split.n_scenarios,
        headline_brier=None if head_metrics is None else head_metrics.brier,
        notes=(
            f"{len(CELLS) - len(absent)}/{len(CELLS)} cells scored; "
            f"{len(blocked)} quantities not produced; headline_brier is the {chosen} "
            f"partition of split {declaration.sha256[:16]}"
        ),
    )

    console.print(f"report written to [bold]{directory}[/bold]")
    _print_partition(declaration, chosen)
    if head_metrics is not None:
        _print_metrics(head_metrics, title=f"Headline -- {headline} on {chosen}")
    if blocked:
        console.print(f"[yellow]{len(blocked)} quantity/quantities not produced:[/yellow]")
        for message in blocked:
            console.print(f"  - {message}")


def _replicate_policy_sentence(policy: str, settings: Settings) -> str:
    """State which reading of Q1 the report applies, in the report's own words."""
    if policy == "design":
        return (
            "Every cell ran at its Appendix C design factor D. No budget cap was "
            "applied, so the ensemble-contribution estimate is an estimate at the "
            "designed replicate count."
        )
    return (
        "Appendix C's D is read as the design factor and "
        f"§10.3/§12.3's {settings.ensemble.ablation_replicates} replicates as a budget "
        "cap on the 11 non-headline cells; the headline cell runs at the full D. "
        "This resolves the Q1 ambiguity in CLAUDE.md. The ensemble contribution "
        "estimated from a capped cell is an estimate at the capped count, not at D."
    )


def _report_config_snapshot(settings: Settings) -> dict[str, Any]:
    """The configuration that produced the report, as primitive data.

    Appendix D asks manifest.json to carry "config". Secrets are Pydantic
    SecretStr and are not in these sections, so this projection cannot leak one.
    """
    return {
        "study": settings.study.model_dump(mode="json"),
        "kernel": settings.kernel.model_dump(mode="json"),
        "aperture": settings.aperture.model_dump(mode="json"),
        "retrieval": settings.retrieval.model_dump(mode="json"),
        "ensemble": settings.ensemble.model_dump(mode="json"),
        "flags": settings.flags.model_dump(mode="json"),
        "llm": {"mode": settings.llm.mode, "prompt_rev": settings.llm.prompt_rev},
        "pinned_stack": {entry.label: entry.pin for entry in PINNED_STACK},
    }


def _leakage_snapshot(settings: Settings) -> dict[str, Any]:
    """Appendix D's leakage_report.json, from what is actually verifiable here.

    Poison-pill hits and memorisation scores are produced by `cascade retrieval`
    commands and are not recomputed here -- recomputing them would be a second
    implementation of the study's leakage claim. What this records is the state
    of the three structural mechanisms §1.3 names, which *is* checkable at
    report time.
    """
    from cascade.eval.store import prompt_revision_audit

    snapshot: dict[str, Any] = {
        "frozen_split_asserted": True,
        "label_grant": "cascade_sim has no grant on scenario_labels (migration 002)",
        "aggregation_role": "cascade_sim (ADR-0022)",
        "poison_pill_hits": None,
        "poison_pill_note": (
            "Measured by `make test-leakage`, not recomputed here; a second "
            "implementation of a leakage check is a second thing to get wrong."
        ),
        "memorization": None,
        "memorization_note": (
            "Measured by `cascade retrieval memorization`; needs " "CASCADE_ANTHROPIC_API_KEY."
        ),
    }
    try:
        snapshot["prompt_revisions"] = prompt_revision_audit(settings)
    except Exception as exc:  # noqa: BLE001 -- reported in the artifact, never fatal
        snapshot["prompt_revisions_error"] = f"{type(exc).__name__}: {exc}"
    return snapshot


def _cost_snapshot(settings: Settings) -> dict[str, Any]:
    """Appendix D's cost_ledger.json: measured spend per phase where recorded.

    Reads the meter checkpoints rather than re-pricing anything. §12.4's live
    ledger reconciles against Langfuse at the end of each phase; that
    reconciliation is M8's acceptance criterion and is not asserted here.
    """
    ledger: dict[str, Any] = {
        "phase_ceilings_usd": {
            phase: str(amount)
            for phase, amount in sorted(settings.budget.phase_ceiling_usd.items())
        },
        "phases": {},
        "note": (
            "Spend is read from the per-phase meter checkpoints. Reconciliation "
            "against the Langfuse total within 2% is M8's criterion (§12.4)."
        ),
    }
    checkpoints = repo_root() / settings.paths.checkpoints
    if checkpoints.is_dir():
        for path in sorted(checkpoints.glob("*.checkpoint.json")):
            try:
                ledger["phases"][path.stem.split(".")[0]] = json.loads(
                    path.read_text(encoding="utf-8")
                )
            except (OSError, ValueError) as exc:
                ledger["phases"][path.stem.split(".")[0]] = {
                    "error": f"{type(exc).__name__}: {exc}"
                }
    return ledger


# ---------------------------------------------------------------------------
# trace: Strata -- determinism, provenance and the cost ledger (M8, spec §8, §11)
# ---------------------------------------------------------------------------


@trace_app.command("explain")
def trace_explain(
    config: OverlayOpt = None,
    run: Annotated[
        str,
        typer.Option("--run", help="Run id to explain; default the first stored run."),
    ] = "",
    factor: Annotated[
        str | None,
        typer.Option("--factor", help="Which outcome factor to trace; default the last moved."),
    ] = None,
) -> None:
    """Walk one run's log from its outcome back to an exogenous shock (§11.2).

    "Every outcome traces back to the exact agent decision that triggered it"
    is the study's strongest claim, and §11.2 asks for it to be demonstrable in
    thirty seconds rather than described. Exits **3** when the chain does not
    reach a root cause, because an incomplete chain is the claim failing, not
    the command failing.
    """
    from cascade.trace.provenance import explain, render
    from cascade.trace.replay import load_replay_targets

    settings = _settings(config)
    if not run:
        # Defaulting rather than requiring: §11.2 asks for this to be
        # demonstrable in thirty seconds, and making the demo start with a
        # database query for a uuid is thirty seconds of its own. Ordered by
        # run_id and taken from the front, so the default is the same run twice.
        targets = load_replay_targets(settings, limit=1)
        if not targets:
            _fail(
                "no stored runs to explain; run `cascade simulate all` or "
                "`cascade eval grid` first",
                EXIT_PRECONDITION,
            )
        run = targets[0].run_id
        console.print(f"[dim]no --run given; explaining the first stored run {run}[/dim]")
    try:
        explanation = explain(settings, run_id=run, factor=factor)
    except LookupError as exc:
        _fail(str(exc), EXIT_PRECONDITION)

    header = Table(title=f"Run {explanation.run_id}")
    header.add_column("Field", style="cyan")
    header.add_column("Value", overflow="fold")
    header.add_row("scenario", explanation.scenario_id)
    header.add_row("config", explanation.config_id)
    header.add_row("replicate", str(explanation.replicate))
    header.add_row("outcome score", f"{explanation.outcome_score:.4f}")
    header.add_row("steps / termination", f"{explanation.steps_run} / {explanation.termination}")
    console.print(header)

    for line in render(explanation):
        console.print(line)

    root = explanation.root
    if root is None or explanation.truncated:
        reason = (
            "the walk hit the depth bound with antecedents left"
            if explanation.truncated
            else "no exogenous movement was recorded for this run"
        )
        _fail(f"chain does not reach a root cause: {reason}", EXIT_PRECONDITION)
    console.print(
        f"[bold green]complete chain: {len(explanation.links)} decision(s) "
        f"to an exogenous shock at step {root.step}[/bold green]"
    )


@trace_app.command("replay")
def trace_replay(
    config: OverlayOpt = None,
    runs: Annotated[int, typer.Option("--runs", help="How many stored runs to replay.")] = 25,
    config_id: Annotated[
        str | None, typer.Option("--config-id", help="Restrict to one configuration.")
    ] = None,
    workers: Annotated[int, typer.Option("--workers", help="Concurrent child processes.")] = 4,
) -> None:
    """Re-run stored runs in fresh processes and compare the event-log hashes.

    M8's first criterion. Each replay runs in its own interpreter under a
    different `PYTHONHASHSEED`: within one process a dependency on dict
    insertion order or on `id()` is perfectly stable, and diverges only across
    one. Nothing is written -- a verification pass that inserted its own copy
    of the log would make the M6 event count drift every time someone checked.

    Exits **3** on any divergence, naming the first step whose state hash
    differs (§8.4: the bisect names the first divergent field, and the fix is
    the source, never a tolerance).
    """
    from cascade.trace.replay import load_replay_targets, verify_replays

    settings = _settings(config)
    targets = load_replay_targets(settings, limit=runs, config_id=config_id)
    if not targets:
        _fail(
            "no stored runs to replay; run `cascade simulate all` first"
            + (f" (config {config_id})" if config_id else ""),
            EXIT_PRECONDITION,
        )

    report = verify_replays(targets, workers=workers, env_file=str(env_file_path()))

    table = Table(title="Replay determinism (spec §8.4, M8)")
    table.add_column("Quantity", style="cyan")
    table.add_column("Measured", justify="right")
    table.add_column("Criterion", justify="right")
    table.add_row("runs replayed", f"{report.attempted:,}", f"{runs:,}")
    table.add_row(
        "byte-identical event-log hash",
        f"{report.matched:,}/{report.attempted:,}",
        f"{report.attempted:,}/{report.attempted:,}",
    )
    table.add_row("child hash seed", CHILD_HASH_SEED, "differs from the parent's")
    table.add_row("elapsed", f"{report.elapsed_s:.1f}s", "-")
    console.print(table)

    if report.diverged:
        detail = Table(title="Divergences")
        detail.add_column("run", style="cyan", overflow="fold")
        detail.add_column("first divergent step", justify="right")
        detail.add_column("stored state hash")
        detail.add_column("replayed state hash")
        detail.add_column("error", overflow="fold")
        for outcome in report.diverged:
            detail.add_row(
                outcome.run_id,
                "-" if outcome.first_divergent_step is None else str(outcome.first_divergent_step),
                (outcome.expected_state_hash or "-")[:16],
                (outcome.actual_state_hash or "-")[:16],
                outcome.error or "",
            )
        console.print(detail)
        _fail(
            f"{len(report.diverged)} of {report.attempted} run(s) did not replay identically; "
            "fix the source of the nondeterminism, never add a tolerance",
            EXIT_PRECONDITION,
        )
    console.print("[bold green]every replayed run reproduced its event-log hash[/bold green]")


@trace_app.command("cost")
def trace_cost(
    config: OverlayOpt = None,
    config_id: Annotated[
        str | None, typer.Option("--config-id", help="Restrict to one configuration.")
    ] = None,
) -> None:
    """Reconcile the run ledger against Langfuse (spec §12.4, M8).

    Two independent records of the same spend: the meter prices every call from
    the pinned table and writes the total to `runs.cost_usd`; the tracer emits
    one Langfuse generation per call carrying the same cost. §12.4 blocks the
    report on a discrepancy above 2%, so this exits **3** there.

    An *unreachable* Langfuse is reported as unreconciled rather than as a
    discrepancy: `tracing.py` is allowed to degrade to a no-op because
    observability must never fail a run, and "we could not check" is not
    "we checked".
    """
    from cascade.trace.ledger import reconcile

    settings = _settings(config)
    result = reconcile(settings, config_id=config_id)

    table = Table(title="Cost ledger reconciliation (spec §12.4)")
    table.add_column("Source", style="cyan")
    table.add_column("USD", justify="right")
    table.add_column("calls", justify="right")
    table.add_column("tokens in / out", justify="right")
    table.add_column("detail", overflow="fold")
    table.add_row(
        result.local.source,
        f"${result.local.total_usd:.6f}",
        f"{result.local.calls:,}",
        f"{result.local.input_tokens:,} / {result.local.output_tokens:,}",
        result.local.detail,
    )
    if result.remote is None:
        table.add_row("langfuse", "not measured", "-", "-", "unreachable or not configured")
    else:
        table.add_row(
            result.remote.source,
            f"${result.remote.total_usd:.6f}",
            f"{result.remote.calls:,}",
            f"{result.remote.input_tokens:,} / {result.remote.output_tokens:,}",
            result.remote.detail,
        )
    console.print(table)

    relative = result.relative_difference
    if result.vacuous:
        console.print("[yellow]no spend recorded on either side[/yellow]")
    console.print(
        "difference: "
        + (
            "[yellow]not measured[/yellow]"
            if relative is None
            else f"[bold]{relative:.4%}[/bold] against a {result.tolerance:.0%} tolerance"
        )
    )

    if result.remote is None:
        _fail(
            "Langfuse reported no total, so the ledger is unreconciled. §12.4 makes "
            "reconciliation a gate on the report; bring Langfuse up (`make up`) and "
            "re-run, or record the phase as unreconciled in the report.",
            EXIT_PRECONDITION,
        )
    if result.vacuous:
        _fail(
            "neither record holds any spend, so there is nothing to reconcile. Zero "
            "agrees with zero to 0.0000% and passing on that would be a gate that "
            "cannot fail -- run a phase that reaches the model first.",
            EXIT_PRECONDITION,
        )
    if not result.within_tolerance:
        _fail(
            f"ledger and Langfuse differ by {relative:.4%}, above the "
            f"{result.tolerance:.0%} tolerance; §12.4 calls that a bug in the meter",
            EXIT_PRECONDITION,
        )
    console.print("[bold green]cost ledger reconciles within tolerance[/bold green]")


@trace_app.command("status")
def trace_status(config: OverlayOpt = None) -> None:
    """What is replayable and traceable right now."""
    from cascade.trace.store import run_stats

    settings = _settings(config)
    stats = run_stats(settings)

    table = Table(title="Provenance (M8, spec §8, §11)")
    table.add_column("Quantity", style="cyan")
    table.add_column("Measured", justify="right")
    table.add_column("Criterion", justify="right")
    table.add_row("stored runs", f"{stats.runs:,}", "36,000")
    table.add_row("logged decision events", f"{stats.events:,}", f"{TARGET_EVENTS:,} +/- 5%")
    table.add_row(
        "distinct event-log hashes",
        f"{stats.distinct_event_hashes:,}",
        f"{stats.runs:,} (one per run)",
    )
    table.add_row("distinct configs", ", ".join(stats.configs) or "-", "-")
    console.print(table)
    console.print(
        "[dim]`cascade trace replay --runs 25` checks the M8 hash criterion; "
        "`cascade trace explain --run <id>` walks one outcome to its root cause.[/dim]"
    )


# ---------------------------------------------------------------------------
# dev: self-test hooks exercised by the acceptance tests
# ---------------------------------------------------------------------------


@dev_app.command("budget-probe")
def budget_probe(
    ceiling: Annotated[float, typer.Option(help="Ceiling in USD for this probe.")] = 0.01,
    phase: Annotated[str, typer.Option(help="Phase name to book spend against.")] = "simulate",
) -> None:
    """Spend synthetic tokens against a real meter until the ceiling breaks.

    Exercises the production abort path end to end -- meter -> BudgetExceeded ->
    CLI boundary -> non-zero exit -- so the M0 acceptance criterion is tested
    against the real code rather than a mock.
    """
    from cascade.llm.meter import CostMeter
    from cascade.llm.types import Usage

    settings = _settings(None)
    meter = CostMeter(settings, phase, ceiling_usd=Decimal(str(ceiling)))
    meter.set_resume_state({"probe": True, "completed_units": 0})

    usage = Usage(input_tokens=100_000, output_tokens=10_000)
    for index in range(1_000):
        meter.set_resume_state({"probe": True, "completed_units": index})
        meter.record(model=settings.models.agent, usage=usage, batch=False)
    console.print(f"no breach after 1000 calls; spent ${meter.total_usd:.6f}")


@dev_app.command("cost")
def dev_cost(
    model: Annotated[str, typer.Option(help="Model id from the pricing table.")] = "",
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    batch: bool = False,
) -> None:
    """Print the exact USD cost of a hypothetical call. Used by the ledger tests."""
    from cascade.llm.meter import compute_cost
    from cascade.llm.types import Usage

    settings = _settings(None)
    target = model or settings.models.agent
    cost = compute_cost(
        usage=Usage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_input_tokens=cache_read_tokens,
            cache_creation_input_tokens=cache_write_tokens,
        ),
        price=settings.price_for(target),
        multipliers=settings.pricing_multipliers,
        batch=batch,
        cache_ttl=settings.prompt_cache.ttl,
    )
    console.print(f"{cost:.6f}")


@dev_app.command("config")
def dev_config(config: OverlayOpt = None) -> None:
    """Dump the resolved configuration, secrets redacted."""
    import json

    settings = _settings(config)
    payload: dict[str, Any] = json.loads(settings.model_dump_json())
    console.print_json(data=payload)


# ---------------------------------------------------------------------------
# Error boundary
# ---------------------------------------------------------------------------


def main() -> int:
    """Console-script entry point with the process-wide error boundary.

    Maps the failure modes the spec cares about onto distinct exit codes so a
    supervising script can tell a budget abort from a replay miss from a bug.
    """
    try:
        # With standalone_mode=False click *returns* the exit code for a
        # typer.Exit rather than raising it, so the return value is
        # load-bearing. Ignoring it silently turns every `raise typer.Exit(3)`
        # into a zero exit -- a stub that claims success.
        result = app(standalone_mode=False)
        if isinstance(result, int):
            return result
    except typer.Exit as exc:
        return int(exc.exit_code)
    except click_exceptions() as exc:  # pragma: no cover - argument errors
        err_console.print(f"[red]{exc}[/red]")
        return EXIT_ERROR
    except BudgetExceeded as exc:
        err_console.print(f"[bold red]budget ceiling breached[/bold red] {exc}")
        return EXIT_BUDGET_BREACH
    except CacheMiss as exc:
        err_console.print(f"[bold red]cache miss in replay mode[/bold red] {exc}")
        return EXIT_CACHE_MISS
    except PromptTooShortToCache as exc:
        err_console.print(f"[bold red]prompt cache misconfigured[/bold red] {exc}")
        return EXIT_PRECONDITION
    except ProviderNotReady as exc:
        err_console.print(f"[bold red]model provider not ready[/bold red] {exc}")
        return EXIT_PRECONDITION
    except SplitError as exc:
        # The dev/test guard. A refusal to touch a held-out scenario, or to run
        # against a split that is not the declared one, is a precondition
        # failure wherever it is raised from -- including from a tuning path
        # that did not think to catch it.
        err_console.print(f"[bold red]dev/test split[/bold red] {exc}")
        return EXIT_PRECONDITION
    except KeyboardInterrupt:  # pragma: no cover
        err_console.print("[yellow]interrupted[/yellow]")
        return EXIT_ERROR
    return EXIT_OK


def click_exceptions() -> type[BaseException] | tuple[type[BaseException], ...]:
    """Return click's user-error types without importing click at module scope."""
    import click

    return (click.ClickException, click.UsageError)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
