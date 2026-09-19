# ADR-0028 — Four model providers behind the one call site

- **Status:** accepted — approved by the project owner, 2026-09-18 (stack change, CLAUDE.md §3)
- **Milestone:** M10
- **Records a choice the spec left open, and one constraint it could not have known**

## Context

The spec pins Claude Haiku 4.5 and Sonnet 4.6 and reaches them through the
Anthropic API. Every live acceptance number from M4 to M8 has been blocked on
one long-lived credential, `CASCADE_ANTHROPIC_API_KEY`. The project is now
meant to run on AWS, and there are **two** AWS surfaces for Claude, which are
not interchangeable. Verified against the SDK's platform-availability table,
not recalled:

| | Claude Platform on AWS (`AnthropicAWS`) | Amazon Bedrock (`AnthropicBedrockMantle`) |
|---|---|---|
| operated by | Anthropic, through AWS | AWS (partner) |
| authentication | SigV4 / IAM | SigV4 / IAM |
| **Message Batches** | **yes** | **no** |
| model id on the wire | bare (`claude-haiku-4-5-…`) | `anthropic.`-prefixed |
| pricing | its own listing | partner pricing, separate |

## Decision

1. **Four providers, one door.** `llm.provider` selects `anthropic`, `aws`,
   `bedrock` or `claude_code` (ADR-0031). The three SDK client classes all
   ship inside the one `anthropic` package, so `cascade/llm/client.py` is
   still the only importer and invariant 5's test needs no exemption.
   `cascade/llm/providers.py` describes the providers and is pure.
2. **Bedrock cannot carry a batched phase.** `complete_batch` raises
   `ProviderNotReady` (exit 3) once there is anything to submit — before any
   spend. A wave served entirely from recordings is allowed, since it costs
   nothing on any provider.
3. **Routing is explicit; identity is ambient.** Region, workspace and
   endpoint are passed to the SDK from `Settings` on every construction.
   Credentials come from the standard AWS chain (a task or instance role in
   the cloud, a profile locally), so no credential appears in configuration.
4. **Each provider is priced from its own table**, by the *logical* model the
   request named, and the AWS tables ship empty. A provider that cannot be
   routed or priced is refused when the client is constructed, listing every
   problem at once.

## Rationale

**The batch constraint is load-bearing, not a feature gap.** The simulate
phase is inside its $240 ceiling only at the 50% batch rate — $126 batched,
$252 not (ADR-0020). A per-call fallback on Bedrock would breach the ceiling
the phase was sized against. Refusing to fall back is the only response that
keeps the budget honest; the error names the two providers that can record
the phase, and ADR-0029 lets those recordings replay through Bedrock.

**Explicit routing closes a silent-default hole.** Both SDK clients consult
`AWS_REGION`, `ANTHROPIC_AWS_BASE_URL` and `ANTHROPIC_BEDROCK_MANTLE_BASE_URL`
from the ambient environment *before* the region. Left unset, a variable in a
developer's shell would decide where the study's spend lands, without the
choice appearing in any reviewed file — the M0 lesson about variables that
bind silently. The endpoint templates are reproduced in `providers.py` so the
SDK's fallback is never reached, and a test asserts they agree with what the
installed SDK derives, so the copy cannot drift.

**Pricing by the logical model.** The meter used to price by the model string
the *response* echoed. Bedrock echoes a prefixed id that is in no price table,
so that would have raised rather than booked — but a provider echoing a
*different known* model would have booked the wrong rate silently. The price
of a call is a property of what was asked for.

**Empty price tables, refused at construction.** Rates are read from the live
pricing pages when a provider is enabled, never transcribed from memory: a
wrong rate is a ledger error that surfaces only at the M8 reconciliation.

## Cost of being wrong

If AWS changes an endpoint format, the reproduced template sends traffic to
the wrong host and the first call fails loudly — the drift test catches it
when the SDK is upgraded. `providers.<p>.base_url` overrides it without a code
change. If a Bedrock deployment publishes a model id other than
`anthropic.<logical>`, `providers.bedrock.model_ids` maps it.

## Verified by

`tests/unit/test_llm_providers.py` — the real SDK against a mock transport
with fake credentials in the environment: the SigV4 signature, the regional
host winning over ambient decoys, the wire model id, the workspace header,
pricing from the partner table, the batch refusal with zero requests sent,
readiness listing every problem, replay needing no configuration. Five of its
guards were checked by deliberately breaking the behaviour each protects.
