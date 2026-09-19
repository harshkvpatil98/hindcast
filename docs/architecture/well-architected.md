# Well-Architected review

A review against the six pillars of the AWS Well-Architected Framework, of the
design as it stands at M12. A review with no findings is not a review, so each
pillar states what holds, **what does not**, and what would close the gap.
Nothing here has run on AWS; "holds" means *designed and gated offline*.

## Operational excellence

**Holds.** Everything is code: Terraform with pinned, locked providers; four
offline gates in CI (`make infra-check`); every suppression justified in place
and checked by a test. Each phase is a resumable CLI command with distinct exit
codes, so a supervisor can tell "budget breached" (2) from "precondition failed"
(3) from a bug (1). Decisions and their evidence are recorded as ADRs.

**Does not.**
- No orchestration. Phases are started by hand; the Step Functions state
  machines for ingest and the simulation fan-out are designed in the plan and
  **not written**.
- No dashboards or alarms beyond cost. Task logs reach CloudWatch, but nothing
  watches them.
- No deployment pipeline: `terraform apply` is a person at a terminal.

**To close.** Step Functions for the two fan-out phases; a CloudWatch dashboard
and alarms on task failure and on Aurora capacity; OIDC-federated `plan` on
pull requests and `apply` on merge.

## Security

**Holds.** No internet path in the VPC; encryption at rest with CMKs and in
transit with verified TLS; least-privilege roles with explicit denies where
they matter (the lake writer); no long-lived model credential on the AWS paths;
secrets off command lines and out of state where RDS can manage them; SCPs for
region, audit trail, encryption and root. See the [threat model](threat-model.md).

**Does not.**
- **Prompt injection through the evidence corpus is unmeasured** (threat T3).
  The blast radius is bounded by the action schema and the deterministic
  arbiter, but no probe tests it.
- SCPs are written and tested, **not attached**: that needs an organization.
- No GuardDuty, Security Hub or CloudTrail in the Terraform yet — the SCP
  protects an audit trail this code does not create.
- Role passwords exist in Terraform state until the IAM switch is made.

**To close.** An adversarial-document probe alongside the poison-pill probe;
an `audit` module (CloudTrail to a locked bucket, GuardDuty, Config); attach
the SCPs from a management account.

## Reliability

**Holds.** Every phase resumes from its last completed unit, and the ingest's
stop rules fire only *between* units, so an interruption never leaves partial
state. Replay is byte-identical (25/25, M8), which makes recomputation a valid
recovery path. Aurora gives point-in-time recovery; experiments run on
copy-on-write clones. Recovery tiers are set by what each dataset costs to lose
([DR runbook](dr-runbook.md)).

**Does not.**
- **No restore has ever been exercised.** Every RTO is a target.
- The tier-0 export (sealed registry, source cache, LLM cache) is **not
  built** — and its absence is exactly what made this project's one real
  data-loss incident permanent.
- Single-AZ writer, single region. Acceptable for a batch study; stated, not
  hidden.

**To close.** Build the tier-0 export into `ledger seal` and the end of each
recorded phase; run one restore drill per tier and replace the targets with
measurements.

## Performance efficiency

**Holds.** The one criterion still missed on real data — retrieval p95 90.92 ms
against 15 ms — has a diagnosis (a 7.75 GB container; 3.9 ms + 0.34 ms per
partition scanned) and an experiment designed to test it: fixed Aurora capacity
at two sizes, and annual partitioning on a clone. HNSW replaced IVFFlat on
measurement (ADR-0026). Graviton for the bench task.

**Does not.**
- **No Aurora number exists.** The design is a hypothesis until the bench runs.
- The ingest runs at ~36 chunks/s on one machine and is bound by single-threaded
  text processing; parallelising it across Batch jobs is designed, not built.
- Two memory settings were changed together during an incident, so their
  separate effects are unknown; the defaults were deliberately left alone.

**To close.** Run the M11 bench; measure the two ingest settings one at a time;
fan the ingest out per unit.

## Cost optimization

**Holds.** Per-phase ceilings that abort rather than warn; an account-level
budget derived from the same configuration; anomaly detection; a sandbox built
to be destroyed, with nothing meant to run between measurements; interface
endpoints kept to the four the task needs; batch inference where the provider
offers it, and a refusal to run a batched phase unbatched. No price is written
into the repository.

**Does not.**
- **No AWS cost has been measured**, so the infrastructure allowance has no
  basis yet — which is why it is a required input with no default.
- Marketplace billing for model spend defeats tag-based allocation; the
  dedicated account is a workaround, and per-phase model cost on AWS is visible
  only through the in-process meter.

**To close.** One measured sandbox cycle (create, bench, destroy) to set the
allowance from data; reconcile the meter against Cost Explorer at M8's gate.

## Sustainability

**Holds.** Compute exists only while a measurement runs. Serverless capacity is
fixed per run and released. Recorded model calls are never repeated: replay
serves them from disk, and identical requests inside a wave are submitted once.
Graviton for the task.

**Does not.** Nothing is measured here either, and the corpus is stored in full
precision in Postgres *and* would be dumped to S3 — two copies of the largest
dataset, kept for recovery time rather than need.

**To close.** Lifecycle the corpus dump to infrequent-access storage after the
study; drop it once the study is published and the pipeline can rebuild it.
