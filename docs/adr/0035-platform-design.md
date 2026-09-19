# ADR-0035 — The platform around the study: one account, derived budgets, a locked event lake

- **Status:** accepted as a design — gated offline, not applied
- **Milestone:** M12
- **Records the platform decisions, and where the design stops**

## Context

M11 gave the study a data plane. A platform also has to answer: who may spend
what, what nobody may do, and where the study's permanent record lives. The
project already had answers to each as *local* mechanisms — a cost meter with
phase ceilings, Postgres grants, an append-only event table. M12's rule was to
map those onto AWS rather than invent parallel ones.

## Decision

1. **One dedicated account for the study.** Model spend through Claude
   Platform on AWS is billed via AWS Marketplace, which cost-allocation tags do
   not reach. A tag-filtered budget would silently omit the largest line item;
   an account boundary cannot.
2. **The budget is derived, not declared.** `modules/governance` reads
   `budget.phase_ceiling_usd` from `configs/base.yaml` with `yamldecode` and
   adds a required infrastructure allowance. The meter aborts from inside the
   process; the budget alarms from outside it; both read one file. The
   allowance has **no default**, because no AWS cost has been measured and an
   invented number would be a target nobody checked.
3. **Guardrails as SCPs, bound to nothing by default.** Allowed regions; the
   audit trail cannot be stopped; databases cannot be created unencrypted; the
   account's public-access block cannot be lifted; no root user; no leaving the
   organization. With no targets the policies are created and attached to
   nothing, so they can be reviewed before they can lock anyone out.
4. **The event lake makes invariant 6 two controls.** S3 Object Lock, and a
   writer role with an explicit Deny on delete and on every lock override.
   GOVERNANCE mode by default; COMPLIANCE must be chosen, because nobody can
   undo it. Athena's workgroup enforces encryption and a per-query scan limit
   over any client setting. **Labels are never exported**, so invariant 2 needs
   no rule here.
5. **Recovery tiers follow cost-to-lose, not size** — set by this project's own
   incident, in which the smallest dataset (the sealed registry and the source
   cache that reproduces it) was the only one that could not be rebuilt. See
   `docs/architecture/dr-runbook.md`.

## What mutation testing changed

A test asserted that the study ceiling equals the sum of the phase ceilings. A
hardcoded `330` — today's sum — **passed**. The assertion could not tell
"derived" from "restated". The test now points the module at a fixture with
different ceilings; the mutant fails. The same pass caught a writer role without
its delete Deny, Object Lock switched off, and an SCP statement that allowed.

## Where the design stops

Written after this record was first accepted, by a team of agents working in
separate worktrees and integrated here: `modules/audit`, `modules/pipeline`,
`modules/observability`, `modules/cicd`, `modules/recovery` and the
`cascade ledger export`/`restore` commands. Two briefs were wrong and the
agents were right to depart from them: AWS Config cannot deliver to a bucket
with a default Object Lock retention (so Config has its own bucket), and with
capacity pinned for measurement the two Serverless capacity alarms would fire
for the cluster's whole life (so the module watches CPU against that capacity
instead). Observability and the pipeline live in `envs/sandbox`, beside what
they watch and run: a metric filter cannot attach to a log group that does not
exist, and the platform root must plan while the sandbox is destroyed.

Still not written: an egress or fetch path that would let the ingest chain
actually run inside the isolated VPC; a study task with model access and
durable cache storage; any workflow that uses the OIDC roles; routing of
GuardDuty findings; syncing the two caches to the recovery bucket; an
adversarial-document probe. Not possible without an account or an
organization: applying anything, attaching the SCPs, and every AWS number. The
[Well-Architected review](../architecture/well-architected.md) lists each gap
under its pillar.

## Verified by

`terraform test` (73 runs in `envs/platform`, 21 in `envs/sandbox`, mock
providers), TFLint, Checkov (742 passed, 0 failed, 61 justified skips across
all roots), and the static invariants in the pytest suite -- which now also
fail if a required decision input (a region, a budget allowance, an unmeasured
threshold) is ever given a default, a gap `terraform test` cannot cover. One Checkov finding was real and fixed rather
than skipped: the analyst role's Glue permissions were granted on `*` and are
now scoped to the catalog, the database and the one table.
