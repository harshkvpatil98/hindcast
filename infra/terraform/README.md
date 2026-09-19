# Cascade infrastructure (M11–M12)

Aurora PostgreSQL 16 + pgvector 0.8.0 in an isolated VPC, with the retrieval
bench running as a Fargate task beside it. Built to be **created, measured and
destroyed**. Design: [ADR-0034](../../docs/adr/0034-aurora-data-plane.md);
toolchain and gates: [ADR-0033](../../docs/adr/0033-infrastructure-as-code-terraform.md);
the platform: [ADR-0035](../../docs/adr/0035-platform-design.md); the way
out, model access and the caches:
[ADR-0042](../../docs/adr/0042-ingest-egress-model-access-and-durable-caches.md).

```
envs/bootstrap   encrypted, versioned state bucket (local state, run once)
envs/platform    M12: cost governance + event lake + SCP guardrails (docs/architecture/)
envs/sandbox     network + database + bench, composed
modules/network  private subnets only, VPC endpoints, flow logs -- no internet path
modules/database Aurora 16.11 Serverless v2, KMS, TLS forced, IAM auth, role secrets
modules/bench    ECR, ECS/Fargate task, artifacts bucket, least-privilege roles
modules/governance  budget derived from configs/base.yaml, anomaly detection, alerts
modules/guardrails  service control policies; attached to nothing until targets are named
modules/eventlake   Object-Locked event log, Glue catalog, Athena workgroup, writer/analyst roles
modules/recovery    tier-0 recovery bucket: locked, replicated cross-region, closed to the simulation
modules/audit       CloudTrail (locked bucket, data events for the lake and recovery buckets), GuardDuty, Config
modules/cicd        GitHub OIDC roles: read-only plan from pull requests, apply from a reviewed environment
modules/pipeline    Step Functions chains for the ingest and the study (in envs/sandbox, opt-in)
modules/observability  exit-code-preserving task alerts, Aurora alarms, dashboard (in envs/sandbox, opt-in)
modules/egress      the one way out: one NAT in one AZ, a DNS allow-list that fails closed (in envs/sandbox, opt-in)
modules/cache       the LLM cache on EFS, copied add-only into the recovery bucket by DataSync (in envs/sandbox, with `study`)
modules/study       the study task: the bench's container plus a model grant per provider and the cache mount (in envs/sandbox, opt-in)
```

**Destroying the platform root:** the Config bucket denies `s3:DeleteObjectVersion`
to every principal, and the trail, lake and recovery buckets are under Object
Lock. `terraform destroy` cannot empty them; that is the point. Remove the
Config bucket's policy and wait out (or, under GOVERNANCE, explicitly bypass)
the retention before deleting them by hand.

## What is verified, and what is not

| | Status |
|---|---|
| Configuration valid against provider schemas (AWS 6.65.0) | **verified offline** |
| Security properties (encryption, TLS, IAM auth, nothing public, pgvector pinned, fixed capacity, secrets never in plain env) | **verified offline** — 60 `terraform test` runs in `envs/sandbox`, 89 in `envs/platform`, against mock providers |
| The isolated tier never routes out, even with the egress tier on; only `CorpusBuild` and (without PrivateLink) the three model-calling states leave; the study grant is three routes in one workspace; the cache is encrypted and copied add-only; findings are admitted by ARN | **verified offline** — `terraform test`, and each broken on purpose once (ADR-0042) |
| The pgvector gate rejects an engine that is too old | **verified offline** — the test expects 16.6 to fail |
| Every gateway, route and open CIDR lives in `modules/egress`; the two opt-ins default to null; no decision input has a default; every Checkov skip justified; images pinned by digest; every third-party action in every workflow pinned by SHA | **verified offline** — `tests/unit/test_infra_invariants.py` |
| TFLint (AWS ruleset 0.48.0), Checkov 3.3.19 | **clean** — 925 passed, 0 failed, 66 justified skips |
| AWS accepts the configuration at apply | **not verified** — no AWS account yet. In particular: that DataSync writes into the locked recovery bucket; that the regional DNS allow-list is complete; the Claude Platform on AWS PrivateLink service name |
| The bench image builds and runs | **not verified** — linted, not built |
| Any Aurora latency or recall number | **does not exist** |

Run every offline gate with `make infra-check` (Docker required; no AWS
credentials needed).

## Runbook

Everything below needs an AWS account. Each step says what it costs to leave
running.

**1. State bucket** (once per account and region)
```bash
cd infra/terraform/envs/bootstrap
terraform init && terraform apply -var region=us-east-1
```

**2. Configure the sandbox**
```bash
cd ../sandbox
cp backend.hcl.example backend.hcl                  # fill from bootstrap's outputs
cp terraform.tfvars.example terraform.tfvars        # region, AZs, acu
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

**6d. The study, with a model and a cache that outlives the task.** Set
`study`: the provider (`aws`, `bedrock` or `anthropic`), its routing, and
`recovery` copied whole from the platform root's `terraform output recovery`.
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
why the schedule is yours to set. Nothing here is meant to run between
measurements. ADR-0042 records the prices that were verified for its
decisions, and the ones that were not.
