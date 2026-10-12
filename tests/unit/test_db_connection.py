"""How Cascade reaches Postgres: TLS, IAM tokens, and secrets kept off argv (ADR-0034).

URLs are parsed with psycopg's own conninfo parser rather than string-matched,
so these tests check what libpq would receive, not what the URL looks like.
"""

from __future__ import annotations

import subprocess
from typing import Any

import pytest
from psycopg.conninfo import conninfo_to_dict
from pydantic import SecretStr, ValidationError

import cascade.config as config_module
import cascade.db as db_module
from cascade.config import DatabaseConfig, Settings
from cascade.db import Migration, MigrationError, _psql_quote

# Every character that re-parses a URL if left raw. RDS-generated passwords
# and IAM tokens contain several of them.
HOSTILE = "p#a?s%s&w@r:d/ =+"


def with_database(settings: Settings, **update: Any) -> Settings:
    return settings.model_copy(
        update={"database": DatabaseConfig(**{**settings.database.model_dump(), **update})}
    )


def aurora(settings: Settings, **update: Any) -> Settings:
    return with_database(
        settings,
        host="c.cluster-x.us-east-1.rds.amazonaws.com",
        port=5432,
        sslmode="verify-full",
        sslrootcert="/etc/ssl/rds/global-bundle.pem",
        **update,
    )


# ---------------------------------------------------------------------------
# The URL
# ---------------------------------------------------------------------------


def test_a_hostile_password_reaches_libpq_intact(settings: Settings) -> None:
    hostile = settings.model_copy(update={"db_sim_password": SecretStr(HOSTILE)})
    parsed = conninfo_to_dict(hostile.database_url("sim"))
    assert parsed["password"] == HOSTILE
    assert parsed["user"] == "cascade_sim"
    assert parsed["host"] == "localhost" and parsed["dbname"] == "cascade"


def test_sslmode_is_always_explicit_so_the_shell_cannot_weaken_it(settings: Settings) -> None:
    assert conninfo_to_dict(settings.database_url("sim"))["sslmode"] == "prefer"
    parsed = conninfo_to_dict(aurora(settings).database_url("sim"))
    assert parsed["sslmode"] == "verify-full"
    assert parsed["sslrootcert"] == "/etc/ssl/rds/global-bundle.pem"


def test_the_argv_form_carries_no_secret(settings: Settings) -> None:
    hostile = settings.model_copy(update={"db_admin_password": SecretStr("topsecret")})
    url = hostile.database_url("admin", with_secret=False)
    assert "topsecret" not in url
    assert "password" not in conninfo_to_dict(url)


@pytest.mark.parametrize(
    ("update", "message"),
    [
        ({"sslmode": "verify-full"}, "needs database.sslrootcert"),
        ({"auth": "iam", "sslmode": "verify-full", "sslrootcert": "/ca.pem"}, "iam_region"),
        ({"auth": "iam", "iam_region": "us-east-1"}, "bearer credential"),
    ],
)
def test_incoherent_database_settings_are_refused_at_load(
    settings: Settings, update: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        with_database(settings, **update)


# ---------------------------------------------------------------------------
# IAM tokens
# ---------------------------------------------------------------------------


def test_iam_auth_replaces_the_password_with_a_token_for_the_right_role(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []
    token = "c.cluster-x:5432/?Action=connect&DBUser=cascade_eval&X-Amz-Signature=ab%2Fcd"

    def fake(**kwargs: Any) -> str:
        calls.append(kwargs)
        return token

    monkeypatch.setattr(config_module, "_rds_auth_token", fake)
    iam = aurora(settings, auth="iam", iam_region="us-east-1").model_copy(
        update={"db_eval_password": SecretStr("stored-password-must-not-be-used")}
    )
    parsed = conninfo_to_dict(iam.database_url("eval"))
    assert parsed["password"] == token
    # The admin secret is generated and rotated by RDS; it never becomes a token.
    admin = iam.model_copy(update={"db_admin_password": SecretStr("rds-managed")})
    assert conninfo_to_dict(admin.database_url("admin"))["password"] == "rds-managed"
    assert calls == [
        {
            "host": "c.cluster-x.us-east-1.rds.amazonaws.com",
            "port": 5432,
            "user": "cascade_eval",
            "region": "us-east-1",
        }
    ]


def test_the_real_signer_uses_the_configured_region_not_the_ambient_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """boto3's own signer, offline: signing is local computation."""
    pytest.importorskip("boto3")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIDEXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY")
    monkeypatch.delenv("AWS_SESSION_TOKEN", raising=False)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.setenv("AWS_REGION", "eu-west-1")  # the decoy
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-west-1")
    token = config_module._rds_auth_token(
        host="c.cluster-x.us-east-1.rds.amazonaws.com",
        port=5432,
        user="cascade_sim",
        region="us-east-1",
    )
    assert "Action=connect" in token and "DBUser=cascade_sim" in token
    assert "%2Fus-east-1%2Frds-db%2Faws4_request" in token
    assert "eu-west-1" not in token


# ---------------------------------------------------------------------------
# Secrets stay off the command line
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("have_psql", [True, False])
def test_the_psql_command_never_carries_a_secret(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, have_psql: bool
) -> None:
    secret = settings.model_copy(update={"db_admin_password": SecretStr("topsecret")})
    monkeypatch.setattr(
        db_module.shutil,
        "which",
        lambda name: f"/usr/bin/{name}" if (name == "psql") == have_psql else None,
    )
    command = db_module._psql_command(secret)
    assert not any("topsecret" in part for part in command)
    if not have_psql:
        # Name only: compose copies the value from the environment.
        assert command[command.index("-e") + 1] == "PGPASSWORD"


def test_a_migration_gets_its_secrets_by_environment_and_stdin_only(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    secret = settings.model_copy(
        update={
            "db_admin_password": SecretStr("admin-secret"),
            "db_sim_password": SecretStr("si'm\\secret"),
            "db_eval_password": SecretStr("eval-secret"),
        }
    )
    seen: dict[str, Any] = {}

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.update(command=command, **kwargs)
        return subprocess.CompletedProcess(command, 0, "", "")

    class FakeConnection:
        def __enter__(self) -> FakeConnection:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def cursor(self) -> FakeConnection:
            return self

        def execute(self, *args: object) -> None:
            return None

        def commit(self) -> None:
            return None

    import psycopg

    monkeypatch.setattr(db_module.shutil, "which", lambda name: "/usr/bin/psql")
    monkeypatch.setattr(db_module.subprocess, "run", fake_run)
    monkeypatch.setattr(psycopg, "connect", lambda *a, **k: FakeConnection())

    path = tmp_path / "001_probe.sql"
    path.write_text("SELECT 1;\n", encoding="utf-8")
    db_module.apply_one(secret, Migration(version="001", path=path, checksum="x"))

    for value in ("admin-secret", "eval-secret", "secret"):
        assert not any(value in part for part in seen["command"]), value
    assert seen["env"]["PGPASSWORD"] == "admin-secret"
    assert seen["input"] == (
        "\\set sim_password 'si''m\\\\secret'\n\\set eval_password 'eval-secret'\nSELECT 1;\n"
    )


def test_a_line_break_in_a_password_is_refused_not_smuggled() -> None:
    """It would end the \\set meta-command and run the remainder as SQL."""
    with pytest.raises(MigrationError, match="line break"):
        _psql_quote("x'\nDROP TABLE scenario_labels; --", what="CASCADE_DB_SIM_PASSWORD")
