# Demo script: Cascade in five minutes

A timed walkthrough for a live presentation. Every command below was run
against `main` and the deployed account on 2026-10-02. If the network or AWS
is unavailable, use the [offline path](#offline-path-no-network-no-aws) at the
bottom: the same story, told from the committed evidence.

## Before you present

Run these once, a few minutes ahead. None of them changes anything.

```bash
make up                                   # Postgres + Langfuse, waits for healthy
cascade doctor                            # ends with: doctor: all checks passed
export AWS_PROFILE=<your-profile>         # credentials for the deployed account
aws sts get-caller-identity               # confirms the session is live
cascade aws smoke-test                    # a rehearsal; it is idempotent
```

Open three things in tabs: the repository home page, [ARCHITECTURE.md](ARCHITECTURE.md)
and [the evidence pack](evidence/README.md). Use a terminal at least 120
columns wide.

The smoke test needs the Parquet writer:
`uv sync --extra dev --extra kernel --extra aws --extra analytics`.

## The five minutes

### 0:00 – 0:45 · The problem

*Show: the top of the README.*

> "Ask an AI model whether a merger will be approved and you get a number,
> with no way to see why, replay it, or prove it didn't peek at the answer.
> Cascade models the situation instead: it compiles the question into a
> causal graph of actors and forces, simulates those actors with LLM agents,
> and scores the forecast against what really happened. Three things make it
> trustworthy: the evidence is time-locked, every run replays byte for byte,
> and every decision can be traced to its cause."

Point at the metrics row: 2,533 tests, 21 live AWS resources, zero drift.

### 0:45 – 1:30 · The architecture

*Show: the diagram in [ARCHITECTURE.md](ARCHITECTURE.md).*

> "Three columns. On the left, the application: retrieval, the graph compiler,
> the simulation, scoring, and an append-only event log. In the middle, one
> door to every model — four providers behind one interface; today it runs on
> the Claude Code CLI, and Claude Platform on AWS or Bedrock is a
> configuration change. On the right, what is live in AWS: an encrypted S3
> event lake, a Glue catalog, Athena, and two IAM roles — one that can only
> write, one that can only read."

### 1:30 – 2:30 · The application

*Show: the terminal.*

```bash
cascade trace replay --runs 25 --policy heuristic
```

> "Twenty-five stored simulation runs, each re-run in a fresh process with a
> different hash seed. All 25 reproduce their event log byte for byte."

Expected: `byte-identical event log  25/25`, in about five seconds.

```bash
cascade trace explain
```

> "And any outcome can be walked back. This is one forecast, traced decision
> by decision to the external shock that started it."

Expected: a chain of decisions ending in `complete chain: ... to an exogenous shock at step 0`.

### 2:30 – 4:00 · Live on AWS

*Show: the terminal. This is the centrepiece.*

```bash
cascade aws check
```

> "Three read-only calls: who am I, does the Bedrock guardrail resolve, does
> the reranker resolve. Nothing is invoked and nothing is spent."

```bash
cascade aws smoke-test
```

Narrate the rows as they appear:

> "It assumes the **writer role** and writes three events to S3 as Parquet,
> encrypted with our KMS key. Then it asks that same role to **delete** the
> file — and AWS refuses. That is least privilege, tested live, not just
> written in a policy. The partition is registered in Glue. Then it assumes
> the **analyst role** and queries through Athena. Three rows written, three
> identical rows returned, under a kilobyte scanned — and an AWS request ID
> for every call."

Expected last line: `round trip complete: synthetic events only, not the event export`.

Say it plainly: these are synthetic events that prove the path; exporting the
full event log continuously is the next step.

### 4:00 – 4:30 · Infrastructure and security

*Show: [evidence/terraform-no-drift.svg](evidence/terraform-no-drift.svg) and
[evidence/infra-security.svg](evidence/infra-security.svg).*

> "All of that is Terraform: 21 resources, applied from a reviewed plan, and
> the next plan shows no changes. The same code holds the full production
> design — cross-region recovery, Object Lock, GuardDuty, Config — as
> switches that are off in this low-cost deployment. Every change is gated by
> 193 Terraform tests and a security scan with 1,027 checks passing and zero
> failing."

To show the plan live instead (about 40 seconds, so start it during the
previous section in a second terminal):

```bash
cd infra/terraform/envs/platform
terraform plan -var-file=portfolio.tfvars
# No changes. Your infrastructure matches the configuration.
```

### 4:30 – 5:00 · Results

*Show: the README's "Current validated scope" table.*

> "So: a working forecasting simulation with a time-locked evidence engine of
> two million chunks; a live, encrypted, least-privilege analytics plane on
> AWS, proven end to end; and an evaluation harness that reports what it
> measures. The full study ran — 360 simulations, 66,000 model calls — and
> the result is in the repository as measured. Everything you saw is
> reproducible from this repository."

## Likely questions

| Question | Answer |
|---|---|
| Is Claude running on AWS? | Not today. Inference uses the local Claude Code CLI. Claude Platform on AWS and Bedrock are implemented behind the same interface and switch on by configuration. |
| Is real data in the lake? | The smoke test writes three synthetic events. Continuous export of the real event log is the next step. |
| Does it forecast better than a single model? | The first study measured the three approaches as statistically indistinguishable on 36 questions. The harness reports that rather than a flattering number; the held-out test set is untouched. See [RESULTS.md](RESULTS.md). |
| What does the AWS deployment cost? | One KMS key standing; everything else bills on use. |
| What is in Terraform but not deployed? | Recovery with cross-region replication, Object Lock, GuardDuty, Config, a reports bucket, and an Aurora sandbox. |
| How do you know retrieval cannot leak the future? | The date filter is inside the database, and the leakage suite plants post-cutoff documents and asserts none is retrieved. |

## Offline path: no network, no AWS

Tell the same story from the committed evidence, in this order:

| Minute | Show | Say |
|---|---|---|
| 0:00 | README, top | The problem and the metrics row |
| 0:45 | [assets/cascade-architecture.svg](assets/cascade-architecture.svg) | The three columns |
| 1:30 | [evidence/quality-gates.svg](evidence/quality-gates.svg) | 2,533 tests, strict typing, 25 of 25 replays |
| 2:30 | [evidence/aws-smoke-test.svg](evidence/aws-smoke-test.svg) | The live run, step by step, with request IDs |
| 3:15 | [evidence/athena-roundtrip.svg](evidence/athena-roundtrip.svg) | The query, 953 bytes scanned, the three rows |
| 3:45 | [evidence/least-privilege.svg](evidence/least-privilege.svg) | The writer's policy and its refused delete |
| 4:00 | [evidence/terraform-no-drift.svg](evidence/terraform-no-drift.svg) | 21 resources, no drift |
| 4:30 | [evidence/infra-security.svg](evidence/infra-security.svg) | 193 Terraform tests, 1,027 checks, 0 failed |

With a database but no AWS, the application half still runs live:
`cascade trace replay --runs 25 --policy heuristic` and `cascade trace explain`
need only `make up`. `make demo` runs the whole no-key path from a clean
clone: registry, simulation cells, replay, trace and report.
