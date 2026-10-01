# ADR-0053 — Finishing as a portfolio: one provider interface, the names the deployment uses, one region, and no further study runs

- **Status:** accepted — requested by the project owner, 2026-09-30
- **Milestone:** M17
- **Amends ADR-0028 (one environment variable the SDK documents is now admitted, through `Settings`) and ADR-0031 (the provider's name); records the owner's change of objective**

## Context

On 2026-09-30 the project owner changed the objective: finish Cascade as an
**engineering portfolio project**, as quickly as possible, rather than as a
research study. Concretely:

- Claude runs through the locally installed, logged-in **Claude Code CLI**
  (`claude -p`) under the owner's subscription, now. No API key, no Claude
  Platform on AWS yet, and the subscription's token is never extracted,
  exposed or stored by this project.
- **Claude Platform on AWS** stays in the provider architecture, so that
  switching later is configuration -- `LLM_PROVIDER=claude_platform_aws` and
  `ANTHROPIC_AWS_WORKSPACE_ID=...` -- but it is not enabled: account
  provisioning is pending.
- The AWS footprint is **us-west-2**, for everything regional: the Terraform
  roots, **Bedrock Rerank** (`amazon.rerank-v1:0`) and **Bedrock Guardrails**
  (the account's existing `cascade-audit-guardrail`, published version 1, read
  from `CASCADE_GUARDRAIL_ID` / `CASCADE_GUARDRAIL_VERSION`).
- **No expensive workload is run to finish**: not the study, not hundreds of
  prompts, not the rerank bench, not the guardrail audit over the stored
  graphs. Zero-cost, local tests only.
- Documentation must say exactly what runs where, and must not claim that
  Claude inference runs through AWS.

Almost all of the machinery already existed by M16: four providers behind the
one call site (ADR-0028), the CLI provider (ADR-0031), the Bedrock reranker
(ADR-0047), the guardrail audit (ADR-0050), sixteen Terraform modules
(ADR-0033 to ADR-0046). What did not exist was the *shape the brief asks for*:
an explicit provider interface with one class per provider, the names the
brief uses, a two-variable switch to Claude Platform on AWS, one region
everywhere, and an honest front page for a project whose study produced a
null result (M16).

## Decision

### 1. Four provider classes behind one interface, inside the one door

`cascade.llm.providers.ModelProvider` is a `Protocol` -- `spec`,
`constructed_sdk_client`, `sdk_client()`, `complete(request)`, `batches()` --
and `cascade/llm/client.py` holds one class per provider:
`AnthropicApiProvider`, `ClaudePlatformAwsProvider`, `BedrockProvider`,
`ClaudeCliProvider`. `LLMClient` builds exactly one through
`build_provider(settings)` and talks to it through the interface alone, so the
cache, the meter, the tracer and the record/replay contract are written once,
above the seam. `complete` returns a `ProviderResponse`: the provider's own
Messages body, its **request id**, and metadata that says how the call was made.

The classes live in `client.py` and nowhere else, for the reason ADR-0030 and
ADR-0047 gave: the one door is a module, and the answer to "a second
model-adjacent path" is to put it inside the door, not in a different package.
`tests/unit/test_invariants.py` still refuses a second SDK importer and a
second builder of a model-serving `boto3` client.

### 2. Canonical names, and the old spellings as aliases

The providers are `anthropic`, `claude_platform_aws`, `bedrock` and
`claude_cli`. `aws` and `claude_code` -- the M10–M16 spellings for the second
and fourth -- are accepted everywhere a provider is named and normalised at
the configuration boundary (`cascade.config.normalise_provider`, a
`BeforeValidator` on `llm.provider`). The `providers:` sections of
`configs/base.yaml` carry the canonical names, and the study task's Terraform
emits them (`CASCADE_PROVIDERS__CLAUDE_PLATFORM_AWS__REGION`, ...).

**The CLI provider's cache namespace keeps its original spelling,
`claude_code`.** A namespace is an identity for stored bytes, not a display
name: renaming it would turn every recording made under ADR-0031 -- the
compiled graphs, the probes, the 66,231 calls of the M16 study -- into a
replay miss. `tests/unit/test_llm_providers.py` pins this.

### 3. Plain environment aliases, read by `config.py` and shown by `doctor`

Four variables each bind to one nested setting, with the nested
`CASCADE_<SECTION>__<FIELD>` spelling outranking the alias and `.env`
supplying either:

| variable | field |
|---|---|
| `LLM_PROVIDER` | `llm.provider` |
| `ANTHROPIC_AWS_WORKSPACE_ID` | `providers.claude_platform_aws.workspace_id` |
| `CASCADE_GUARDRAIL_ID` | `providers.bedrock.guardrail_id` |
| `CASCADE_GUARDRAIL_VERSION` | `providers.bedrock.guardrail_version` |

`ANTHROPIC_AWS_WORKSPACE_ID` is a deliberate, narrow amendment of ADR-0028.
That record passes every routing value to the SDK explicitly *because* the SDK
would otherwise read `AWS_REGION` and `ANTHROPIC_AWS_WORKSPACE_ID` from the
ambient shell. The variable is now admitted -- but **into `Settings`**, where
it is validated, where a reviewed `CASCADE_` setting outranks it, and where
`cascade doctor` prints the resolved workspace -- and still passed to the SDK
explicitly, so the SDK's own fallback never runs. **The region has no alias.**
Where spend lands is configured in a reviewed file and read from no shell.

The pinned study configuration in `configs/base.yaml` remains
`llm.provider: anthropic`. Which provider a *machine* has is machine-local,
so it is a `.env` fact: the shipped `.env.example` says `LLM_PROVIDER=claude_cli`.

### 4. One region

`providers.claude_platform_aws.region`, `providers.bedrock.region` and
`observability.aws_region` are `us-west-2` in `configs/base.yaml`; both
Terraform roots' `terraform.tfvars.example` files name it; the runbook uses it.
The one exception is the tier-0 recovery replica, which by definition is a
second region (`us-west-1`, the nearest). The Terraform roots still refuse a
region default (`tests/unit/test_infra_invariants.py`): us-west-2 is written
where an operator can read it, never assumed.

### 5. The Terraform is completed, not extended

- The platform root now exposes `model_guardrail` and
  `publish_guardrail_version` (the module had them; the root did not pass
  them) and prints `guardrail` and `guardrail_environment` -- the two `.env`
  lines the audit reads.
- The two service control policies are behind
  `create_service_control_policies` (default true). They can only be created
  from an AWS Organizations management account, so a standalone account sets
  it false and keeps the rest of the root, the Bedrock guardrail included.
- The three guardrail tests that read computed attributes `apply` under mock
  providers rather than `plan`. ADR-0050 recorded that those tests had never
  executed, because the development machine's Terraform predates
  `terraform test`; run for the first time under the pinned 1.16.3 in Docker,
  one failed with *Unknown condition value*. Now 112 platform runs pass.

### 6. Request ids and a structured call log

Every `complete` goes through `with_raw_response`, so the provider's request
id (`request-id`, `x-amzn-requestid`, or the CLI's session id) is kept on the
`LLMResult`, on the recording (`CachedCall.provider_metadata`, never in the
key) and in the trace. `BedrockReranker` keeps AWS's request id per page and
books **one search unit per 100 documents**, the service's own pricing rule
("a query ... can contain up to 100 document chunks", read from the Bedrock
user guide on 2026-09-30); `BedrockGuardrail` returns the request id on every
`ScreenResult`, and `cascade eval guardrails` prints it. `CallLogTracer`
appends one JSON object per call to `observability.call_log`, beside or
instead of Langfuse, so a phase can be reconciled against CloudTrail or the
CLI's own session by id.

### 7. A read-only `cascade aws check`

Three control-plane calls -- `sts:GetCallerIdentity`, `bedrock:GetGuardrail`
for the configured guardrail, `bedrock:GetFoundationModel` for the configured
reranker -- that invoke no model and screen no text. Exit 3 on any failure, so
a deployment can gate a paid phase on it. Built in `client.py`, because a
`bedrock` client is a model-serving service's client.

### 8. A stale derived digest, found by running the live gate

`runs.event_log_hash` is a cached digest of a run's events. M16's commit
`e3e13b5` removed `cache_hit` from `DecisionEvent.canonical()` -- correctly,
for the reason it gives -- and left the 2,480 runs stored before it with
digests nothing could reproduce; its gates were offline-only, so the live
replay check never ran. Found here when it did: a fresh replay of a stale run
reproduced all 24 per-step state hashes and a different event-log hash, with
the unmodified frontier code giving the same fresh hash as this branch.

The repair follows migration 016's precedent for `runs.llm_calls`: the
derived column is recomputed from the record, and the record is never touched.
`load_replay_targets` now hashes the **stored events** under the current
domain and compares the replay against that, carrying the cached digest beside
it as *stale* when they differ; `cascade trace rehash --apply` refreshes the
column as the admin role after archiving every replaced value; and a run whose
model recordings are not on this machine -- the M16 study's are on the machine
that ran it -- is reported as *unreplayable here*, which is the honest word,
never as a divergence. `--policy heuristic` verifies the stand-in runs on any
machine with the database.

### 9. The study is not re-run

M16's headline stands as measured: Brier **0.219709** over 36 development
scenarios, not distinguishable from the single-model baseline (0.206903) or
from climatology (0.250000) at that n. Nothing here spends to move it, and
every document that quotes it says what it is. The 125 test scenarios stay
unspent.

## What this record does *not* claim

- That Claude inference runs through AWS. It runs through the Claude Code
  CLI on the operator's machine. Claude Platform on AWS is implemented and
  tested against the real SDK over a mock transport, and has never received a
  call from this project.
- That the guardrail audit or the rerank benchmark has been run against the
  account. Both commands exist and are tested; the development machine
  carries no AWS credentials, and the owner asked that neither workload be
  run to finish the project.
- That `cascade aws check` has succeeded live. It is tested through a fake
  session and has not been run against the account for the same reason.

## Consequences

- A fifth provider is a class in `client.py`, a row in `PROVIDERS` and a
  branch in `build_provider`; nothing in the cache, the meter or the tracer
  changes.
- Two spellings of two names exist in the repository's history and will keep
  appearing in ADRs and build-log entries written before this record. The
  code accepts both; the documentation written from here uses the canonical
  ones.
- A developer's shell that exports `ANTHROPIC_AWS_WORKSPACE_ID` now selects a
  workspace when the provider is `claude_platform_aws` and no `CASCADE_`
  setting names one. `doctor` shows which workspace was resolved; the region
  cannot be redirected the same way.
- `make test` exports the repository's `.env`, whose `LLM_PROVIDER=claude_cli`
  would route every provider test built against an HTTP mock at the CLI. The
  test fixture detaches the aliases as it already detached every `CASCADE_`
  variable, and the one subprocess test points `CASCADE_ENV_FILE` at a path
  that does not exist.

## Verified by

`tests/unit/test_config.py` (aliases bind, normalise, keep precedence, read
from `.env`, treat an empty value as absent, and the footprint is one
region); `tests/unit/test_llm_providers.py` (every class implements the
interface, aliases build the same class, the CLI keeps its namespace, request
ids survive the recording and the replay); `tests/unit/test_tracing.py` (the
call log writes one object per event and degrades rather than failing a run);
`tests/unit/test_aws_check.py` (every client is built for the configured
region, failures are per check, the command exits 3). ruff, black, mypy strict
and the offline suite are green; `make infra-check` runs the Terraform gates
under the pinned 1.16.3 in Docker. Measured values are in the M17 build-log
entry in `CLAUDE.md`.
