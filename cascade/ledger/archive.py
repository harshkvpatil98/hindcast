"""The tier-0 archive: the sealed registry as one verifiable file. Pure.

The sealed split is the one dataset this project cannot regenerate. Before M10
the database volume was lost and the registry with it; re-fetching the sources
produced a valid 180-scenario set, but a *different* one, because markets had
resolved in the meantime. Every number ever reported is keyed to a manifest
hash, so a registry that cannot be restored is a study that cannot be
continued (docs/architecture/dr-runbook.md).

An archive carries two independent checks, because the manifest hash alone is
not enough to trust a restore:

* ``manifest_sha256`` covers the four fields the seal covers -- id, cutoff,
  resolution time, outcome. It proves the *split* is the sealed one.
* ``content_sha256`` covers every field of every record. It proves the *file*
  is what was written: a reworded question leaves the manifest intact and
  changes what every agent is asked.

**An archive contains the resolution labels.** Invariant 2 is enforced in the
database by a grant; nothing enforces it on a file. The CLI therefore refuses
to write one inside the repository and creates it owner-readable only, and the
recovery design keeps it under a prefix the simulation's principal cannot read.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from cascade.canonical import canonical_json
from cascade.ledger.manifest import compute_manifest
from cascade.ledger.schema import ScenarioRecord

__all__ = ["ARCHIVE_FORMAT", "ArchiveCorrupt", "RegistryArchive", "dump_archive", "load_archive"]

ARCHIVE_FORMAT = 1


class ArchiveCorrupt(RuntimeError):
    """An archive failed one of its checks. Never restore from it."""


@dataclass(frozen=True)
class RegistryArchive:
    """A parsed, fully verified archive."""

    records: tuple[ScenarioRecord, ...]
    manifest_sha256: str
    study_salt: str


def _records_payload(records: tuple[ScenarioRecord, ...]) -> list[dict[str, Any]]:
    # Sorted (invariant 7): the digest must not depend on load order.
    return [
        record.model_dump(mode="json")
        for record in sorted(records, key=lambda item: item.scenario.scenario_id)
    ]


def _content_digest(payload: list[dict[str, Any]]) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def dump_archive(
    records: tuple[ScenarioRecord, ...], *, manifest_sha256: str, study_salt: str
) -> str:
    """Serialise a sealed registry. Refuses one that does not match its seal.

    Preserves the invariant that an archive is only ever made of a registry
    that verifies: exporting a drifted registry under the sealed hash would
    produce a file that fails at restore, which is when it is needed.
    """
    if not records:
        raise ValueError("refusing to archive an empty registry")
    actual = compute_manifest(records)
    if actual != manifest_sha256:
        raise ArchiveCorrupt(
            f"the loaded registry hashes to {actual[:16]}... but is sealed as "
            f"{manifest_sha256[:16]}...; find out what changed before archiving anything"
        )
    payload = _records_payload(records)
    return canonical_json(
        {
            "format": ARCHIVE_FORMAT,
            "manifest_sha256": manifest_sha256,
            "content_sha256": _content_digest(payload),
            "study_salt": study_salt,
            "n_scenarios": len(payload),
            "records": payload,
        }
    )


def load_archive(text: str) -> RegistryArchive:
    """Parse and verify an archive. Raises :class:`ArchiveCorrupt` on any doubt."""
    try:
        document = json.loads(text)
    except ValueError as exc:
        raise ArchiveCorrupt(f"not JSON: {exc}") from exc
    if not isinstance(document, dict) or document.get("format") != ARCHIVE_FORMAT:
        raise ArchiveCorrupt(
            f"unknown archive format {document.get('format') if isinstance(document, dict) else None!r}"
        )
    try:
        payload = document["records"]
        manifest = str(document["manifest_sha256"])
        content = str(document["content_sha256"])
        salt = str(document["study_salt"])
        records = tuple(ScenarioRecord.model_validate(item) for item in payload)
    except (KeyError, TypeError, ValidationError) as exc:
        raise ArchiveCorrupt(f"malformed archive: {type(exc).__name__}: {exc}") from exc

    if not records or len(records) != document.get("n_scenarios"):
        raise ArchiveCorrupt(
            f"the archive declares {document.get('n_scenarios')} scenarios and holds {len(records)}"
        )
    # Digest the parsed-and-redumped records, not the raw text, so the check
    # covers what will actually be written to the database.
    if _content_digest(_records_payload(records)) != content:
        raise ArchiveCorrupt("content digest mismatch: a record was altered after export")
    if compute_manifest(records) != manifest:
        raise ArchiveCorrupt("manifest mismatch: the archive's records are not the sealed split")
    return RegistryArchive(records=records, manifest_sha256=manifest, study_salt=salt)
