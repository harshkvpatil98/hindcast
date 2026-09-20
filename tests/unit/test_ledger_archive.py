"""The tier-0 archive (M12): the sealed registry must survive losing the database.

It did not, once: before M10 the volume was lost and the frozen split with it.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from cascade.canonical import canonical_json
from cascade.ledger.archive import ArchiveCorrupt, dump_archive, load_archive
from cascade.ledger.manifest import compute_manifest
from tests.unit.test_ledger_manifest import record, registry

SALT = "cascade-test-salt"


def sealed(n: int = 8) -> tuple[tuple, str]:  # type: ignore[type-arg]
    records = registry(n)
    return records, compute_manifest(records)


def test_a_round_trip_restores_every_field_and_the_same_seal() -> None:
    records, manifest = sealed()
    restored = load_archive(dump_archive(records, manifest_sha256=manifest, study_salt=SALT))
    assert restored.manifest_sha256 == manifest
    assert restored.study_salt == SALT
    assert restored.records == tuple(sorted(records, key=lambda r: r.scenario.scenario_id))
    assert compute_manifest(restored.records) == manifest


def test_the_archive_does_not_depend_on_load_order() -> None:
    records, manifest = sealed()
    forward = dump_archive(records, manifest_sha256=manifest, study_salt=SALT)
    backward = dump_archive(tuple(reversed(records)), manifest_sha256=manifest, study_salt=SALT)
    assert forward == backward


def test_a_drifted_registry_cannot_be_archived_under_the_sealed_hash() -> None:
    records, manifest = sealed()
    drifted = (*records[:-1], record("s007", outcome=0))  # the sealed outcome was 1
    with pytest.raises(ArchiveCorrupt, match="find out what changed"):
        dump_archive(drifted, manifest_sha256=manifest, study_salt=SALT)


def test_a_reworded_question_is_caught_though_the_manifest_cannot_see_it() -> None:
    """The seal covers four fields; the question an agent is asked is not one."""
    records, manifest = sealed()
    document = json.loads(dump_archive(records, manifest_sha256=manifest, study_salt=SALT))
    document["records"][0]["scenario"]["question"] = "Will something else entirely happen?"
    with pytest.raises(ArchiveCorrupt, match="content digest"):
        load_archive(json.dumps(document))


def test_a_flipped_label_is_caught_even_if_the_content_digest_is_recomputed() -> None:
    """An attacker who fixes up the file digest still cannot match the seal."""
    records, manifest = sealed()
    document = json.loads(dump_archive(records, manifest_sha256=manifest, study_salt=SALT))
    document["records"][0]["label"]["outcome"] = 1 - document["records"][0]["label"]["outcome"]
    document["content_sha256"] = hashlib.sha256(
        canonical_json(document["records"]).encode("utf-8")
    ).hexdigest()
    with pytest.raises(ArchiveCorrupt, match="not the sealed split"):
        load_archive(json.dumps(document))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(format=99),
        lambda d: d.pop("content_sha256"),
        lambda d: d.update(n_scenarios=3),
        lambda d: d["records"].pop(),
    ],
)
def test_a_malformed_archive_is_refused(mutate) -> None:  # type: ignore[no-untyped-def]
    records, manifest = sealed()
    document = json.loads(dump_archive(records, manifest_sha256=manifest, study_salt=SALT))
    mutate(document)
    with pytest.raises(ArchiveCorrupt):
        load_archive(json.dumps(document))


def test_garbage_is_refused_and_empty_is_never_archived() -> None:
    with pytest.raises(ArchiveCorrupt, match="not JSON"):
        load_archive("not an archive")
    with pytest.raises(ValueError, match="empty registry"):
        dump_archive((), manifest_sha256="0" * 64, study_salt=SALT)


# ---------------------------------------------------------------------------
# The commands, against an in-memory stand-in for the registry tables
# ---------------------------------------------------------------------------


class FakeRegistry:
    """What `ledger export` / `restore` read and write, without Postgres."""

    def __init__(self, records: tuple = (), manifest: str | None = None) -> None:  # type: ignore[type-arg]
        self.records = records
        self.manifest = manifest
        self.corrupt_on_write = False

    def install(self, monkeypatch: pytest.MonkeyPatch, *, salt: str) -> None:
        import cascade.ledger.store as store

        def read_manifest(settings: object, *, role: str = "sim") -> object:
            if self.manifest is None:
                return None
            return store.SealedManifest(
                manifest_sha256=self.manifest,
                sealed_at=__import__("datetime").datetime(2026, 9, 19),
                n_scenarios=len(self.records),
                n_yes=0,
                yes_rate=0.5,
                climatology_brier=0.25,
                study_salt=salt,
                notes="",
            )

        def write_registry(settings: object, records: tuple, *, replace: bool = False) -> int:  # type: ignore[type-arg]
            if self.records and not replace:
                raise RuntimeError("scenarios are already loaded. Pass --replace")
            self.records = records[:-1] if self.corrupt_on_write else records
            return len(records)

        def write_manifest(settings: object, *, manifest_sha256: str, **kwargs: object) -> None:
            self.manifest = manifest_sha256

        monkeypatch.setattr(store, "read_manifest", read_manifest)
        monkeypatch.setattr(store, "load_records", lambda settings, role="eval": self.records)
        monkeypatch.setattr(store, "write_registry", write_registry)
        monkeypatch.setattr(store, "write_manifest", write_manifest)


@pytest.fixture
def cli(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    from typer.testing import CliRunner

    from cascade.cli import app
    from cascade.config import Settings

    salt = Settings().study.salt
    runner = CliRunner()

    def invoke(registry_: FakeRegistry, *args: str):  # type: ignore[no-untyped-def]
        registry_.install(monkeypatch, salt=salt)
        return runner.invoke(app, ["ledger", *args])

    return invoke, salt


def test_export_then_restore_into_an_empty_database_lands_on_the_same_seal(cli, tmp_path) -> None:  # type: ignore[no-untyped-def]
    invoke, _ = cli
    records, manifest = sealed()
    target = tmp_path / "backup" / "registry.json"

    exported = invoke(FakeRegistry(records, manifest), "export", "--to", str(target))
    assert exported.exit_code == 0, exported.output
    assert (target.stat().st_mode & 0o777) == 0o600  # it holds the labels

    empty = FakeRegistry()
    restored = invoke(empty, "restore", "--from", str(target))
    assert restored.exit_code == 0, restored.output
    assert empty.manifest == manifest
    assert compute_manifest(empty.records) == manifest


def test_export_refuses_a_destination_inside_the_repository(cli) -> None:  # type: ignore[no-untyped-def]
    from cascade.config import repo_root

    invoke, _ = cli
    records, manifest = sealed()
    inside = repo_root() / "reports" / "registry.json"
    result = invoke(FakeRegistry(records, manifest), "export", "--to", str(inside))
    assert result.exit_code == 3
    assert "inside the repository" in " ".join(result.output.split())
    assert not inside.exists()


def test_export_refuses_an_unsealed_registry(cli, tmp_path) -> None:  # type: ignore[no-untyped-def]
    invoke, _ = cli
    records, _manifest = sealed()
    result = invoke(FakeRegistry(records, None), "export", "--to", str(tmp_path / "r.json"))
    assert result.exit_code == 3 and "not sealed" in result.output


def test_restore_refuses_an_archive_sealed_under_another_salt(cli, tmp_path) -> None:  # type: ignore[no-untyped-def]
    invoke, _ = cli
    records, manifest = sealed()
    path = tmp_path / "r.json"
    path.write_text(dump_archive(records, manifest_sha256=manifest, study_salt="another-study"))
    empty = FakeRegistry()
    result = invoke(empty, "restore", "--from", str(path))
    assert result.exit_code == 3 and "salt" in result.output
    assert empty.records == ()  # nothing was written


def test_restore_will_not_overwrite_a_loaded_registry_without_replace(cli, tmp_path) -> None:  # type: ignore[no-untyped-def]
    invoke, salt = cli
    records, manifest = sealed()
    path = tmp_path / "r.json"
    path.write_text(dump_archive(records, manifest_sha256=manifest, study_salt=salt))
    result = invoke(FakeRegistry(registry(4), "x" * 64), "restore", "--from", str(path))
    assert result.exit_code == 3 and "--replace" in result.output


def test_restore_rechecks_the_database_and_exits_3_if_it_landed_wrong(cli, tmp_path) -> None:  # type: ignore[no-untyped-def]
    invoke, salt = cli
    records, manifest = sealed()
    path = tmp_path / "r.json"
    path.write_text(dump_archive(records, manifest_sha256=manifest, study_salt=salt))
    lossy = FakeRegistry()
    lossy.corrupt_on_write = True
    result = invoke(lossy, "restore", "--from", str(path))
    assert result.exit_code == 3 and "do not reseal" in " ".join(result.output.split())
