# ADR-0034 — The Aurora data plane: pgvector pinned, no internet, bench in the VPC

- **Status:** accepted — design gated offline; not yet applied (no AWS account)
- **Milestone:** M11
- **Records the design that makes M11's retrieval measurement mean what it claims**

## Context

The one acceptance criterion still missed on real data is retrieval p95:
**90.92 ms** against **15 ms** (M8), with recall@20 at 0.9675. M8 traced the
remaining gap to the partition count and to a **7.75 GB container** — raising
`shared_buffers` made p95 *worse*, because the buffer pool took memory from
the OS page cache. That is a hardware ceiling. M11 removes it by moving
Chronofence to Aurora, and the design exists so the resulting number is a
measurement of Aurora, not of anything else.

## Decision

1. **Aurora PostgreSQL 16.11, pgvector 0.8.0, minor upgrades off.** Measured on
   2026-09-19 from AWS's extension table: 16.8–16.11 ship pgvector 0.8.0,
   16.13 ships 0.8.1, 16.14 ships 0.8.2. The local measurements are 0.8.0, so
   16.11 holds pgvector constant across the comparison. `engine_version`
   refuses anything without pgvector ≥ 0.8.0 — the plan's hard gate, as a
   Terraform validation, tested to fire on 16.6.
2. **An isolated VPC.** Private subnets only; no internet gateway, NAT gateway
   or Elastic IP (asserted statically). AWS services are reached through four
   interface endpoints — `ecr.api`, `ecr.dkr`, `logs`, `secretsmanager`, the
   minimum the task needs, since each is billed per AZ-hour — and S3 through a
   gateway endpoint whose policy names the artifacts bucket and ECR's layer
   bucket, so it cannot move data to an arbitrary bucket.
3. **The bench runs as a Fargate task inside the VPC.** Latency measured from a
   laptop over the internet measures the internet. The task has no ingress,
   egress only to the endpoints, S3 and PostgreSQL, a read-only root
   filesystem, and the embedding model baked into an image pinned by digest,
   because there is no internet to fetch it from.
4. **Fixed capacity for measurement.** Serverless v2 scales under load, and a
   p95 taken while it scales measures the scaler. The sandbox sets minimum and
   maximum ACU to one value per run; the experiment runs at two sizes, each
   stated, neither tuned toward the target.
5. **The partitioning experiment runs on a copy-on-write clone.** M8 deferred
   annual partitioning as "a 1.95M-row rewrite whose recall consequence needs
   its own measurement". A clone shares pages with the source until either
   writes, so the rewrite costs only what it changes and never disturbs the
   baseline.
6. **Identity and secrets.** TLS is required (`rds.force_ssl = 1`). The master
   password is RDS-managed and never in Terraform state. IAM database
   authentication is enabled, and the task role may `rds-db:connect` as the two
   app roles. Until the CLI switches to IAM tokens, `cascade_sim` and
   `cascade_eval` keep generated passwords in Secrets Manager, injected as ECS
   secrets. Those passwords are in state, which is why state is encrypted with
   its own CMK. Invariant 2 — the simulation cannot read `scenario_labels` —
   stays a Postgres grant on Aurora exactly as locally.

## Rationale

Each decision removes a way the p95 could measure something other than
Chronofence on Aurora: a moving pgvector version (1), the internet (2, 3), the
autoscaler (4), a disturbed baseline (5). The security posture follows from the
same design rather than being added to it: an isolated network is both the
faster measurement and the smaller attack surface.

## Cost of being wrong

If 16.11 is withdrawn from a region, the validation refuses it and 16.8–16.10
are the constant-pgvector alternatives. If the p95 still misses 15 ms on
Aurora, the finding is that the gap was not the hardware ceiling, and the
partition count (3.9 ms + 0.34 ms per partition, M8) is what remains — which
the clone experiment measures directly.

## Not yet done

The CLI still connects with `sslmode=prefer` and passwords: `sslmode=verify-full`
and IAM tokens in `DatabaseConfig` are M11's code change. Nothing has been
applied, the image has not been built, and no Aurora number exists.

## Verified by

`terraform test` (17 runs, mock providers), `tests/unit/test_infra_invariants.py`,
TFLint and Checkov — see ADR-0033. AWS's pgvector-per-version table, read
2026-09-19.
