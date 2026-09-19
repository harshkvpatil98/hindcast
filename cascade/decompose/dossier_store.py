"""Persistence for scenario dossiers (migration 019, ADR-0037).

One dossier per scenario for the whole study, upserted on ``scenario_id`` like
a compiled graph. The stored hash makes a hand-edited report detectable, and
the refused claims are kept beside the accepted ones: they never reach a
prompt, and they are the record the unsupported-claim rate is read off.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from cascade.canonical import canonical_json
from cascade.config import Settings
from cascade.decompose.dossier import Dossier, WriteOutcome, dossier_hash, render

__all__ = [
    "DossierMismatch",
    "DossierStats",
    "MissingDossiers",
    "dossier_stats",
    "load_dossiers",
    "situations_for",
    "verify_dossier_hashes",
    "write_dossier",
    "written_scenarios",
]

Role = Literal["admin", "sim", "eval"]


class MissingDossiers(RuntimeError):
    """The dossier is enabled and some scenarios have none.

    Raised rather than rendered as an empty report: a phase that quietly ran
    some scenarios with the report and some without would produce a headline
    over a mixture nobody configured. Exit code 3 at the CLI boundary.
    """

    def __init__(self, scenario_ids: tuple[str, ...]) -> None:
        self.scenario_ids = scenario_ids
        shown = ", ".join(scenario_ids[:5]) + (" ..." if len(scenario_ids) > 5 else "")
        super().__init__(
            f"dossier.enabled is set and {len(scenario_ids)} scenario(s) have no stored "
            f"dossier ({shown}); run `cascade compile dossier` first"
        )


@dataclass(frozen=True)
class DossierMismatch:
    """A stored dossier whose content no longer matches its recorded hash."""

    scenario_id: str
    recorded: str
    recomputed: str


@dataclass(frozen=True)
class DossierStats:
    """What `cascade compile dossier` reports, measured from the stored rows."""

    written: int
    empty: int
    claims: int
    dropped: int
    dropped_by_reason: tuple[tuple[str, int], ...]
    mean_excerpts: float

    @property
    def dropped_rate(self) -> float:
        """Refused claims over all claims the writer emitted."""
        emitted = self.claims + self.dropped
        return self.dropped / emitted if emitted else 0.0


def _connect(settings: Settings, role: Role = "admin") -> Any:
    import psycopg

    return psycopg.connect(settings.database_url(role), connect_timeout=30)


def write_dossier(settings: Settings, outcome: WriteOutcome) -> None:
    """Persist one dossier. Idempotent on ``scenario_id``."""
    payload = canonical_json(outcome.dossier.canonical())
    dropped = canonical_json([claim.model_dump(mode="json") for claim in outcome.dropped])
    with _connect(settings) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO scenario_dossiers (
                scenario_id, dossier_sha256, dossier, n_claims, n_dropped, dropped,
                n_excerpts, llm_calls, writer_model, prompt_rev
            ) VALUES (%s, %s, %s::jsonb, %s, %s, %s::jsonb, %s, %s, %s, %s)
            ON CONFLICT (scenario_id) DO UPDATE SET
                dossier_sha256 = EXCLUDED.dossier_sha256,
                dossier = EXCLUDED.dossier,
                n_claims = EXCLUDED.n_claims,
                n_dropped = EXCLUDED.n_dropped,
                dropped = EXCLUDED.dropped,
                n_excerpts = EXCLUDED.n_excerpts,
                llm_calls = EXCLUDED.llm_calls,
                writer_model = EXCLUDED.writer_model,
                prompt_rev = EXCLUDED.prompt_rev,
                written_at = now()
            """,
            (
                outcome.scenario_id,
                outcome.dossier_sha256,
                payload,
                outcome.dossier.n_claims,
                len(outcome.dropped),
                dropped,
                outcome.n_excerpts,
                outcome.llm_calls,
                settings.models.compiler,
                settings.llm.prompt_rev,
            ),
        )
        conn.commit()


def written_scenarios(settings: Settings) -> set[str]:
    """Scenario ids that already have a dossier, so a restart skips them
    (invariant 8)."""
    with _connect(settings) as conn, conn.cursor() as cur:
        cur.execute("SELECT scenario_id FROM scenario_dossiers")
        return {str(row[0]) for row in cur.fetchall()}


def _from_row(payload: Mapping[str, Any]) -> Dossier:
    return Dossier.model_validate(
        {
            **payload,
            "evidence": [tuple(item) for item in payload["evidence"]],
        }
    )


def load_dossiers(settings: Settings, *, role: Role = "sim") -> dict[str, tuple[Dossier, str]]:
    """Every stored dossier with its recorded hash, keyed by scenario id.

    Read as ``sim`` by default -- the role that builds agent prompts -- so the
    grant in migration 019 is exercised by the path that depends on it.
    """
    with _connect(settings, role) as conn, conn.cursor() as cur:
        cur.execute("SELECT scenario_id, dossier, dossier_sha256 FROM scenario_dossiers ORDER BY 1")
        rows = cur.fetchall()
    return {str(row[0]): (_from_row(row[1]), str(row[2])) for row in rows}


def situations_for(
    settings: Settings,
    scenario_ids: tuple[str, ...],
    *,
    role: Role = "sim",
) -> dict[str, tuple[str, str | None]]:
    """The rendered report and its hash for each scenario: ``(text, sha256)``.

    Preserves the all-or-nothing rule. With the dossier off every scenario
    maps to ``("", None)`` and nothing is read, so a disabled configuration
    never touches the table and its prompts stay byte-identical to the ones
    recorded before the dossier existed. With it on, a scenario without a
    stored dossier is an error, never an empty report.
    """
    if not settings.dossier.enabled:
        return {scenario_id: ("", None) for scenario_id in sorted(scenario_ids)}
    stored = load_dossiers(settings, role=role)
    missing = tuple(sorted(set(scenario_ids) - set(stored)))
    if missing:
        raise MissingDossiers(missing)
    return {
        scenario_id: (
            render(stored[scenario_id][0], max_chars=settings.dossier.prompt_chars),
            stored[scenario_id][1],
        )
        for scenario_id in sorted(scenario_ids)
    }


def verify_dossier_hashes(settings: Settings) -> tuple[DossierMismatch, ...]:
    """Recompute every stored dossier's hash and report disagreements."""
    mismatches: list[DossierMismatch] = []
    for scenario_id, (dossier, recorded) in sorted(load_dossiers(settings, role="admin").items()):
        recomputed = dossier_hash(dossier)
        if recomputed != recorded:
            mismatches.append(
                DossierMismatch(scenario_id=scenario_id, recorded=recorded, recomputed=recomputed)
            )
    return tuple(mismatches)


def dossier_stats(settings: Settings) -> DossierStats:
    """Measured figures over the stored dossiers, including the refusal
    reasons -- the unsupported-claim rate is the dossier's own integrity
    number and is reported wherever the dossier is."""
    with _connect(settings) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*), count(*) FILTER (WHERE n_claims = 0), "
            "coalesce(sum(n_claims), 0), coalesce(sum(n_dropped), 0), "
            "coalesce(avg(n_excerpts), 0) FROM scenario_dossiers"
        )
        written, empty, claims, dropped, mean_excerpts = cur.fetchone()
        cur.execute(
            "SELECT split_part(item->>'reason', ':', 1) AS reason, count(*) "
            "FROM scenario_dossiers, jsonb_array_elements(dropped) AS item "
            "GROUP BY 1 ORDER BY 1"
        )
        reasons = tuple((str(reason), int(count)) for reason, count in cur.fetchall())
    return DossierStats(
        written=int(written),
        empty=int(empty),
        claims=int(claims),
        dropped=int(dropped),
        dropped_by_reason=reasons,
        mean_excerpts=float(mean_excerpts),
    )
