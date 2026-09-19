"""M11's infrastructure invariants, checked statically (ADR-0033, ADR-0034).

The Terraform has its own offline tests (``terraform test`` with mock
providers, run by ``make infra-check`` and CI's infra job). These are the rules
that must hold however the configuration grows, so they are checked by text in
the ordinary test suite, where no Terraform binary and no AWS account are
needed -- the same way ``test_invariants.py`` guards the Python.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TERRAFORM = REPO_ROOT / "infra" / "terraform"
DOCKERFILES = sorted((REPO_ROOT / "infra" / "docker").glob("*Dockerfile"))


def terraform_sources() -> list[Path]:
    return sorted(p for p in TERRAFORM.rglob("*.tf") if ".terraform" not in p.parts)


def code_lines(path: Path) -> list[tuple[int, str]]:
    """Lines with comments removed, so prose about a rule cannot trip it."""
    out: list[tuple[int, str]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = re.sub(r"\s*(#|//).*$", "", line)
        if stripped.strip():
            out.append((number, stripped))
    return out


def rel(path: Path) -> str:
    return str(path.relative_to(REPO_ROOT))


def test_the_terraform_tree_is_where_we_think_it_is() -> None:
    """Guard the guard: an empty glob would make every check below vacuous."""
    assert len(terraform_sources()) >= 15
    assert DOCKERFILES


# Resource types that would give the VPC a path to the internet. The design
# (ADR-0034) reaches every AWS service through an endpoint, so the bench
# measures the database and never the network.
EGRESS_TYPES = (
    "aws_internet_gateway",
    "aws_egress_only_internet_gateway",
    "aws_nat_gateway",
    "aws_eip",
)


def test_the_vpc_has_no_path_to_the_internet() -> None:
    found = [
        f"{rel(path)}:{number}: {line.strip()}"
        for path in terraform_sources()
        for number, line in code_lines(path)
        if re.search(rf'resource\s+"({"|".join(EGRESS_TYPES)})"', line)
    ]
    assert not found, "internet egress is not part of this design (ADR-0034):\n" + "\n".join(found)


@pytest.mark.parametrize(
    "pattern",
    [
        r"0\.0\.0\.0/0",
        r"::/0",
        r"publicly_accessible\s*=\s*true",
        r"map_public_ip_on_launch\s*=\s*true",
        r"assignPublicIp=ENABLED",
    ],
)
def test_nothing_is_open_or_public(pattern: str) -> None:
    found = [
        f"{rel(path)}:{number}: {line.strip()}"
        for path in terraform_sources()
        for number, line in code_lines(path)
        if re.search(pattern, line)
    ]
    assert not found, f"{pattern!r} found:\n" + "\n".join(found)


def test_the_region_is_never_defaulted() -> None:
    """ADR-0028: routing is explicit. Invariant 1's rule, applied to where spend lands."""
    offenders = []
    for path in terraform_sources():
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r'variable\s+"region"\s*\{', text):
            depth, index = 1, match.end()
            while depth and index < len(text):
                depth += {"{": 1, "}": -1}.get(text[index], 0)
                index += 1
            body = text[match.end() : index]
            if re.search(r"^\s*default\s*=", body, flags=re.MULTILINE):
                offenders.append(rel(path))
    assert not offenders, f"a region variable carries a default: {offenders}"


# Inputs that are decisions or unmeasured quantities. Each is required on
# purpose, and `terraform test` cannot protect that: omitting a required
# variable is a run error, not something `expect_failures` can target, so a
# default slipped in later would pass every Terraform test. Found by mutation
# at M12 -- the mutant that added one survived.
NEVER_DEFAULTED = {
    "region": "where spend lands is a decision (ADR-0028)",
    "replica_region": "as region",
    "allowed_regions": "as region",
    "infrastructure_allowance_usd": "no AWS cost has been measured; a default would be an invented number",
    "freeable_memory_low_bytes": "depends on a measurement nobody has made",
    "database_connections_high": "depends on a measurement nobody has made",
    "apply_policy_arns": "what a pipeline may change is a decision; the easy default is admin",
    "github_repository": "who may deploy is a decision",
}


def _variable_bodies(text: str) -> list[tuple[str, str]]:
    found = []
    for match in re.finditer(r'variable\s+"([^"]+)"\s*\{', text):
        depth, index = 1, match.end()
        while depth and index < len(text):
            depth += {"{": 1, "}": -1}.get(text[index], 0)
            index += 1
        found.append((match.group(1), text[match.end() : index]))
    return found


def test_decisions_and_unmeasured_thresholds_are_never_defaulted() -> None:
    offenders, seen = [], set()
    for path in terraform_sources():
        for name, body in _variable_bodies(path.read_text(encoding="utf-8")):
            if name not in NEVER_DEFAULTED:
                continue
            seen.add(name)
            # Top-level `default =` only: an object type's optional() defaults
            # and nested blocks are indented deeper than two spaces.
            if re.search(r"^  default\s*=", body, flags=re.MULTILINE):
                offenders.append(f"{rel(path)}: variable {name!r} -- {NEVER_DEFAULTED[name]}")
    assert not offenders, "a required input has been given a default:\n" + "\n".join(offenders)
    # Guard the guard: a renamed variable would make its entry vacuous.
    assert seen == set(
        NEVER_DEFAULTED
    ), f"never declared anywhere: {sorted(set(NEVER_DEFAULTED) - seen)}"


def test_every_checkov_skip_says_why() -> None:
    """A suppression without a reason is indistinguishable from an oversight."""
    bad = []
    for path in terraform_sources():
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            match = re.search(r"#checkov:skip=([A-Z0-9_]+)(?::(.*))?$", line)
            if match and len((match.group(2) or "").strip()) < 25:
                bad.append(f"{rel(path)}:{number}: {line.strip()}")
    assert not bad, "every checkov skip needs a written justification:\n" + "\n".join(bad)


def test_every_checkov_skip_detector_actually_detects() -> None:
    """Guard the guard: the pattern above must match the form used in the tree."""
    sample = "  #checkov:skip=CKV_AWS_18:too short"
    match = re.search(r"#checkov:skip=([A-Z0-9_]+)(?::(.*))?$", sample)
    assert match is not None and len((match.group(2) or "").strip()) < 25


@pytest.mark.parametrize("dockerfile", DOCKERFILES, ids=lambda p: p.name)
def test_base_images_are_pinned_by_digest(dockerfile: Path) -> None:
    """A tag moves; a digest names one image forever -- the image tag in ECR is immutable for the same reason."""
    unpinned = [
        f"{rel(dockerfile)}:{number}: {line.strip()}"
        for number, line in enumerate(dockerfile.read_text(encoding="utf-8").splitlines(), start=1)
        if line.strip().upper().startswith("FROM ") and "@sha256:" not in line
    ]
    assert not unpinned, "base images must be pinned by digest:\n" + "\n".join(unpinned)
