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

Since ADR-0042 the chains have what they lacked, each as an opt-in the root
refuses to default: an egress tier for the one ingest state that fetches; a
study task definition with model access, per provider, and the LLM cache on a
file system that outlives the task; and a `plan` workflow on pull requests
through the OIDC plan role, inert until an account exists and never an apply.

**Does not.**
- **Neither chain has run.** The wiring is complete and gated offline; the
  first execution will find what mocks cannot: whether the regional DNS
  allow-list is complete (run in ALERT first), whether DataSync accepts the
  locked bucket, and what the provider's price table must hold (it ships
  empty, and the task exits 3 until it is filled from the live page).
- The pipeline retries a task that *failed* only for `corpus build`, because a
  retried phase used to be re-granted its whole budget ceiling (fixed in the
  cost meter at M12; the wider retry is now safe and not yet enabled).
- `terraform apply` is still a person at a terminal — by decision (ADR-0035),
  and the plan workflow's test asserts it stays that way.
- Two alarm thresholds (free memory, connections) have no value yet: they
  depend on a bench that has not run.
- What `cascade report` writes still dies with the task.

**To close.** One ingest execution in ALERT mode, then BLOCK; set the two
thresholds from the first bench; an artifact for the report.

## Security

**Holds.** No internet path in the VPC; encryption at rest with CMKs and in
transit with verified TLS; least-privilege roles with explicit denies where
they matter (the lake writer); no long-lived model credential on the AWS paths;
secrets off command lines and out of state where RDS can manage them; SCPs for
region, audit trail, encryption and root. See the [threat model](threat-model.md).

GuardDuty findings now reach the alerts topic above a severity the operator
chooses (no default), through a rule the topic admits by ARN, with an alarm
for the finding that could not be delivered. The egress tier is the one
designed exception to "no internet path": opt-in, one state, one AZ, TCP 443,
a DNS allow-list that fails closed, and a static test that keeps every
gateway, route and open CIDR inside `modules/egress`. Every third-party
action in the credentialed workflow is pinned by SHA and tested for it.

**Does not.**
- **Prompt injection through the evidence corpus is unmeasured** (threat T3).
  The blast radius is bounded by the action schema and the deterministic
  arbiter, but no probe tests it.
- **The egress tier is a lookup control, not a packet control** (T13): a
  compromised ingest task that already holds an address can reach it. The
  ingest still runs as the database admin; a `cascade_ingest` role without
  the labels grant is the control that would close it, and is code.
- SCPs are written and tested, **not attached**: that needs an organization.
- There is no Security Hub.
- `ci.yml` uses moving tags for three third-party actions (reported, not
  rewritten: it holds no cloud credential; the invariants test carries them
  as a ratchet).
- Role passwords exist in Terraform state until the IAM switch is made.

**To close.** An adversarial-document probe alongside the poison-pill probe;
a `cascade_ingest` Postgres role; attach the SCPs from a management account;
pin `ci.yml`'s actions.

## Reliability

**Holds.** Every phase resumes from its last completed unit, and the ingest's
stop rules fire only *between* units, so an interruption never leaves partial
state. Replay is byte-identical (25/25, M8), which makes recomputation a valid
recovery path. Aurora gives point-in-time recovery; experiments run on
copy-on-write clones. Recovery tiers are set by what each dataset costs to lose
([DR runbook](dr-runbook.md)).

The LLM cache is on EFS, so a task stopped at its timeout — the design's own
stop rule — loses one call, not a wave; a scheduled DataSync task copies it,
add-only, into the locked and replicated recovery bucket; the source cache
has a prefix, an add-only upload policy and a procedure ([DR
runbook](dr-runbook.md)).

**Does not.**
- **No restore has ever been exercised.** Every RTO is a target; the LLM
  cache's is unmeasured and bound by object count.
- The source cache's upload is a person, not a schedule.
- The egress tier is one AZ: an AZ outage stops the ingest until it is
  re-run, which it survives by resuming.
- Single-AZ writer, single region. Acceptable for a batch study; stated, not
  hidden.

**To close.** Run one restore drill per tier and replace the targets with
measurements; a count check between the file system and the archive.

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

The egress decision was made on cost-of-mistake: a NAT gateway forgotten for
a month is about $33; a Network Firewall endpoint forgotten for a month is
about $288, most of the study's $330 budget (ADR-0042, with what could and
could not be verified). The DataSync task is pinned to Basic mode because
Enhanced bills per execution; the cache's copy schedule and the DNS
firewall's domain list are inputs with no default because each prices a
decision.

**Does not.**
- **No AWS cost has been measured**, so the infrastructure allowance has no
  basis yet — which is why it is a required input with no default.
- The DataSync schedule's per-run S3 request cost scales with the cache
  (hundreds of thousands of entries at study scale) and is unmeasured.
- Marketplace billing for model spend defeats tag-based allocation; the
  dedicated account is a workaround, and per-phase model cost on AWS is visible
  only through the in-process meter.
- Reading CC-NEWS through the S3 gateway endpoint instead of the NAT would
  save about $6 per full crawl in us-east-1 and needs a code change.

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
