# ADR-0051 — Bedrock Agents rejected: the Action Group is admissible, the loop is not

- **Status:** accepted
- **Milestone:** M15
- **Applies ADR-0047's line to a second managed service, and reaches the
  opposite verdict for a reason ADR-0047 predicts. Constrained by ADR-0049 (the
  tool surface), ADR-0028 (no Bedrock batch) and ADR-0020 (the batch wavefront)**

## Context

M15 built `ToolUsingAgents`: a bounded loop that runs through `LLMClient`,
calls `lookup_evidence` and `recall` against a `ToolBelt` with the scenario's
cutoff bound into it, and returns one action. Every turn is one
`client.complete()`, so every turn is content-addressed, cached, metered in
exact `Decimal` and traced, exactly as the single-call arm's one turn is.

AWS offers the managed equivalent. A **Bedrock Agent** holds an **Action
Group** — an OpenAPI schema describing the operations the model may call —
decides which operation to invoke, and runs the loop itself, either calling a
Lambda or handing the call back to the caller. The loop that `ToolUsingAgents`
is would become AWS's.

This project has drawn this line twice before and in both directions. ADR-0030
rejected Bedrock Knowledge Bases because a managed retriever *replaces* the
`published_at < as_of` predicate with an optional request parameter. ADR-0047
admitted Bedrock Rerank because a reranker *permutes a set the database already
filtered*, and cannot be handed `as_of` at all. The line was never "managed is
bad"; it was where the service sits relative to the guarantee. This record asks
where an Action Group sits, and the answer turns out to be different for the
*schema* than for the *loop*.

## Decision

**Rejected for the study loop. The Action Group schema is built anyway, and
ships as the evidence.**

1. `cascade/llm/bedrock_agent.py` generates the Action Group's OpenAPI 3.0.0
   document from the same Pydantic argument models `ToolSession.execute`
   validates against. It is pure: no boto3 client, no session, nothing sent.
   No code was added to `cascade/llm/client.py`, because nothing here egresses.
2. **`RETURN_CONTROL` is the only executor the module names.** A Lambda
   executor is not refused with an argument check; it is absent, on the
   precedent `LookupEvidenceArgs` set for `as_of` itself.
3. Nothing is wired into a run, no configuration field was added, and no
   Terraform module was written. There is no Bedrock Agent arm.

## Rationale

### `as_of` is the crux, and the two executors give two different answers

**Under a Lambda executor the guarantee is strictly weaker, and the weakening
is the kind this project has twice decided it cannot accept.** Verified against
the Bedrock user guide's Lambda input-event contract: the event carries
`parameters` and `requestBody` (written by the model) alongside
`sessionAttributes` and `promptSessionAttributes` (string maps). The cutoff has
no other way in. So `as_of` would travel from this process, through a service
this project does not own, into a `map<string,string>`, and be read back out of
an event payload — where a missing key is whatever the Lambda's `.get()`
returns. Invariant 1 says `as_of` is never defaulted and that a missing one is
a `TypeError` at the Python boundary; a string map has no signature in which
the cutoff can be required. Three further facts make it worse rather than
better:

- `promptSessionAttributes` is rendered *into the orchestration prompt* through
  the `$prompt_session_attributes$` placeholder. A cutoff put there is a cutoff
  the model reads — which is not merely a leak risk but a different experiment,
  since `ToolBelt` holds the cutoff privately precisely so an agent cannot
  reason about what it is being kept from.
- The Lambda's *response* may itself set both maps. The channel is writable
  from inside the loop, so "the caller bound it" stops being true after turn one.
- The chain is caller → Bedrock → Lambda → Chronofence. ADR-0030 rejected a
  knowledge base for turning "a database-enforced invariant into a caller's
  promise and a vendor's implementation". This is that, with an extra hop.

**Under `RETURN_CONTROL` the guarantee survives intact, and that is why it is
the only executor offered.** Bedrock invokes no Lambda: it streams a
`returnControl` event carrying `invocationId` and `invocationInputs`, *this*
process executes the call and returns results in the next request's
`sessionState`. So the executor is still `ToolSession.execute`, the belt still
holds the cutoff, and the payload is still validated into a closed model.
`bedrock_agent.session_state()` takes no parameter for a session attribute at
all, so there is no expression in this module that puts a cutoff in either map.

### …but one thing about `as_of` degrades under *both*, and it is undocumented

`extra="forbid"` is what makes a model-supplied `as_of` a *validation error*
rather than a *dropped key*. ADR-0049 calls this "the barrier that matters
most", because a silent drop is indistinguishable, from both sides, from having
been honoured. Its only expression in OpenAPI is `additionalProperties: false`
on an object schema — which is why the generated operations are POSTs with
request bodies rather than GETs with parameters: a `parameters` list has no
object to hang the closure on. The HTTP verb here is decided by the invariant.

The document carries the member. What Bedrock *does* with it is not documented.
The user guide describes the parser only as "a subset of the OpenAPI 3.0
specification" and enumerates exactly one exclusion (`enum`).
`additionalProperties` is named neither as supported nor as unsupported, and
there is no AWS account here to ask. So under a Bedrock Agent the strongest
barrier of the three becomes an undocumented behaviour of a parser nobody in
this project can inspect.

Two things follow, and both are in the code rather than in this paragraph.
`invocation_arguments()` collects arguments from **both** the `parameters` list
and the request body — discarding the loose list would be this module
performing the very silent drop it refuses the schema for — and hands the whole
payload to the one validator. `tests/unit/test_bedrock_agent.py` then drives a
real `ToolBelt` with an `as_of` that Bedrock has passed through anyway, and
asserts the answer is a refusal that spends one turn, with neither the rejected
key nor the cutoff echoed back. That restores the barrier *in this process*,
which is the only place it can be restored, and it is the reason a Lambda
executor is unavailable rather than merely discouraged.

### Record and replay do not survive, and this is what actually decides it

M8's criterion is a byte-identical event-log hash across processes, and
ADR-0049's claim that the tool arm is replayable rests on each turn being one
`LLMClient.complete()`: content-addressed on the canonical request, served from
disk on replay, exit 4 on a miss.

Under `InvokeAgent` the request this process sends is
`(agentId, agentAliasId, sessionId, inputText, sessionState)`. The prompt the
model answers is composed by Bedrock from the agent's stored `instruction`, its
orchestration template, the action-group schemas and the session attributes.
**A request this process did not compose cannot be content-addressed**, and
that is not a gap to be engineered around: the whole cache is a function from
request bytes to response, and the bytes are not ours. Worse for replay, an
agent keeps conversation history server-side under `sessionId`, so the *n*-th
turn is not a pure function of anything this process holds — it is a function
of state in AWS, under an agent version and an alias that can be repointed
without the id changing. ADR-0047 could admit a managed reranker because its
response is a pure function of (model id, top-k, query, ordered bodies), all of
which this process has. Here, it does not have them.

### Metering degrades from exact to conditional, in the shape M8 named

`CostMeter` books exact `Decimal` from the `Usage` on every response. Verified
against the `bedrock-agent-runtime` service model: `InvokeAgent` returns an
event stream whose token counts live only in the **trace** —
`orchestrationTrace.modelInvocationOutput.metadata.usage`, with `inputTokens`
and `outputTokens` — which requires `enableTrace: true`. A debugging flag that,
left off, silently zeroes the meter is precisely the defect M8's criterion 3
exists to catch: a gate that compares two zeros and passes. ADR-0048 has
already found that this project's two spend records corroborate rather than
verify; adding a third whose numbers exist only under a debug flag does not
help.

Two further costs, both measured rather than assumed:

- **Prompt caching has no expression on this path.** ADR-0001 requires the
  static prefix to clear the provider's 4,096-token cache floor and ADR-0019
  put the evidence block in the prefix specifically so it would; M5 measured
  that prefix at ~4,652 tokens. `CachePointBlock` exists in the
  `bedrock-agent` service model, but is reachable only from the prompt-
  management and flow shapes (`TextPromptTemplateConfiguration`,
  `SystemContentBlock`, `ContentBlock`, `Tool`) — **not** from
  `PromptConfiguration`, which is the agent's own orchestration configuration.
  There is nowhere to mark a prefix boundary, and the prefix is Bedrock's
  template anyway.
- **Neither service has a batch operation.** Verified by enumerating all 75
  `bedrock-agent` and 35 `bedrock-agent-runtime` operations: no name contains
  "Batch". ADR-0028 already refuses Bedrock for a batched phase because §12.1
  prices simulate at $126 batched against $252 unbatched and a $240 ceiling.
  ADR-0049 established that a tool loop cannot ride ADR-0020's 24-batch shape
  regardless, so this is not a *new* loss — but it does mean the arm could
  never grow past ablation scale even if everything above were solved.

### What *would* survive, stated so the rejection is not a stacked deck

`temperature`, `maximumLength`, `topP`, `topK` and `stopSequences` **can** be
pinned, through `promptOverrideConfiguration.promptConfigurations[]
.inferenceConfiguration` with `promptType: ORCHESTRATION`; the API reference
marks `inferenceConfiguration` as independent of `promptCreationMode`, so
pinning them does not force replacing the base prompt template. That is
genuinely better than ADR-0031's Claude Code CLI, which cannot set either and
is keyed into its own cache namespace for that reason. Routing and identity
would work the same way the two existing boto3 adapters already do (ADR-0028).
Guardrails would attach natively, where ADR-0030 had to route around them.

The objection is not that Bedrock Agents are poorly built. It is that this
study's validity rests on properties — a cutoff bound where the model cannot
reach it, a request whose bytes this process composed, a ledger that cannot
compare zero against zero — that a service running the loop on our behalf is
not in a position to provide.

### One shape worth naming, if this is ever revisited

A Bedrock Agent has **one `instruction` per agent resource**, while this study
has 14 actors × 165 scored scenarios = 2,310 distinct personas, each a separate
system block that ADR-0019 makes the cacheable part of the prefix. Creating
2,310 agent resources is not a design. Putting the persona in `inputText` makes
it a user message rather than a system block, and ADR-0045 measured that this
distinction is not cosmetic: an injection that impersonated the system frame
was obeyed on 20 of 30 scenarios and on 0 of 30 once the frame was made
unforgeable. The only shape that fits is **`InvokeInlineAgent`**, which takes
`foundationModel`, `instruction` and `actionGroups` per call and creates no
persistent resource — and therefore needs no Terraform, which is why none was
written here.

## Consequences

- **`cascade/llm/bedrock_agent.py` exists, is tested, and is wired into
  nothing.** It is not scaffolding: it is what makes every claim above
  checkable against an artifact instead of a recollection, and it is the answer
  to "could we?" for whoever asks next. 27 offline tests; 8 of 8 deliberately
  broken behaviours failed at least one of them.
- **The schema is generated, never written beside the models.** A member the
  translation cannot carry into OpenAPI 3.0.0 — `$defs`, `anyOf` for an
  optional field, `enum`, `exclusiveMinimum` as a number — raises
  `UnsupportedByBedrock` naming itself, rather than being emitted as a document
  whose meaning is a guess, or dropped. A schema that could express an instant
  raises `TimeParameterRefused`.
- **No configuration field was added and none is needed.** The generator takes
  the same `allow` list `kernel.tools.allow` already carries.
- **A defect in `cascade/sim/tools.py`, reported and not fixed here** (that
  file belongs to another owner). `_tool()` uses `model_json_schema()`, so each
  argument model's **class docstring becomes the `input_schema.description`
  sent to the model** — and those docstrings are written for a maintainer.
  `LookupEvidenceArgs`'s reads, in part, *"The lock is bound to `ToolBelt` from
  the scenario's own cutoff … `args.as_of` is a mypy error before it is a
  leak."* `_bad_arguments` goes to deliberate lengths not to echo the rejected
  key or the cutoff, on the grounds that "an error reading `as_of is not
  accepted` … would hand the agent, through the refusal itself, the fact the
  refusal exists to withhold" — and then the static prefix hands over the field
  name, the class names and the implementation. `tool_rules` already tells the
  model the cutoff exists and cannot be moved, so the *existence* is not the
  leak; the canonical key name and the internal vocabulary are. Measured with
  the project's own estimator: the two tool schemas are **453** tokens, of which
  **201** are these docstrings and pydantic's `title` echoes — 44%, carried in
  the cached prefix of every request the arm makes. `bedrock_agent.py` drops
  both and takes its operation descriptions from the tool definition's
  model-facing prose instead, so there is still exactly one source.
- **`docs/adr/README.md` and CLAUDE.md §7 do not yet index this record.** Both
  belong to the integrator; ADR-0046 was found unindexed at M15 and the count
  was stale in both directions, so this is worth doing rather than assuming.

## What would change this

If Bedrock Agents ever expose the composed prompt — the exact bytes sent to the
model for a turn — then the request becomes content-addressable and the largest
objection falls. Replay would still need the server-side conversation state to
be either absent or returnable, so `RETURN_CONTROL` with no server-side history
is the shape to look for.

If token usage moves out of the trace and onto the response, metering stops
depending on a debugging flag and the M8 ledger can be honest about this path.

If `additionalProperties: false` becomes documented as enforced, the schema's
own barrier is restored and a Lambda executor becomes arguable — though it
would still have to answer where `as_of` comes from, and the answer would still
be a string map.

And if a batch surface appears for agent invocations, the arithmetic in
ADR-0049 that confines any tool arm to ablation scale changes, at which point
this is worth re-deriving rather than re-reading.

## Verified by

Every AWS shape named above was read from the **installed botocore service
models** (`botocore` 1.43.98: `bedrock-agent` 2023-06-05, 75 operations;
`bedrock-agent-runtime` 2023-07-26, 35 operations) or from the AWS
documentation, and never from memory:

| Claim | Source |
|---|---|
| `ActionGroupExecutor` is `customControl: RETURN_CONTROL` **or** `lambda` | `bedrock-agent`, `ActionGroupExecutor` |
| `APISchema` carries an inline `payload` or an `s3` pointer | `bedrock-agent`, `APISchema` |
| `SessionState` has `sessionAttributes`, `promptSessionAttributes`, `invocationId`, `returnControlInvocationResults` | `bedrock-agent-runtime`, `SessionState` |
| `InvokeAgent` answers with an event stream (`completion: ResponseStream`) | `bedrock-agent-runtime`, `InvokeAgentResponse` |
| Token usage lives in the trace, as `Metadata.usage {inputTokens, outputTokens}` | `bedrock-agent-runtime`, `OrchestrationModelInvocationOutput` |
| No batch operation in either service | enumeration of all 110 operation names |
| `CachePointBlock` is unreachable from `PromptConfiguration` | enumeration of every `bedrock-agent` shape referencing it |
| `inferenceConfiguration` is independent of `promptCreationMode` | API reference, `PromptConfiguration` |
| The Lambda event carries the two attribute maps, and the response may set them | user guide, *Configure Lambda functions…* |
| `promptSessionAttributes` reaches the prompt via `$prompt_session_attributes$` | user guide, *Control agent session context* |
| `openapi` must be exactly `"3.0.0"`; the parser is "a subset"; `enum` unsupported | user guide, *Define OpenAPI schemas…* |
| `RETURN_CONTROL` returns `invocationInputs` + `invocationId`; results go back in `sessionState`; `inputText` is then ignored | user guide, *Return control to the agent developer* |

`tests/unit/test_bedrock_agent.py` asserts the generated members are members of
those service models, so the copy cannot drift when botocore is upgraded — the
same discipline `test_llm_providers.py` keeps for the SDK's endpoint templates.

**Not verified, and named as such:** whether Bedrock enforces
`additionalProperties: false`; whether `parameters` the schema does not declare
are forwarded to the executor or dropped; and the two AWS documents' own
disagreement about the `apiResult` response body key, which is shown as
`"TEXT"` in *Control agent session context* and as `"application/json"` in
*Return control to the agent developer*. This module uses the media type its
own schema declares. All three need an AWS account to settle, and none is
claimed here.
