# Cascade infrastructure (M11–M17)

Aurora PostgreSQL 16 + pgvector 0.8.0 in an isolated VPC, with the retrieval
bench running as a Fargate task beside it. Built to be **created, measured and
destroyed**. Everything regional is **us-west-2**
([ADR-0053](../../docs/adr/0053-portfolio-completion-provider-interface-and-region.md));
the tier-0 replica is the one deliberate second region.
Design: [ADR-0034](../../docs/adr/0034-aurora-data-plane.md);
toolchain and gates: [ADR-0033](../../docs/adr/0033-infrastructure-as-code-terraform.md);
the platform: [ADR-0035](../../docs/adr/0035-platform-design.md); the way
out, model access and the caches:
[ADR-0042](../../docs/adr/0042-ingest-egress-model-access-and-durable-caches.md);
publishing the deliverable and proving the recovery path:
[ADR-0046](../../docs/adr/0046-publishing-and-recovery-drills.md).

```
envs/bootstrap   encrypted, versioned state bucket (local state, run once)
envs/platform    M12: cost governance + event lake + SCP guardrails (docs/architecture/)
envs/sandbox     network + database + bench, composed
modules/network  private subnets only, VPC endpoints, flow logs -- no internet path
modules/database Aurora 16.11 Serverless v2, KMS, TLS forced, IAM auth, role secrets
modules/bench    ECR, ECS/Fargate task, artifacts bucket, least-privilege roles
modules/governance  budget derived from configs/base.yaml, anomaly detection, alerts
modules/guardrails  service control policies (optional: they need an Organizations management account) and the Bedrock guardrail `cascade eval guardrails` audits against
modules/eventlake   Object-Locked event log, Glue catalog, Athena workgroup, writer/analyst roles
modules/recovery    tier-0 recovery bucket: locked, replicated cross-region, closed to the simulation; the restore-drill role and a scheduled archive inventory
modules/reports     where `cascade report` publishes: versioned, encrypted, Object-Locked, fetched by a named reader and never served
modules/audit       CloudTrail (locked bucket, data events for the lake and recovery buckets), GuardDuty, Config
modules/cicd        GitHub OIDC roles: read-only plan from pull requests, apply from a reviewed environment
modules/pipeline    Step Functions chains for the ingest and the study (in envs/sandbox, opt-in)
modules/observability  exit-code-preserving task alerts, Aurora alarms, dashboard (in envs/sandbox, opt-in)
modules/egress      the one way out: one NAT in one AZ, a DNS allow-list that fails closed (in envs/sandbox, opt-in)
modules/cache       the LLM cache on EFS, copied add-only into the recovery bucket by DataSync (in envs/sandbox, with `study`)
modules/study       the study task: the bench's container plus a model grant per provider and the cache mount (in envs/sandbox, opt-in)
```

**Destroying the platform root:** the Config and inventory buckets deny
`s3:DeleteObjectVersion` to every principal, and the trail, lake, recovery and
reports buckets are under Object Lock. `terraform destroy` cannot empty them;
that is the point. Remove the bucket policy and wait out (or, under GOVERNANCE,
explicitly bypass) the retention before deleting them by hand. The reports
bucket needs the extra step: its policy denies
`s3:BypassGovernanceRetention` to every principal as well, so the policy has
to go first and the trail records that it did (ADR-0046).

## What is verified, and what is not

| | Status |
|---|---|
| Configuration valid against provider schemas (AWS 6.65.0) | **verified offline** |
| Security properties (encryption, TLS, IAM auth, nothing public, pgvector pinned, fixed capacity, secrets never in plain env) | **verified offline** — 62 `terraform test` runs in `envs/sandbox`, 130 in `envs/platform`, against mock providers under the pinned Terraform 1.16.3 in Docker (M17 was the first time the guardrail runs executed; three needed `apply` rather than `plan`) |
| The isolated tier never routes out, even with the egress tier on; only `CorpusBuild` and (without PrivateLink) the three model-calling states leave; the study grant is three routes in one workspace; the cache is encrypted and copied add-only; findings are admitted by ARN | **verified offline** — `terraform test`, and each broken on purpose once (ADR-0042) |
| A study report is private, versioned and locked; the task that writes one cannot read one back; the restore role cannot write the archive it restores from; the archivist's key must name its seal | **verified offline** — `terraform test`, and each broken on purpose once (ADR-0046) |
| The pgvector gate rejects an engine that is too old | **verified offline** — the test expects 16.6 to fail |
| Every gateway, route and open CIDR lives in `modules/egress`; the two opt-ins default to null; no decision input has a default; no report is ever served from a public endpoint; every Checkov skip justified; images pinned by digest; every third-party action in every workflow pinned by SHA | **verified offline** — `tests/unit/test_infra_invariants.py` |
| TFLint (AWS ruleset 0.48.0), Checkov 3.3.19 | **clean** — 1,027 passed, 0 failed, 73 justified skips |
| AWS accepts the configuration at apply | **not verified** — this Terraform has not been applied end to end from this repository. The owner's account (us-west-2) holds CloudTrail, a budget and the `cascade-audit-guardrail`, created outside this tree. Still open in particular: that DataSync writes into the locked recovery bucket; that S3 Inventory delivers under the destination policy here; that the regional DNS allow-list is complete; the Claude Platform on AWS PrivateLink service name |
| Any restore actually works | **not verified** — no drill has been run. The procedure, its checks and the role it runs as are in [docs/architecture/dr-runbook.md](../../docs/architecture/dr-runbook.md) |
| The bench image builds and runs | **not verified** — linted, not built |
| Any Aurora latency or recall number | **does not exist** |

Run every offline gate with `make infra-check` (Docker required; no AWS
credentials needed).

**Where to run Terraform.** There is no root at the top of the repository:
the three roots are `infra/terraform/envs/bootstrap`, `envs/platform` and
`envs/sandbox`, and `terraform init` anywhere else finds an empty directory.
They need Terraform **>= 1.10** and are gated at **1.16.3**; an older binary
stops with "Unsupported Terraform Core version". Check `terraform version`
first.

## Runbook

Everything below needs an AWS account. Each step says what it costs to leave
running.

**1. State bucket** (once per account and region)
```bash
cd infra/terraform/envs/bootstrap
terraform init && terraform apply -var region=us-west-2
```

**1b. The platform root** (governance, audit, the event lake, the Bedrock
guardrail). `terraform.tfvars.example` carries us-west-2, the replica region
and every required decision; in a standalone account leave
`create_service_control_policies = false` -- the SCPs need an Organizations
management account and nothing else depends on them.
```bash
cd ../platform
cp backend.hcl.example backend.hcl                   # fill from bootstrap's outputs
cp terraform.tfvars.example terraform.tfvars
terraform init -backend-config=backend.hcl
terraform plan -out=platform.tfplan                  # read it before anything else
```
**The account's existing `cascade-audit-guardrail` stays outside Terraform by
default**: `model_guardrail` is null in the example, nothing about the
guardrail is planned, and its id and version go into `.env` as
`CASCADE_GUARDRAIL_ID` / `CASCADE_GUARDRAIL_VERSION=1`; `cascade aws check`
then confirms they resolve, without invoking anything. To manage it here
instead, fill `model_guardrail` to match the console exactly and import both
resources before the first plan -- the ids are comma-delimited:
```bash
terraform import 'module.guardrails.aws_bedrock_guardrail.model[0]' '<guardrail-id>,DRAFT'
terraform import 'module.guardrails.aws_bedrock_guardrail_version.model[0]' '<guardrail-arn>,1'
terraform plan      # must show no change to either; an update means the block does not match
terraform output guardrail_environment               # the two .env lines the audit reads
```

**Before the first platform apply, check what the account already has.** This
root creates a CloudTrail trail, a GuardDuty detector, an AWS Config recorder
and delivery channel, a budget (`cascade-study`) and a cost-anomaly monitor.
GuardDuty allows one detector and Config one recorder per region, and an
account has one AWS-services anomaly monitor, so an apply fails on any that
exist; a second trail is allowed and bills its own copy of management events.
Read-only checks:
```bash
aws guardduty list-detectors --region us-west-2
aws configservice describe-configuration-recorders --region us-west-2
aws ce get-anomaly-monitors
aws cloudtrail describe-trails --region us-west-2
aws budgets describe-budgets --account-id "$(aws sts get-caller-identity --query Account --output text)"
```
Three of the five have a switch in `terraform.tfvars`, each true by default
and each leaving what the account already has exactly as it is:

| the account already has | set | what goes with it |
|---|---|---|
| a multi-region trail | `create_audit_trail = false` | this root's locked trail bucket, log group and **S3 data events** on the lake, recovery, reports and Config buckets: give the existing trail those selectors, or they have no access record |
| a budget that is to stay the only one | `create_budget = false` | the limit derived from `configs/base.yaml` is still an output (`monthly_limit_usd`), just not enforced by a budget |
| an AWS-services anomaly monitor (`Default-Services-Monitor`) | `create_cost_anomaly_detection = false` | the subscription that sends anomalies to the alerts topic |

A GuardDuty detector or a Config recorder that already exists has no switch:
it is imported (`terraform import`) or the plan is not applied. Nothing here
deletes or changes what it did not create. `terraform output
account_level_services` says which of them this root made.

**1c. Or the portfolio profile: a minimal live deployment** (ADR-0054). The
defaults above are the platform as designed -- recovery in a second region,
Object Lock, GuardDuty, Config. A deployment that demonstrates the
architecture rather than operates the study needs none of them, and
`portfolio.tfvars` is that profile: every production tier is a switch, each
on by default, and the profile turns them off without removing a module.
```bash
cd infra/terraform/envs/platform
cp portfolio.tfvars terraform.tfvars                 # gitignored, read automatically
terraform init -backend-config=backend.hcl
terraform plan -out=platform.tfplan                  # 21 to add in the owner's account
terraform output deployment                          # after an apply: which tiers exist
```

| switch | default | the profile | what off leaves out |
|---|---|---|---|
| `enable_recovery` | true | false | `modules/recovery` whole: the replica bucket and its region's CMK, replication, S3 Inventory, the restore role |
| `enable_reports` | true | false | the reports bucket and its read policy |
| `enable_object_lock` | true | false | Object Lock on the lake and the reports bucket, and the reports bucket's Deny on removing versions |
| `enable_guardduty` | true | false | the detector, the findings rule, the delivery alarm |
| `enable_config` | true | false | the recorder, the delivery channel, its role and its history bucket |
| `disposable` | false | true | (on) a destroy may empty the lake and reports buckets; refused while the lock is on |

What it keeps is the platform CMK, the event lake -- the events bucket, the
Athena results bucket, the Glue database and table, the Athena workgroup, the
writer and analyst roles -- and the alerts topic. One region; nothing in the
replica region. The Bedrock guardrail and the reranker are not Terraform's in
either profile: both are read from `.env` and checked with `cascade aws check`.

What it gives up is written in ADR-0054 and is not small: the lake has the
writer's Deny and no lock, so this profile does not claim the lake is
immutable; there is no tier-0 archive on AWS; and nothing records who read
the lake. **GuardDuty and Config are off because they would add cost and
nothing to show**: a detector is worth the findings somebody reads and Config
the history somebody consults, both bill on activity, and Config's bucket is
the one a destroy cannot empty. Turn either on with its switch when the
account is operated rather than demonstrated.

**Prove it works, after an apply:**
```bash
uv sync --extra dev --extra kernel --extra aws --extra analytics   # pyarrow writes the Parquet
AWS_PROFILE=<profile> cascade aws smoke-test --record .logs/aws-smoke.json
```
Three events that say they are synthetic go round the lake: the **writer
role** puts one small Parquet file under `config_id=SMOKE` and is then
refused a delete -- the refusal is required, because without the lock that
Deny is the lake's one append-only control; the **operator** registers the
partition; the **analyst role** reads the rows back through the workgroup;
and the rows are compared with what was written. Every step prints the
request id AWS gave it. It costs one object of a few kilobytes and one
Athena query.

Two things to know before relying on it. **The smoke test is not the event
export**: no code here moves the real event log from Postgres to the lake,
so apart from those three rows Athena has a table and nothing in it. A real
export also needs an identity that may register partitions, which neither
lake role can. And
**`terraform test` reads the local `terraform.tfvars`**, which is why every
test file pins the switches -- and why `make infra-check`, which
re-initialises each root with `-backend=false`, is run on a copy of the tree
while this directory is initialised against the real backend.

**2. Configure the sandbox**
```bash
cd ../sandbox
cp backend.hcl.example backend.hcl                  # fill from bootstrap's outputs
cp terraform.tfvars.example terraform.tfvars        # us-west-2, AZs, acu
terraform init -backend-config=backend.hcl
```

**3. Create it.** Start the clock: from here Aurora bills per ACU-hour at the
fixed capacity, and each interface endpoint per AZ-hour.
```bash
terraform apply
```

**4. Build and push the bench image** (arm64, for Fargate Graviton). Tags are
immutable in ECR, so a new build needs a new tag and a matching `image_tag`.
```bash
REPO=$(terraform output -raw repository_url)
aws ecr get-login-password | docker login --username AWS --password-stdin "${REPO%%/*}"
docker buildx build --platform linux/arm64 -f infra/docker/bench.Dockerfile -t "$REPO:m11" --push .
```

**5. Move the corpus.** The schema comes from the migrations on Aurora, the
rows from a data-only dump of the local database, and the HNSW indexes are
built on Aurora rather than restored.
```bash
docker exec cascade-postgres pg_dump -U cascade_admin -d cascade --format=custom --data-only > corpus.dump
aws s3 cp corpus.dump "s3://$(terraform output -raw artifacts_bucket)/corpus.dump"
```

**6. Run inside the VPC.** `terraform output -raw run_task` prints the exact
command; replace `COMMAND` with the subcommand as a JSON array:
```text
["cascade","db","migrate"]
["sh","-c","python -c \"import boto3,sys; boto3.client('s3').download_file(sys.argv[1],'corpus.dump','/scratch/corpus.dump')\" BUCKET && PGPASSWORD=\"$CASCADE_DB_ADMIN_PASSWORD\" pg_restore --data-only --no-owner -d \"host=$CASCADE_DATABASE__HOST user=cascade_admin dbname=cascade sslmode=require\" /scratch/corpus.dump"]
["cascade","retrieval","index","--drop-legacy"]
["cascade","retrieval","verify"]
["cascade","retrieval","bench"]
```
**The restore line is untested.** A data-only restore across foreign keys
normally leans on `--disable-triggers`, which needs a true superuser, and
Aurora's master user is `rds_superuser`, not one. If pg_restore reports
constraint violations, restore table by table in dependency order; the first
live run settles it, and the result goes in the build log.

Results land in the task's log group (`terraform output -raw log_group`).
Record the ACU setting beside every number: it is a condition of the
measurement, and the experiment runs at two sizes.

**6b. Optional: switch the app roles to IAM tokens.** Two explicit steps, in
this order — granting `rds_iam` disables the roles' passwords:
```text
["cascade","db","enable-iam"]          # as a task; exits 3 anywhere but RDS
terraform apply -var db_auth=iam       # the next task logs in with tokens
```
Untested against a live cluster. To go back: `REVOKE rds_iam FROM cascade_sim,
cascade_eval` as the admin, then `db_auth=stored`.

**6c. The ingest, with a way out.** The chains (`pipeline`) can be created
without it, and the ingest then stops at `corpus verify`, exit 3, having
fetched nothing. To let `CorpusBuild` fetch, set `egress` — every attribute
is a decision (`terraform.tfvars.example`). Start with
`dns_firewall_action = "ALERT"`, run one ingest, and read
`terraform output egress` → `dns_log_group` for names that would have been
refused; then switch to `BLOCK`. The NAT gateway bills per hour from `apply`
to `destroy`, and the sources see one address (`nat_public_ip`).

**6d. The study, with a model, a cache that outlives the task, and somewhere
to publish.** Set `study`: the provider (`aws` -- Claude Platform on AWS,
`claude_platform_aws` in the settings --, `bedrock` or `anthropic`; the
subscription CLI is local-only and refused here), its routing, `recovery` copied whole from the platform root's `terraform output
recovery`, and `reports` copied whole from its `terraform output
reports_publish`.
The task runs on its own definition, records through the provider, and keeps
`llm.cache_dir` on an EFS file system that a DataSync task copies into the
recovery bucket on `sync_schedule_expression` (no default). `anthropic`, and
`aws` without `model_endpoint_service_name`, also need `egress`; `bedrock`
cannot be combined with `pipeline` (no batches, ADR-0028). Then give the
platform root the task role: `terraform output study` → `task_role_arn` into
`simulation_principal_arns`, so the recovery bucket denies it the labels. The
provider's price table (`providers.<p>.pricing`) ships empty and the task
exits 3 until it is filled from the provider's live page — no price is
transcribed here.

**6e. Before destroying a sandbox with a study**, start the archive task and
wait for it: `aws datasync start-task-execution --task-arn $(terraform output
-json study | jq -r .sync_task_arn)`. The file system goes with the sandbox;
the archive is only as new as its last run.

**6f. Publish the report.** `cascade report` writes `reports/study_<ts>/`
inside the task. `terraform output publish_report` prints the exact upload
command; replace `STUDY_ID` with that directory. The task role may add objects
under `reports/<sandbox>/` and may **not** read one back — a report carries the
resolution labels per scenario, so the process that writes one is denied the
read (ADR-0046). Readers attach the platform root's
`reports_read_policy_arn` and `aws s3 sync` the directory; there is no site
and no public endpoint, by decision. Every version is locked for
`report_lock_retention_days`: a wrong figure is superseded by a new report,
never withdrawn.

**6g. The restore drill.** Nothing here has ever been restored.
`docs/architecture/dr-runbook.md` has the ordered procedure, the check that
proves each step, and the role it runs as
(`terraform output recovery_restore_role_arn`), which reads tier 0 in both
regions and cannot write either. Its last step is a deliberate failure: one
`aws s3 cp` into the recovery bucket, which must be refused.

**7. Partitioning experiment.** `experiment_clone = true` creates a
copy-on-write clone; re-partition and bench it by overriding
`CASCADE_DATABASE__HOST` with `terraform output -raw clone_endpoint`.

**8. Destroy.** The sandbox is disposable by design — deletion protection off,
the image repository and artifacts bucket force-deletable.
```bash
terraform destroy
```

## The plan workflow

`.github/workflows/infra-plan.yml` plans `envs/platform` on pull requests
that touch `infra/**` or `configs/base.yaml`, through the OIDC plan role
`modules/cicd` creates, and posts the plan as the job summary. It is skipped
until the repository variable `AWS_PLAN_ROLE_ARN` is set (the workflow lists
the others). There is no apply workflow: apply is a person at a terminal
(ADR-0035), and `tests/unit/test_infra_invariants.py` fails if one appears.

## Cost

No prices are written here: they change, and a stale figure in a README is the
same defect as a stale figure in a report. What bills while the sandbox exists:
Aurora ACU-hours at the fixed capacity, storage and I/O; four interface
endpoints per AZ-hour (five with Bedrock); three KMS keys per month; Secrets
Manager per secret per month; Fargate vCPU- and memory-seconds while a task
runs; CloudWatch Logs ingestion. With `egress`: the NAT gateway per hour and
per GB processed, one public IPv4 address per hour, DNS Firewall per domain
and per million queries. With `study`: EFS storage and elastic throughput, and
DataSync per GB plus S3 requests per object scanned on every run — which is
why the schedule is yours to set. In the platform root: S3 storage for the
published reports and the tier-0 archive, and **S3 Inventory per million
objects listed on every run**, which is the other schedule that is yours to
set (`inventory_schedule`) and which grows with the archive. Under the
portfolio profile the standing charge is one KMS key per month; S3 storage,
Athena bytes scanned and KMS requests bill only when the lake is used, and
none of the recovery, inventory, GuardDuty or Config charges exist. Nothing here is
meant to run between measurements. ADR-0042 and ADR-0046 record the prices
that were verified for their decisions, and the ones that were not — the S3
pricing table did not render for ADR-0046, so no S3 figure is quoted anywhere
in this repository.
