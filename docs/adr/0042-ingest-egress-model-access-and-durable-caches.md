# ADR-0042 — A way out for the ingest, model access for the study, and caches that outlive the task

- **Status:** accepted as a design — gated offline, not applied
- **Milestone:** M12 follow-up
- **Closes the five gaps ADR-0035's "Where the design stops" and the Well-Architected review named**

## Context

ADR-0034 gave the study an isolated VPC, and ADR-0035 a platform around it.
Both were right, and both left the two Step Functions chains unable to
finish: the ingest fetches from the public internet and had no path to it;
the study task was pinned to `replay`, had no model permission, and kept the
LLM cache on scratch storage that died with the task. Around them, three
mechanisms existed with nothing attached: the OIDC roles no workflow used, a
GuardDuty detector whose findings went nowhere, and a recovery bucket that
held the registry but neither cache — the two datasets whose loss this
project has already paid for once (`docs/architecture/dr-runbook.md`).

Each gap is a decision about money, exposure or identity. This record makes
the decisions, names what was rejected, and writes down what is still not
done and what could not be verified without an account.

## Decisions

### 1. One egress tier, opt-in, NAT plus a DNS allow-list — not a firewall

`modules/egress` adds one public subnet with one NAT gateway and one private
*workload* subnet whose route table is the only one in the VPC with a default
route. The database, the bench, the interface endpoints and the cache's mount
targets stay in `modules/network`'s subnets, whose route table the egress
module never sees. A task leaves by being launched in the workload subnet
with the egress security group *added* to its own; in the ingest chain that
is `CorpusBuild` alone — the four states that never fetch, which carry the
database's admin credential, stay inside. `envs/sandbox` creates none of it
unless `egress` is given, and the default is `null`.

A NAT forwards to any address and a security group cannot name a domain, so
the narrowing is Route 53 Resolver DNS Firewall: an allow-list of the names
the ingest resolves (read from `cascade/corpus/sources/*.py`, supplied by the
operator, never typed into a module) plus the regional AWS names every task
needs, then NXDOMAIN for everything else. `dns_firewall_action` has no
default: AWS's own guidance is to run a new list in ALERT and read the query
log before BLOCK, and DNS Firewall is associated with the whole VPC — a list
that forgot a name the isolated tier needs would break the bench, and no
offline gate can find that out.

**Rejected: (a) the S3 gateway endpoint for Common Crawl.** The code fetches
CC-NEWS over HTTPS from `data.commoncrawl.org`, a CloudFront name, not
through the S3 API. A gateway endpoint carries nothing for it without a code
change, and `cascade/` is out of this record's scope. The saving it would
buy, once the code reads `s3://commoncrawl` with the task role and the
sandbox is in us-east-1, is the NAT's processing charge on the crawl —
about 140 GB × $0.045 ≈ **$6.30** per full read — which is recorded here as
the price of not doing it.

**Rejected: (c) AWS Network Firewall.** It does filter, by SNI, which DNS
Firewall does not. Its price is per firewall endpoint-hour whether or not an
ingest is running: $0.395/h, or about **$288 a month per AZ** — most of this
study's entire budget ($330 across the five phase ceilings) — against about
**$33 a month** for a NAT gateway left running by the same mistake. A
three-day ingest under Network Firewall would cost roughly $28 + $9 in
traffic (NAT charges are waived when chained), against roughly $3 + $6 under
NAT. The stronger filter is bought with a failure mode this project cannot
afford, and with a firewall-subnet routing topology that no offline gate can
validate. The residual risk of the choice is stated below.

**Region.** If the sandbox is not in us-east-1, the crawl read costs the
reader the same under the current code — the bytes arrive from a CDN over
the internet, and AWS bills nothing for data transferred in — and the
gateway-endpoint saving above becomes unavailable: a gateway endpoint serves
its own region's S3 only, so an S3-API read of a us-east-1 bucket from
another region goes through the NAT regardless. The inter-region transfer
charge on a public bucket is billed to the bucket's owner, not the reader,
unless the bucket is Requester Pays, which `commoncrawl` is not. Common
Crawl's own guidance is to read from us-east-1.

### 2. The study task: a second task definition, IAM per provider, the cache on EFS

`modules/study` builds a second task definition from `modules/bench`'s
container *as a value* — the same image, database wiring and log group, with
`CASCADE_LLM__MODE=record`, the provider's routing, and the cache directory
moved to an EFS mount. It has its own task role and security group, so the
bench's promise ("the bench never calls a model") and the ingest task, which
has the internet path, both keep the least they need. `model_provider` has no
default. Per provider, read from `cascade/llm/providers.py` and verified
against the provider's documentation:

| provider | path | IAM |
|---|---|---|
| `bedrock` | interface endpoint `com.amazonaws.<region>.bedrock-mantle`, private DNS `bedrock-mantle.<region>.api.aws` — **verified** (Bedrock user guide) | `bedrock-mantle:CreateInference` — the action AWS's endpoint-policy example names; its resource types could **not** be verified, so the resource is `*` bounded to one region |
| `aws` | PrivateLink is **stated as supported** (Claude Platform docs) and the endpoint service name is published on no page this project could read, so it is an input (`model_endpoint_service_name`) and never guessed; without it, the egress tier | `aws-external-anthropic:CreateInference`, `CreateBatchInference`, `GetBatchInference` on `arn:aws:aws-external-anthropic:<region>:<account>:workspace/<id>` — **verified** (AWS user guide, IAM actions) — exactly the routes `client.py` calls, not cancel, not delete |
| `anthropic` | the egress tier, `api.anthropic.com` on the allow-list | none; the key is a Secrets Manager secret resolved by the execution role |

Bedrock has no Message Batches API (ADR-0028), so the sandbox refuses at plan
to wire it to the study chain; the task definition remains valid for the
unbatched phases.

**The cache is an EFS file system, not an S3 sync at the task boundary.** The
task that matters is the one that does not end cleanly: the fan-out's own
stop rule is a timeout, which stops the task with SIGTERM and, within 120
seconds on Fargate, SIGKILL — a sync of tens of thousands of objects does not
fit that window, and a task killed for memory gets none. `cache.py` already
writes each entry with write–fsync–rename, and an fsync on EFS is durable when
it returns, so the unit of loss becomes the one call in flight. It also
needs no code, and the chains' alerts depend on the CLI's exit code being the
container's. The loss S3-sync risks would be *correct* — the cache is
content-addressed and idempotent, a missing entry is a miss — but every miss
is billed again, against a simulate ceiling that holds only at the batch
rate (ADR-0020). Acceptable for correctness; not for cost. The file system is
encrypted with the sandbox's CMK, refuses plaintext NFS, root, and any mount
not through its one access point, which pins every client to uid 10001.

### 3. A plan workflow through OIDC; apply stays a person

`.github/workflows/infra-plan.yml` assumes the plan role on pull requests
that touch `infra/**` or `configs/base.yaml`, plans `envs/platform`, and
posts the plan as the job summary. It is inert until the repository variable
`AWS_PLAN_ROLE_ARN` is set — skipped, not red. Its permissions are
`id-token: write` and `contents: read`; the plan role cannot write the state
lock, so the plan runs with `-lock=false`; the plan file is never uploaded.
Every third-party action is pinned to a full commit SHA, resolved from each
repository's git refs, and `tests/unit/test_infra_invariants.py` refuses a
floating tag in any workflow. **There is no apply workflow** (ADR-0035); the
file says so and the test asserts one job.

The existing `ci.yml` uses moving tags for three third-party actions
(`astral-sh/setup-uv@v5`, `hashicorp/setup-terraform@v4`,
`terraform-linters/setup-tflint@v6`). It holds no cloud credential, so this
record **reports** them rather than rewriting them: the test carries them as
a ratchet that fails if they are ever anything else, and shrinks when they
are pinned.

### 4. GuardDuty findings go to the alerts topic, above a severity nobody defaulted

`modules/audit` adds an EventBridge rule matching `GuardDuty Finding` events
with `severity >= guardduty_min_severity` — a numeric match, on a variable
with no default, because what pages a person is a decision about who is on
the other end of the topic — targeting the alerts topic with a readable
message. The topic policy statement admitting it is scoped by `aws:SourceArn`
to that one rule's ARN, exported by the module and merged by the platform
root the way the key-policy statements already are. A `FailedInvocations`
alarm on the rule reports the one failure this design cannot otherwise see:
a matched finding EventBridge could not publish, which is what a wrong topic
or key policy looks like — silence with a green light.

### 5. Both caches are in the recovery plan

The LLM cache reaches `modules/recovery`'s bucket by a scheduled DataSync
task from the file system — under `llm-cache/<sandbox>/`, add-only (the
writer role has no `s3:DeleteObject`, which AWS's documented destination
policy includes), never overwriting (an entry's name is the hash of what it
answers), excluding `cache.py`'s `.tmp` files, in **Basic** task mode
(Enhanced bills $0.55 per execution; hourly, that fee alone is ~$400 a
month), with a failed execution announced on the alerts topic. The schedule
has no default: it is the RPO for losing the file system against a per-run
S3 request cost that grows with the cache, and neither has been measured.
The source cache is made on a person's machine (`ledger build` is not one of
the chains), so its path is a person uploading it under `source-cache/`
with an add-only managed policy the recovery module now exports.

**The source cache holds the labels.** It is the markets' own API responses,
and a resolved market's response says how it resolved. The recovery bucket's
invariant-2 Deny, which covered `registry/*`, now covers `source-cache/*` as
well; the LLM cache is not denied, because the simulation itself wrote it
from prompts invariant 2 already kept label-free.

**Rejected: AWS Backup for the file system.** A recovery point restores only
into EFS, in an AWS account; this project's one real loss was on a laptop,
and the documented restore must reach one. And it would be a second tier-0
store — its own vault lock, its own key, its own cross-region copy rule — to
keep in step with the bucket that already has all three. As plain objects
the archive is restorable with `aws s3 sync` to anywhere, countable against
the file system, and audited by the trail's data events.

## What could and could not be verified

Verified against the provider's own pages on 2026-09-19: the Bedrock Mantle
endpoint service, private DNS name and IAM action
(docs.aws.amazon.com/bedrock/…/vpc-interface-endpoints.html); the Claude
Platform on AWS IAM prefix, actions, workspace ARN format and SigV4 service
name (docs.aws.amazon.com/claude-platform/…/iam-actions.html,
platform.claude.com/…/claude-platform-on-aws); NAT gateway $0.045/h and
$0.045/GB, shown for US East (Ohio) (aws.amazon.com/vpc/pricing); public
IPv4 $0.005/h; Network Firewall $0.395/h and $0.065/GB for N. Virginia and
the NAT-charge waiver (aws.amazon.com/network-firewall/pricing); DataSync
Basic $0.0125/GB, Enhanced $0.015/GB + $0.55/execution
(aws.amazon.com/datasync/pricing); DNS Firewall $0.0005/domain/month and
$0.60 per million queries (aws.amazon.com/route53/pricing); PrivateLink
$0.01/GB processed (aws.amazon.com/privatelink/pricing); S3 inter-region
$0.01/GB N. Virginia → Ohio, data in from the internet free, Requester Pays
semantics (aws.amazon.com/s3/pricing); Common Crawl's bucket region, HTTPS
path and us-east-1 guidance (commoncrawl.org/get-started); the three
action SHAs (api.github.com git refs).

**Not verified**, and left symbolic or as an input: NAT prices for N.
Virginia specifically (the regional table did not render); EFS storage and
elastic-throughput prices; S3 per-request prices; the interface endpoint
hourly rate; the inter-region rate to regions other than Ohio; the Claude
Platform on AWS PrivateLink service name; `bedrock-mantle`'s IAM resource
types; whether DataSync writes into a bucket with a default Object Lock
retention (S3 requires a checksum header on such uploads, and AWS Config
refused a locked bucket outright — the precedent in `modules/audit`); whether
a lone `*` is accepted as a DNS Firewall match-everything entry; whether the
regional AWS-name allow-list is complete (the likeliest omission is a client
resolving a global `s3.amazonaws.com`).

## Residual risk

- **T13 (new):** with the egress tier on, `CorpusBuild` holds the database's
  admin credential and a path to the internet. DNS Firewall stops a task that
  looks a name up; it does not stop one that connects to an address it
  already holds, and a compromised parser could hold one. The controls are
  the one-state scope, TCP 443 only, flow and DNS query logs, and that the
  tier exists only while an ingest does. The control that would close it — a
  `cascade_ingest` Postgres role without `scenario_labels` — is a migration
  and a code change, not infrastructure.
- Claude Platform on AWS states that inference "may route to Anthropic's
  primary cloud"; a private path to the gateway is not a residency guarantee.
- The regional AWS allow-list is derived, not observed: first apply in
  ALERT.

## What mutation testing changed

Thirteen mutants, each restored by hash afterwards; every one was killed by
at least one test on its first run. The database's subnet group given the
egress workload subnet, and a default route added to `modules/network`'s own
table, each caught — the first under mocks, the second statically, which is
the reason both checks exist: a route can be added from a file the mock test
never loads. The `aws:SourceArn` condition dropped from the GuardDuty rule's
topic statement; the egress opt-in defaulted on (caught twice: statically,
and by the run asserting the default sandbox has no way out); the
credentials action unpinned to `@v6`; the cache file system unencrypted
(caught at the module and again at the root, against this root's key); the
severity threshold defaulted; the DataSync task in Enhanced mode; the study
role granted `DeleteBatchInference`; every ingest state moved to the egress
tier; the DataSync writer granted `s3:DeleteObject`; the labels Deny
narrowed back to `registry/*`; the anthropic-needs-egress validation
removed.

What the *tests* changed before the mutants ran: a first control reported
the replication rule as filtered when it was not — `try(x.prefix, "")` does
not catch a null, only an error — and the assertion on it made Terraform
1.16.3 crash while formatting the failure. The control now yields the
prefix itself, `""` when there is none, which is also the more useful
number. And the comparison lesson of ADR-0035 held twice over: an output
built by a `for` expression is a tuple, so both sides of every equality are
now wrapped in `tolist`, and an empty result is tested by `length`.

## Where the design still stops

Not built: the `cascade_ingest` role above; a price in either AWS provider's
table (`providers.*.pricing` ships empty and the task exits 3 until it is
filled from the live page); an artifact for what `cascade report` writes;
Bedrock Guardrails (ADR-0030); a scheduled upload of the source cache (it is
a person); a restore drill for either cache. Not possible without an account:
applying any of it, the first ALERT-mode query log, and every price the
allowance depends on.

## Verified by

`terraform test` — 60 runs in `envs/sandbox` (was 21), 89 in `envs/platform`
(was 73), mock providers; TFLint clean over sixteen directories; Checkov
925 passed, 0 failed, 66 justified skips; `tests/unit/test_infra_invariants.py`
— 20 checks, including that every gateway resource, route and open CIDR
lives in `modules/egress` and nowhere else, that the two opt-ins default to
null and are instantiated under a count, that five more decision inputs are
never defaulted (even through an object attribute), and that every
third-party action in every workflow is pinned by SHA.
