# Cascade on AWS — architecture

How the study runs on AWS, and why each piece is there. Decisions:
[ADR-0028](../adr/0028-model-providers-behind-one-door.md) (model access),
[ADR-0033](../adr/0033-infrastructure-as-code-terraform.md) (IaC and gates),
[ADR-0034](../adr/0034-aurora-data-plane.md) (data plane),
[ADR-0035](../adr/0035-platform-design.md) (platform),
[ADR-0042](../adr/0042-ingest-egress-model-access-and-durable-caches.md)
(the way out, model access, durable caches, the plan workflow, findings).

> **Status: designed and gated offline. Nothing here has been applied**, because
> no AWS account has been available. Every property below marked *tested* is
> asserted by `terraform test` against mock providers or by the pytest suite —
> which proves the configuration says what it should, not that AWS accepts it.

## The shape

```mermaid
flowchart TD
  subgraph org["AWS Organization"]
    scp["SCPs: allowed regions, audit trail, encryption, no root"]
    subgraph acct["Dedicated study account"]
      budget["Budget = phase ceilings from base.yaml + allowance"]
      anomaly["Cost Anomaly Detection"]
      subgraph vpc["VPC"]
        subgraph isolated["Isolated tier: private subnets, no default route"]
          task["Fargate task: cascade CLI (read-only root)"]
          study["Study task: + model grant, + cache mount"]
          aurora[("Aurora PostgreSQL 16.11 + pgvector 0.8.0")]
          efs[("EFS: LLM cache, one access point")]
          ep["VPC endpoints: ECR, Logs, Secrets Manager, S3, (bedrock-mantle)"]
        end
        subgraph egress["Egress tier (opt-in, one AZ): NAT + DNS allow-list"]
          fetch["CorpusBuild, and model calls without a private path"]
        end
      end
      lake[("S3 event lake: Object Lock")]
      recovery[("S3 recovery: registry/ source-cache/ llm-cache/ -- locked, replicated")]
      athena["Athena workgroup: enforced encryption + scan limit"]
      secrets["Secrets Manager + KMS"]
      alerts["Alerts topic: budget, alarms, task failures, GuardDuty findings"]
    end
  end
  model["Claude Platform on AWS (IAM, batches) / Bedrock (no batches) / api.anthropic.com"]
  sources["Common Crawl, Wikipedia, Federal Register, EDGAR, GDELT"]

  scp -.-> acct
  task --> aurora
  study --> aurora
  study --> efs
  task --> ep
  ep --> secrets
  task -->|append only| lake
  athena --> lake
  study -->|SigV4 via endpoint| model
  fetch -->|HTTPS, allow-listed names| sources
  fetch -->|when no PrivateLink| model
  efs -->|DataSync, hourly+, add-only| recovery
  budget --> anomaly
  anomaly --> alerts
```

## Why it is shaped this way

| Decision | Reason | Enforced by |
|---|---|---|
| One dedicated account for the study | Model spend via Claude Platform on AWS bills through Marketplace, where cost-allocation tags do not reach; only an account boundary makes "the study's spend" a number a budget can see | `modules/governance` |
| Budget limits read from `configs/base.yaml` | The in-process meter and the AWS-side alarm cannot drift apart | tested, including against a fixture config (a hardcoded value survived the first test and was caught by mutation) |
| No internet path in the VPC by default | The bench measures the database, not the network; and there is no egress to exfiltrate through | static pytest check |
| The way out is one opt-in tier, and only the state that fetches uses it | The ingest must reach the public internet; the four states that do not fetch hold the admin credential and stay inside; a NAT with a DNS allow-list, not a firewall, because the cost of forgetting a firewall for a month is most of the study's budget | tested under mocks (the isolated tier's table never routes out; the database's subnet group never contains an egress subnet) and statically (every gateway, route and open CIDR lives in `modules/egress`) |
| The study task is a second task definition, built from the bench's | The bench must never call a model and the ingest has the internet path; model permissions belong to neither. Per provider: three routes in one workspace, one action in one region, or a key as an ECS secret | tested, including that the bench's settings survive the override |
| The LLM cache is an EFS file system, copied add-only into the recovery bucket | The fan-out's own stop rule is a timeout, which kills the task; an S3 sync at the boundary would lose exactly the runs that paid most. Every recording is money already spent | tested: encrypted with the CMK, one access point, no root, Basic-mode DataSync that never deletes or overwrites |
| GuardDuty findings go to the alerts topic above a chosen severity | A detector nobody reads is a budget with no alarm; the threshold has no default because what pages a person is a decision | tested: the topic admits the rule by ARN, and a finding that cannot be delivered alarms |
| `terraform plan` on pull requests through OIDC; apply is a person | A stored key is long-lived and leaks; the plan role is read-only and the job is skipped until an account exists. No apply workflow, by decision | tested: permissions, gating, SHA-pinned actions, no `apply` |
| The bench runs inside the VPC | A p95 measured over the internet measures the internet | `modules/bench` |
| pgvector pinned by pinning Aurora 16.11 | The Aurora numbers must compare against local pgvector 0.8.0 | tested: 16.6 is refused |
| Event log under S3 Object Lock, writer denied delete | Invariant 6 (append-only) as two independent controls | tested |
| Labels never enter the lake | Invariant 2: nothing to read, so nothing to guard | by construction |
| Claude Platform on AWS carries simulate; Bedrock does not | Bedrock has no Message Batches API, and simulate fits its ceiling only at the batch rate | `complete_batch` refuses, tested |
| Bedrock Knowledge Bases not used | A managed KB cannot enforce the `as_of` time lock at the storage layer | [ADR-0030](../adr/0030-knowledge-bases-rejected-guardrails-deferred.md) |

## Project invariants, as AWS controls

| Invariant | Locally | On AWS |
|---|---|---|
| 1 — `as_of` never defaulted | required argument in Python and SQL | unchanged: Chronofence is the same SQL on Aurora; **region never defaulted** applies the same rule to where spend lands |
| 2 — simulation never reads labels | Postgres grant | the same grant on Aurora, plus separate IAM roles for the lake; labels are never exported to it; in the recovery bucket, an explicit Deny on `registry/*` **and `source-cache/*`** for the study task's role — the source cache is raw market responses, which state resolutions |
| 5 — one model call site | one SDK importer | unchanged: all AWS clients ship in the `anthropic` package |
| 6 — event log append-only | INSERT-only grant | Object Lock + an explicit Deny on delete and lock overrides |
| 8 — every phase resumable | checkpoints, unit states | unchanged; the ingest's stop rules stop *between* units |

## Further reading

[Threat model](threat-model.md) · [Disaster recovery](dr-runbook.md) ·
[Well-Architected review](well-architected.md) · [Runbook](../../infra/terraform/README.md)
