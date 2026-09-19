"""Forward-only SQL migration runner.

No ORM, no autogeneration: the partition strategy at M3 is load-bearing and
has to be explicit in SQL (spec §13). Migrations are executed by ``psql``
rather than by the driver, because they legitimately use psql-level constructs
(``\\gexec``, ``-v`` variables) to create roles with secrets that must not be
interpolated into a SQL string by hand.

Applied versions are tracked with a checksum, so editing an already-applied
migration is detected instead of silently diverging environments.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from cascade.config import Settings, child_environment, repo_root

__all__ = ["Migration", "MigrationError", "applied_versions", "apply_all", "discover", "pending"]

MIGRATIONS_DIR = repo_root() / "migrations"


class MigrationError(RuntimeError):
    """A migration failed to apply, or an applied migration was edited."""


@dataclass(frozen=True)
class Migration:
    version: str
    path: Path
    checksum: str

    @property
    def name(self) -> str:
        return self.path.name


def _checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def discover(directory: Path | None = None) -> list[Migration]:
    """Return every migration on disk, ordered by version.

    Ordering is lexical on the numeric prefix, which is why the files are
    zero-padded: ``010`` must not sort before ``002``.
    """
    root = directory or MIGRATIONS_DIR
    if not root.is_dir():
        return []
    out: list[Migration] = []
    for path in sorted(root.glob("*.sql")):
        version = path.stem.split("_", 1)[0]
        if not version.isdigit():
            raise MigrationError(
                f"migration {path.name!r} does not start with a numeric version prefix"
            )
        out.append(Migration(version=version, path=path, checksum=_checksum(path)))
    return out


def applied_versions(settings: Settings) -> dict[str, str]:
    """Map applied version -> recorded checksum.

    Returns empty when the bookkeeping table does not exist yet, which is the
    expected state before migration 001 has ever run.
    """
    import psycopg

    with (
        psycopg.connect(settings.database_url("admin"), connect_timeout=10) as conn,
        conn.cursor() as cur,
    ):
        cur.execute("SELECT to_regclass('public.schema_migrations') IS NOT NULL")
        row = cur.fetchone()
        if row is None or not row[0]:
            return {}
        cur.execute("SELECT version, checksum FROM schema_migrations")
        return {str(v): str(c) for v, c in cur.fetchall()}


def pending(settings: Settings, directory: Path | None = None) -> list[Migration]:
    """Return migrations not yet applied, verifying applied ones are unedited."""
    on_disk = discover(directory)
    already = applied_versions(settings)
    out: list[Migration] = []
    for migration in on_disk:
        recorded = already.get(migration.version)
        if recorded is None:
            out.append(migration)
        elif recorded != migration.checksum:
            raise MigrationError(
                f"migration {migration.name} has changed since it was applied "
                f"(recorded {recorded[:12]}, on disk {migration.checksum[:12]}). "
                "Migrations are forward-only: add a new one instead of editing this."
            )
    return out


def _psql_command(settings: Settings) -> list[str]:
    """Prefer a local ``psql``; otherwise run the one inside the container."""
    # No secret in either command: a process's arguments are readable by every
    # local user (`ps`), so the credential travels in PGPASSWORD instead.
    local = shutil.which("psql")
    if local is not None:
        return [local, settings.database_url("admin", with_secret=False)]
    docker = shutil.which("docker")
    if docker is None:
        raise MigrationError(
            "neither psql nor docker is available; cannot apply migrations. "
            "Install libpq (brew install libpq) or start Docker."
        )
    return [
        docker,
        "compose",
        "exec",
        "-T",
        # Name only: compose copies the value from this process's environment.
        "-e",
        "PGPASSWORD",
        "postgres",
        "psql",
        f"postgresql://{settings.database.admin_user}@localhost:5432/{settings.database.name}",
    ]


def _psql_quote(value: str, *, what: str) -> str:
    """Quote ``value`` for a psql ``\\set`` line. Pure.

    Inside single quotes psql reads ``''`` as a quote and ``\\`` as a
    backslash (verified against psql 16). A newline would end the meta-command
    and let the rest of the value run as SQL, so it is refused.
    """
    if "\n" in value or "\r" in value:
        raise MigrationError(f"{what} contains a line break and cannot be passed to psql safely")
    escaped = value.replace("\\", "\\\\").replace("'", "''")
    return f"'{escaped}'"


def _role_preamble(settings: Settings) -> str:
    """The ``\\set`` lines that hand migration 001 its role passwords.

    On stdin rather than as ``-v name=value`` arguments, for the same reason
    the login credential is in PGPASSWORD: arguments are world-readable.
    """
    sim = _psql_quote(_secret(settings, "sim"), what="CASCADE_DB_SIM_PASSWORD")
    evl = _psql_quote(_secret(settings, "eval"), what="CASCADE_DB_EVAL_PASSWORD")
    return f"\\set sim_password {sim}\n\\set eval_password {evl}\n"


def _secret(settings: Settings, which: str) -> str:
    value = {
        "sim": settings.db_sim_password,
        "eval": settings.db_eval_password,
    }[which]
    if value is None:
        raise MigrationError(
            f"CASCADE_DB_{which.upper()}_PASSWORD is not set; migration 001 creates the "
            f"cascade_{which} role and refuses to give it an empty password"
        )
    return value.get_secret_value()


def apply_one(settings: Settings, migration: Migration) -> None:
    """Apply a single migration, then record it."""
    import psycopg

    command = [*_psql_command(settings), "-v", "ON_ERROR_STOP=1", "-f", "-"]
    result = subprocess.run(  # noqa: S603 -- fixed argv, no shell
        command,
        input=_role_preamble(settings) + migration.path.read_text(encoding="utf-8"),
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
        env=child_environment(PGPASSWORD=settings.database_secret("admin")),
    )
    if result.returncode != 0:
        raise MigrationError(
            f"migration {migration.name} failed (exit {result.returncode}):\n"
            f"{result.stderr.strip()}"
        )

    with (
        psycopg.connect(settings.database_url("admin"), connect_timeout=10) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(
            "INSERT INTO schema_migrations (version, checksum) VALUES (%s, %s) "
            "ON CONFLICT (version) DO UPDATE SET checksum = EXCLUDED.checksum",
            (migration.version, migration.checksum),
        )
        conn.commit()


def apply_all(settings: Settings, directory: Path | None = None) -> list[Migration]:
    """Apply every pending migration in order. Returns what was applied."""
    todo = pending(settings, directory)
    for migration in todo:
        apply_one(settings, migration)
    return todo
