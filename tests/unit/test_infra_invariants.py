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
WORKFLOWS = sorted((REPO_ROOT / ".github" / "workflows").glob("*.yml"))

# The one place a path to the internet may be built (ADR-0042): an opt-in
# tier with its own subnets and its own route tables, instantiated by the
# sandbox root under a count that is zero unless asked for.
EGRESS_MODULE = TERRAFORM / "modules" / "egress"
SANDBOX_ROOT = TERRAFORM / "envs" / "sandbox"


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


# Resource types that give a VPC a path to the internet. The design (ADR-0034)
# reaches every AWS service through an endpoint, so the bench measures the
# database and never the network -- and the one exception (ADR-0042) is a
# separate module, opt-in, whose route tables the isolated tier never shares.
EGRESS_TYPES = (
    "aws_internet_gateway",
    "aws_egress_only_internet_gateway",
    "aws_nat_gateway",
    "aws_eip",
)

# What could give an EXISTING route table a way out: a route resource, or the
# arguments that name a gateway. Outside the egress module none may appear,
# so modules/network's table cannot be handed a default route by any change
# that stays in its own directory.
ROUTE_PATTERNS = (
    r'resource\s+"aws_route"',
    r"\bnat_gateway_id\s*=",
    r"\bgateway_id\s*=",
    r"\bcarrier_gateway_id\s*=",
    r"\btransit_gateway_id\s*=",
    r"\bnetwork_interface_id\s*=",
)


def outside_egress(path: Path) -> bool:
    return EGRESS_MODULE not in path.parents


def test_internet_egress_exists_only_in_the_egress_module() -> None:
    found = [
        f"{rel(path)}:{number}: {line.strip()}"
        for path in terraform_sources()
        if outside_egress(path)
        for number, line in code_lines(path)
        if re.search(rf'resource\s+"({"|".join(EGRESS_TYPES)})"', line)
    ]
    assert (
        not found
    ), "internet egress lives in modules/egress and nowhere else (ADR-0042):\n" + "\n".join(found)
    # Guard the guard: the exception must actually contain what it excuses.
    egress_text = "\n".join(
        line
        for path in terraform_sources()
        if not outside_egress(path)
        for _, line in code_lines(path)
    )
    assert re.search(
        r'resource\s+"aws_nat_gateway"', egress_text
    ), "modules/egress no longer builds a NAT gateway; retire the exception"


def test_no_route_table_outside_the_egress_module_can_be_given_a_way_out() -> None:
    found = [
        f"{rel(path)}:{number}: {line.strip()}"
        for path in terraform_sources()
        if outside_egress(path)
        for number, line in code_lines(path)
        if any(re.search(pattern, line) for pattern in ROUTE_PATTERNS)
    ]
    assert (
        not found
    ), "only modules/egress may create routes or name a gateway (ADR-0042):\n" + "\n".join(found)


# Open CIDRs are forbidden everywhere but the egress module's security group
# (a group cannot name a domain; DNS Firewall does the narrowing there).
@pytest.mark.parametrize(
    ("pattern", "allowed_in"),
    [
        (r"0\.0\.0\.0/0", EGRESS_MODULE),
        (r"::/0", None),
        (r"publicly_accessible\s*=\s*true", None),
        (r"map_public_ip_on_launch\s*=\s*true", None),
        (r"assignPublicIp=ENABLED", None),
        (r"AssignPublicIp\s*=\s*\"ENABLED\"", None),
    ],
)
def test_nothing_is_open_or_public(pattern: str, allowed_in: Path | None) -> None:
    found = [
        f"{rel(path)}:{number}: {line.strip()}"
        for path in terraform_sources()
        if allowed_in is None or allowed_in not in path.parents
        for number, line in code_lines(path)
        if re.search(pattern, line)
    ]
    assert not found, f"{pattern!r} found:\n" + "\n".join(found)


def _block_body(text: str, header: str) -> str:
    """The body of the first HCL block whose header line matches ``header``."""
    match = re.search(header, text)
    assert match is not None, f"no block matching {header!r}"
    depth, index = 1, text.index("{", match.start()) + 1
    start = index
    while depth and index < len(text):
        depth += {"{": 1, "}": -1}.get(text[index], 0)
        index += 1
    return text[start : index - 1]


@pytest.mark.parametrize("opt_in", ["egress", "study"])
def test_the_way_out_and_the_model_are_opt_in_and_off_by_default(opt_in: str) -> None:
    """The default sandbox is ADR-0034's isolated VPC; a way out, and a task that can call a model, are asked for."""
    variables = (SANDBOX_ROOT / "variables.tf").read_text(encoding="utf-8")
    body = _block_body(variables, rf'variable\s+"{opt_in}"\s*\{{')
    assert re.search(
        r"^  default\s*=\s*null\s*$", body, flags=re.MULTILINE
    ), f"variable {opt_in!r} must default to null"
    main = (SANDBOX_ROOT / "main.tf").read_text(encoding="utf-8")
    modules = {"egress": ["egress"], "study": ["study", "cache"]}[opt_in]
    for module in modules:
        body = _block_body(main, rf'module\s+"{module}"\s*\{{')
        assert re.search(
            rf"^\s*count\s*=\s*var\.{opt_in}\s*==\s*null\s*\?\s*0\s*:\s*1\s*$",
            body,
            flags=re.MULTILINE,
        ), f"module {module!r} must exist only when var.{opt_in} is given"


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
    # ADR-0042
    "model_provider": "where the study's largest line item is billed, and which IAM service authorizes it",
    "allowed_domains": "what the study may reach on the internet is reviewed by reading the list",
    "dns_firewall_action": "whether an unlisted name is refused or merely logged is a security posture, chosen knowingly at first apply",
    "sync_schedule_expression": "the RPO for the LLM cache against a per-run cost that grows with it; neither is measured",
    "guardduty_min_severity": "what severity pages a human is a decision, not a constant",
    # ADR-0046
    "report_lock_retention_days": (
        "how long a published claim cannot be withdrawn is a decision about the record, "
        "and the operational cost of getting it wrong is paid for that whole period"
    ),
    "inventory_schedule": (
        "how often the tier-0 archive is listed prices noticing an omission against a "
        "per-run charge that grows with the archive; neither has been measured"
    ),
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


def test_a_never_defaulted_input_is_not_defaulted_through_an_object_attribute() -> None:
    """The root wraps some of these in opt-in objects; `optional(string, "x")` there is the same default by another spelling."""
    offenders = [
        f"{rel(path)}:{number}: {line.strip()}"
        for path in terraform_sources()
        for number, line in code_lines(path)
        for name in NEVER_DEFAULTED
        if re.search(rf"^\s+{name}\s*=\s*optional\(", line)
    ]
    assert not offenders, "a required decision is defaulted inside an object type:\n" + "\n".join(
        offenders
    )


# ADR-0046 rejected serving the study's report directory as a static site. The
# rejection is a security property, not a preference: every report carries
# `baselines.csv` and `ablation_grid.csv`, which are one row per scenario with
# an `outcome` column, and §1.3's frozen split and §4.4's memorisation probe
# both rest on the labels not being crawlable. A distribution is a standing
# public endpoint; a presigned URL made by an assumed role dies with the
# session. Nothing here may grow one by accident, in any module.
CDN_PATTERNS = (
    r'resource\s+"aws_cloudfront_',
    r"cloudfront\.amazonaws\.com",
    r'resource\s+"aws_s3_bucket_website_configuration"',
)


@pytest.mark.parametrize("pattern", CDN_PATTERNS)
def test_no_report_is_served_from_a_public_endpoint(pattern: str) -> None:
    found = [
        f"{rel(path)}:{number}: {line.strip()}"
        for path in terraform_sources()
        for number, line in code_lines(path)
        if re.search(pattern, line)
    ]
    assert not found, (
        "a study report carries the resolution labels; it is fetched by a named "
        "principal, never served (ADR-0046):\n" + "\n".join(found)
    )


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


# --- Workflows -------------------------------------------------------------------------------

# Third-party actions used before ADR-0042, on moving tags. Reported rather
# than rewritten: ci.yml holds no cloud credential, and re-pinning it is the
# owner's call. The list is a ratchet -- an entry that no longer matches
# fails the test, so it shrinks when they are pinned and cannot grow.
KNOWN_UNPINNED = {
    "ci.yml": {
        "astral-sh/setup-uv@v5",
        "hashicorp/setup-terraform@v4",
        "terraform-linters/setup-tflint@v6",
    },
}


def workflow_uses(path: Path) -> list[tuple[int, str]]:
    return [
        (number, match.group(1).strip().strip("\"'"))
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if (match := re.match(r"\s*-?\s*uses:\s*(\S+)", line))
    ]


def test_the_workflow_tree_is_where_we_think_it_is() -> None:
    assert any(path.name == "infra-plan.yml" for path in WORKFLOWS)
    assert any(path.name == "ci.yml" for path in WORKFLOWS)


def test_third_party_actions_are_pinned_to_a_commit_sha() -> None:
    """A tag can be re-pointed by whoever holds the action's repository; a SHA names one tree forever."""
    unpinned, ratchet_seen = [], set()
    for path in WORKFLOWS:
        for number, ref in workflow_uses(path):
            if ref.startswith("./") or ref.startswith("actions/"):
                continue  # local, or GitHub's own first-party actions
            if re.search(r"@[0-9a-f]{40}$", ref):
                continue
            if ref in KNOWN_UNPINNED.get(path.name, set()):
                ratchet_seen.add((path.name, ref))
                continue
            unpinned.append(f"{path.name}:{number}: {ref}")
    assert not unpinned, "third-party actions must be pinned to a 40-hex commit SHA:\n" + "\n".join(
        unpinned
    )
    expected = {(name, ref) for name, refs in KNOWN_UNPINNED.items() for ref in refs}
    assert ratchet_seen == expected, (
        "KNOWN_UNPINNED no longer matches the tree; remove the entries that were pinned: "
        f"{sorted(expected - ratchet_seen)}"
    )


def test_the_plan_workflow_is_read_only_gated_and_minimal() -> None:
    """ADR-0035: apply is a person. ADR-0042: the plan job is inert without an account and holds the least it can."""
    import yaml

    path = REPO_ROOT / ".github" / "workflows" / "infra-plan.yml"
    text = path.read_text(encoding="utf-8")
    workflow = yaml.safe_load(text)
    assert (
        workflow.get("permissions") == {}
    ), "workflow-level permissions must be empty; each job states its own"
    jobs = workflow["jobs"]
    assert set(jobs) == {"plan"}, "one job: plan. An apply job is a decision for an ADR."
    plan = jobs["plan"]
    assert plan["permissions"] == {"id-token": "write", "contents": "read"}
    assert "vars.AWS_PLAN_ROLE_ARN != ''" in str(
        plan["if"]
    ), "the job must be skipped, not red, until the role variable is set"
    steps = "\n".join(str(step.get("run", "")) for step in plan["steps"])
    assert "terraform plan" in steps
    assert not re.search(r"terraform\s+apply", text), "no apply, anywhere in this file"
    assert "-lock=false" in steps, "the plan role cannot write the state lock"
    assert not any(
        "upload-artifact" in ref for _, ref in workflow_uses(path)
    ), "a plan file is never uploaded"
    for step in plan["steps"]:
        if "aws-actions/configure-aws-credentials" in str(step.get("uses", "")):
            assert (
                "role-to-assume" in step["with"] and "aws-access-key-id" not in step["with"]
            ), "OIDC, never a stored key"
            break
    else:
        raise AssertionError("the plan job must assume the OIDC plan role")
