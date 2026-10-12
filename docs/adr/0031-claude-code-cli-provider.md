# ADR-0031 — A Claude Code CLI provider, under its own cache namespace

- **Status:** accepted — requested by the project owner, 2026-09-18
- **Milestone:** M10
- **Records a deliberate departure from the pinned configuration, and its containment**

## Context

Every live number from M4 onward is blocked on a pay-as-you-go API key. The
project owner has a Claude subscription and asked for the pipeline to run on
it now, with a pay-as-you-go key added later. Two ways were considered:

* passing the subscription's OAuth token to the SDK — **rejected**: it
  impersonates Claude Code, and it is the path Anthropic blocks for
  third-party tools;
* running the official CLI in its documented headless mode, `claude -p`, on
  the owner's own machine — **adopted**.

The CLI is not the Messages API, and the differences were measured on this
machine, not assumed:

| | measured |
|---|---|
| request fields the CLI cannot set | `temperature`, `max_tokens` |
| harness context added per call | **448** input tokens for a ~45-token request, with tools, settings and MCP disabled |
| extended thinking | **on** by default (235 of 254 output tokens); **0** with `MAX_THINKING_TOKENS=0` |
| a forced tool | expressible as `--json-schema` structured output (2 internal turns) |
| `--bare` | unusable: it reads only `ANTHROPIC_API_KEY`, never the subscription login |

## Decision

A fourth provider, `claude_code`, served through `LLMClient` like the others —
cached, metered and traced — with:

1. **its own cache namespace.** Its responses do not answer the request their
   key describes, so they are keyed apart and can never be served as API
   recordings. Switching to pay-as-you-go later means `llm.provider:
   anthropic` and a key; it then records afresh with nothing inherited.
2. **isolation**: system prompt replaced not appended; `--tools ""`,
   `--strict-mcp-config`, `--setting-sources ""`, `--no-session-persistence`;
   session-attachment variables and API keys stripped; thinking off.
3. **a working-directory guard.** The CLI auto-loads every `CLAUDE.md` from
   its working directory upward. This repository's build contract is ~27k
   tokens, so a working directory inside it would inject the contract into
   every forecasting call. A directory below any `CLAUDE.md`, or inside the
   repository, is refused before the CLI runs.
4. **one request shape**, the only one this codebase sends: a system prompt,
   one user message with text content, at most one forced tool. Anything else
   is refused rather than flattened into a prompt.
5. **zero booked cost.** A subscription bills no tokens, so the ledger records
   $0 — true, and it makes the M8 reconciliation report "nothing to reconcile"
   rather than invent a spend. The CLI's notional list-price cost is kept on
   every recording.
6. **no batches**, so it cannot carry a batched phase (ADR-0028).

## What this provider can and cannot do

It can produce real-model output for the compile phase (~540 calls), the
memorization probe (180) and a small simulation sample. It cannot run the
36,000-run study: ~4.2M decisions, each a process start, is far beyond any
subscription's usage window. It is **local-only** — it authenticates as the
person logged in to Claude Code on this machine, which is not an identity that
belongs inside cloud infrastructure. The deployed design uses `aws` or
`bedrock`.

**Results produced through it are not the pinned configuration** and must be
labelled as such wherever they are shown.

## Cost of being wrong

If the isolation is weaker than measured, extra context reaches the model; the
448-token figure is the baseline to re-measure after a CLI upgrade. If a
usage limit is hit mid-phase, the client stops with a 429 rather than retrying
in a loop, and every phase resumes from its recorded calls.

## Verified by

`tests/unit/test_llm_providers.py` — through an injected runner, so no test
spends a subscription: the isolated invocation, structured output returned as
the caller's tool call, the separate namespace in both directions, zero cost,
no retry on 429, retry on 529, the working-directory guard, the environment.
The measurements above come from live `claude -p` calls on 2026-09-18,
Claude Code 2.1.277.
