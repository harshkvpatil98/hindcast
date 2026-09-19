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

Two Step Functions chains run the ingest and the study as Fargate tasks
(`modules/pipeline`); every task failure is routed to an alert and a Fail
state, so a failed chain can never read as finished. Alerts keep the process
exit code, because here a 2 (budget ceiling) and a 1 (a bug) are different
events (`modules/observability`). Deployment identity is GitHub OIDC, with
`apply` trusted only from a reviewed environment (`modules/cicd`).

**Does not.**
- **Neither chain can run to completion today**, and the module says so: the
  ingest fetches from the public internet and the sandbox VPC has no egress by
  design; the study's task is pinned to `replay` and its role cannot call a
  model; and the LLM cache and report files live on task scratch that dies with
  the task. The chains are the right shape for the code as it is -- serial,
  because the ingest has no unit-claiming and the fan-out holds its wavefront
  in one process -- not yet a working deployment.
- The pipeline retries a task that *failed* only for `corpus build`, because a
  retried phase used to be re-granted its whole budget ceiling (fixed in the
  cost meter at M12; the wider retry is now safe and not yet enabled).
- The OIDC roles exist; **no workflow uses them**. `terraform apply` is still a
  person at a terminal.
- Two alarm thresholds (free memory, connections) have no value yet: they
  depend on a bench that has not run.

**To close.** Give the ingest an egress path or a fetch stage outside the VPC;
a study task definition with model access and durable cache storage; a `plan`
workflow on pull requests; set the two thresholds from the first bench.

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
- GuardDuty findings alert nobody yet, and there is no Security Hub. (The
  audit trail itself now exists: `modules/audit` -- CloudTrail to a locked
  bucket with data events for the lake and recovery buckets, GuardDuty, Config.)
- Role passwords exist in Terraform state until the IAM switch is made.

**To close.** An adversarial-document probe alongside the poison-pill probe;
route GuardDuty findings to the alerts topic; attach the SCPs from a management
account.

## Reliability

**Holds.** Every phase resumes from its last completed unit, and the ingest's
stop rules fire only *between* units, so an interruption never leaves partial
state. Replay is byte-identical (25/25, M8), which makes recomputation a valid
recovery path. Aurora gives point-in-time recovery; experiments run on
copy-on-write clones. Recovery tiers are set by what each dataset costs to lose
([DR runbook](dr-runbook.md)).

**Does not.**
- **No restore has ever been exercised.** Every RTO is a target.
- The tier-0 export exists for the registry (`cascade ledger export` /
  `restore`, and a replicated, locked bucket) but **not for the two caches**,
  and nothing schedules it. Its absence is exactly what made this project's one
  real data-loss incident permanent.
- Single-AZ writer, single region. Acceptable for a batch study; stated, not
  hidden.

**To close.** Sync the source and LLM caches to the recovery bucket at the end
of each recorded phase; run one restore drill per tier and replace the targets with
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
