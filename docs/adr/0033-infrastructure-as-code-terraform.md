# ADR-0033 — Infrastructure as code: Terraform, pinned, gated offline

- **Status:** accepted — Terraform chosen by the project owner, 2026-09-18 (CLAUDE.md §3: an addition to the stack, recorded here)
- **Milestone:** M11
- **Records a stack addition and how it is kept honest without an AWS account**

## Context

M11 moves Chronofence to Aurora. The owner chose Terraform over CDK. Two
facts shaped how:

* **The machine's Terraform is 1.5.7** (2023). It predates `terraform test`
  (1.6) and mock providers (1.7), which are what let infrastructure be tested
  with no AWS account — the only condition this project has had so far.
  Upgrading the system binary could break the owner's other projects.
* **This repository gates everything.** Code that ships ungated is, by the
  project's own standard, unverified.

## Decision

1. **Pinned toolchain, run in Docker locally and pinned identically in CI**:
   Terraform **1.16.3**, the AWS provider **6.65.0** and random **3.9.1**
   (locked, with hashes for `darwin_arm64`, `linux_amd64` and `linux_arm64`),
   TFLint **0.64.0** with the AWS ruleset **0.48.0**, Checkov **3.3.19**. The
   system Terraform is never consulted. Base images are pinned by digest.
2. **Layout**: `infra/terraform/modules/{network,database,bench}`, composed by
   `envs/sandbox`; `envs/bootstrap` creates the encrypted state bucket, with
   native S3 locking (`use_lockfile`) instead of a DynamoDB table.
3. **Four gates, all offline** — `make infra-check`, and CI's `infra` job:
   * `fmt -check` and `validate` against the real provider schemas;
   * `terraform test` with **mock providers**: 17 runs asserting security
     properties directly, including that the pgvector gate *rejects* an engine
     that is too old and that minimum capacity above maximum is refused;
   * TFLint with the AWS ruleset;
   * Checkov.
   Plus `tests/unit/test_infra_invariants.py` in the ordinary pytest suite, for
   the rules that must hold however the configuration grows.
4. **Checkov findings are triaged, never blanket-suppressed.** Each skip sits
   on the resource it concerns, with its reason; the static test fails any
   skip without a written justification.

## Rationale

**Offline gating is the only honest gating available.** No AWS account exists
yet. Mock providers let the plan be asserted — encryption on, IAM auth on,
TLS forced, nothing public, pgvector pinned — before anything is created. What
they cannot show is that AWS accepts the configuration at apply time; that is
M11's live criterion, not a claim made here.

**The guards were shown to fire.** Four properties were broken on purpose —
storage unencrypted, the pgvector gate widened to 0.7.4, plaintext connections
allowed, a password dropped from the injected secrets — and each failed a
test; a planted internet gateway with an unjustified skip failed exactly the
two static tests meant to catch it.

**Checkov at first run: 260 passed, 18 failed.** Two were real and fixed: the
bench container's root filesystem is now read-only, with every write
redirected to a mounted scratch volume, and the state key carries an explicit
policy. Sixteen were false positives or deliberate choices, each justified
where it applies — for example `kms:*` for the account root in a key policy,
which is AWS's standard delegation to IAM, and minor upgrades off, which is the
pgvector pin (ADR-0034). After triage: **270 passed, 0 failed, 27 skipped**.

## Cost of being wrong

A mock-provider test cannot catch a configuration AWS rejects at apply, or a
quota or a regional gap. The first live `apply` is where those surface, and it
is recorded as a measurement when it happens.

## Verified by

`make infra-check` (exit 0) on 2026-09-19; `tests/unit/test_infra_invariants.py`
(11 tests); CI's `infra` job, which runs the same pinned commands.
