# Cascade infrastructure (M11)

Aurora PostgreSQL 16 + pgvector 0.8.0 in an isolated VPC, with the retrieval
bench running as a Fargate task beside it. Built to be **created, measured and
destroyed**. Design: [ADR-0034](../../docs/adr/0034-aurora-data-plane.md);
toolchain and gates: [ADR-0033](../../docs/adr/0033-infrastructure-as-code-terraform.md).

```
envs/bootstrap   encrypted, versioned state bucket (local state, run once)
envs/sandbox     network + database + bench, composed
modules/network  private subnets only, VPC endpoints, flow logs -- no internet path
modules/database Aurora 16.11 Serverless v2, KMS, TLS forced, IAM auth, role secrets
modules/bench    ECR, ECS/Fargate task, artifacts bucket, least-privilege roles
```

## What is verified, and what is not

| | Status |
|---|---|
| Configuration valid against provider schemas (AWS 6.65.0) | **verified offline** |
| Security properties (encryption, TLS, IAM auth, nothing public, pgvector pinned, fixed capacity, secrets never in plain env) | **verified offline** — 17 `terraform test` runs against mock providers |
| The pgvector gate rejects an engine that is too old | **verified offline** — the test expects 16.6 to fail |
| No internet egress, no open CIDRs, every Checkov skip justified, images pinned by digest | **verified offline** — `tests/unit/test_infra_invariants.py` |
| TFLint (AWS ruleset 0.48.0), Checkov 3.3.19 | **clean** — 270 passed, 0 failed, 27 justified skips |
| AWS accepts the configuration at apply | **not verified** — no AWS account yet |
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

**7. Partitioning experiment.** `experiment_clone = true` creates a
copy-on-write clone; re-partition and bench it by overriding
`CASCADE_DATABASE__HOST` with `terraform output -raw clone_endpoint`.

**8. Destroy.** The sandbox is disposable by design — deletion protection off,
the image repository and artifacts bucket force-deletable.
```bash
terraform destroy
```

## Cost

No prices are written here: they change, and a stale figure in a README is the
same defect as a stale figure in a report. What bills while the sandbox exists:
Aurora ACU-hours at the fixed capacity, storage and I/O; four interface
endpoints per AZ-hour; three KMS keys per month; Secrets Manager per secret per
month; Fargate vCPU- and memory-seconds while a task runs; CloudWatch Logs
ingestion. Nothing here is meant to run between measurements.
