# ADR-0050 — Guardrails are measured against the study before they are applied to it

- **Status:** accepted; the audit ships, the filter does not
- **Milestone:** M15
- **Supersedes the "deferred" half of ADR-0030. Its Knowledge Bases rejection
  stands unchanged**

## Context

ADR-0030 deferred Bedrock Guardrails on two grounds:

1. **No path through the one door.** The installed SDK's Bedrock client has no
   guardrail parameter, so applying one would mean either an undocumented
   header or the standalone `ApplyGuardrail` API through boto3 — a second
   model-adjacent call path outside `LLMClient`, which invariant 5 exists to
   prevent.
2. **An unmeasured confound.** "A guardrail that alters a single decomposition
   changes the experiment."

That record also said what would settle it, and the sentence is worth quoting
because this decision is its consequence rather than a change of mind:

> Guardrails become worth adopting if the SDK's Bedrock client grows a
> guardrail parameter, *and* a measurement shows it alters no compiled graph on
> the 180 scenarios — at which point it is a safety control with no effect on
> the study. The natural shape is a post-hoc audit over stored graphs rather
> than a filter in the call path.

Two things changed. The SDK's Bedrock client still has no guardrail parameter —
re-measured here, and a case-insensitive search of the installed `anthropic`
0.121.0 for "guardrail" still returns **0 files**, so ADR-0030's first ground
stands unchanged for the *call path*. But ADR-0047 has since drawn the line
ADR-0030 left implicit: a managed service is admissible where it cannot alter
what the system is measuring, and inadmissible where it can. A guardrail in the
compile path alters the graph. A guardrail *asked about* a stored graph alters
nothing.

And the second ground was never an argument for waiting. Nothing about a
confound improves by going unmeasured.

## Decision

**Ship the measurement. Do not ship the filter.**

`cascade/eval/guardrails.py` is a post-hoc audit. It reads compiled graphs as
`cascade_eval`, renders each one's free text, asks a configured guardrail what
it would have done, and reports. It runs after compilation, never during it. It
returns no graph. It has no return path by which an edited decomposition could
reach the store, and the role it reads under holds `SELECT` on `causal_graphs`
and nothing else (migration 008), so that is a grant rather than a promise —
the mechanism ADR-0005 established for the labels.

**A flagged graph is reported as a confound, not as a safety win.** The audit's
own vocabulary says so: `GuardrailAudit.confound`, and a headline that names
the consequence for the experiment rather than the threat caught.

**Four verdicts, because three of them are a zero.** `not_assessed` (no
guardrail configured, or the service answered for none), `incomplete` (some
graphs answered for, some not), `clear` (every graph read was assessed and none
altered), `confound` (at least one would be altered). Only `clear` is a result;
the other three zeros are not.

**Admission condition, stated in advance.** A guardrail may enter the compile
path only if a `clear` audit over the full sealed registry says it alters no
graph — and even then the audit is re-run after any guardrail revision, because
a guardrail is a mutable remote object and "it was clear in September" is not a
property of the thing running in October.

**Where the egress belongs.** The `ApplyGuardrail` call is behind a
single-method `GuardrailScreen` protocol, and `BedrockGuardrail` is the only
part of the audit that touches boto3. Its final home is beside
`RecordedReranker` in `cascade/llm/client.py`, for the reason ADR-0047 gave for
putting the reranker there: ADR-0030's first ground is a real one, and the
answer to "a second model-adjacent path outside the one door" is to put it
inside the door, not in a different package. That move is an import change and
nothing else. It is not done in this record because it also implies record and
replay around the call, which is a decision about the LLM cache's domain and
belongs with whoever owns it.

Terraform's `modules/guardrails` gains the guardrail itself — until now the
module's name referred only to the organization's service control policies —
and publishes `guardrail.id` and `guardrail.version` in the shape
`providers.bedrock` reads. Created but unversioned and unattached by default,
on the same principle as the SCPs in the same module: reviewable before it can
affect anything.

## Rationale

**A safety control that has never been measured against the system it protects
is a liability, not a control.** It is claimed in a design document and untested
in the one place it would act. Worse, an unmeasured guardrail has a failure
mode that looks exactly like success: it intervenes on four graphs, those
scenarios quietly carry a different decomposition from the other 176, and the
ablation deltas that follow are measured across an arm nobody recorded. Nothing
downstream could detect it — the same shape as the two inert ablation factors
ADR-0025 found, where "a small interval straddling zero" was indistinguishable
from an honest null.

**Measuring it post hoc costs nothing and resolves the confound either way.**
One `ApplyGuardrail` call per scenario over the sealed registry. No graph is
recompiled, no forecast is re-run, no recording is invalidated, and the study's
cache domain is untouched because a guardrail call is not a model call and
never enters the prompt. Both outcomes are publishable: `clear` licenses the
control, `confound` names the scenarios and the policies that fired and
explains precisely why the control cannot be applied to this study as it
stands.

**A filter in the compile path would be unmeasurable by construction.** This is
the argument that decides the shape. If the guardrail runs during
`compile build`, the graph it altered is the only graph that ever existed for
that scenario — there is no unfiltered counterpart to diff, no record of what
was removed, and the graph hash is the hash of the filtered thing. The question
"did this change the experiment?" would have no answer, not because it is hard
but because the evidence was destroyed at the moment the filter acted. The
audit exists in the one order in which the question can be asked at all:
compile first, measure second.

**Why the three zeros are kept apart in code rather than in prose.** M8's first
cost ledger reported `difference: 0.0000% — cost ledger reconciles within
tolerance` over $0.00 against $0.00, and `LedgerReconciliation.vacuous` was
added because a gate that compares two zeros cannot fail. The same bug is
available here with the sign flipped: an audit that could not reach AWS flags
no graph, and "0 flagged" would read as *this guardrail is safe to apply*. That
is a stronger and more dangerous claim than a cost figure, because someone
would act on it. So `not_assessed` and `clear` are different values, the
headline always prints the denominator beside the count, and a service error is
recorded per graph rather than raised — a throttle on graph 12 must not discard
the 179 measurable answers, and must not be rounded into them either.

**Why the graph's prose and not the tool-use JSON.** Only the graph survives
compilation; the prompt and the raw completion do not. The audit screens every
actor name, objective and constraint and every factor name, which is a superset
of the prose the model emitted inside the tool call. A guardrail that flags
nothing over the superset would have flagged nothing over the subset, so the
approximation errs toward *finding* a confound — the conservative direction
when the question is whether a control is safe to introduce. What it cannot
screen is the retrieved evidence, which is not stored with the graph. That gap
is stated in the module and not closed.

**The request shape was read, not remembered.** `ApplyGuardrail` was verified
against the installed botocore service model for `bedrock-runtime`
(botocore 1.43.98): `POST /guardrail/{guardrailIdentifier}/version/
{guardrailVersion}/apply`, four required members — `guardrailIdentifier`,
`guardrailVersion`, `source` ∈ {`INPUT`, `OUTPUT`}, `content` (a list of tagged
unions whose text arm is `{"text": {"text": …}}`) — and a response carrying
`action` ∈ {`NONE`, `GUARDRAIL_INTERVENED`}, `actionReason`, `outputs`,
`assessments` and `usage`. An `action` outside those two values raises rather
than reading as *not flagged*, because a third value folded into the safe
branch would understate a confound. The Terraform resources were read the same
way, from the provider's own documentation at the version the lock file pins
(`hashicorp/aws` 6.65.0): `aws_bedrock_guardrail` exports `guardrail_id`,
`guardrail_arn` and `version`, and `aws_bedrock_guardrail_version` exports
`version`.

## Consequences

- **The audit can report a result the project does not want.** A `confound`
  verdict means the guardrail cannot be applied to this study without restating
  what it is comparing. That is the outcome the measurement exists to be
  capable of producing, and nothing about the guardrail is to be relaxed to
  avoid it — §1's rule applies here as it does to a Brier.
- **A `clear` verdict is narrower than it sounds.** It says this guardrail, at
  this version, alters none of these graphs. It says nothing about a later
  revision, about the agents' 4.2M decisions, or about the baselines. Applying
  it anywhere else is a separate measurement.
- **Two `guardrail` nouns now live in the repository.** The Terraform module
  holds both the organization's service control policies and the Bedrock
  guardrail; the README and the module comments say which is which. They were
  not split because they answer the same question — what this account is not
  allowed to do — at two layers.
- **`cascade doctor` gains nothing.** A configured guardrail is not a
  readiness precondition: the study runs without one, and a `doctor` that
  failed on an absent guardrail would make an optional control mandatory by
  accident.
- **Nothing is spent to find this out.** An `ApplyGuardrail` call is priced by
  the guardrail's own policy evaluation, not by tokens through the model, and
  it does not touch the meter's phase ceilings. It is therefore also outside
  the cost ledger, which is stated rather than hidden.

## What would change this

The filter becomes admissible when **all three** hold: the SDK's Bedrock client
grows a guardrail parameter, so the call goes through the one door rather than
around it; the egress has moved into `cascade/llm/` with record and replay, so
a guardrail's answer is reproducible the way a model's is; and a `clear` audit
over the full sealed registry says it alters no graph. Until then a guardrail
is something this study measures, not something it applies.

If a guardrail is ever placed in front of the *agents* rather than the
compiler, this record does not cover it. The audit's subject is 180 stored
graphs; the agents' decisions are 4.2M and are not stored as text, so the same
question would need a different measurement and its own record.

## Verified by

`tests/unit/test_eval_guardrails.py` — **27 tests**, offline, against a stub
client shaped from the installed botocore service model. The three zeros are
asserted to render differently from one another; an intervention is asserted to
report as a confound; the graphs are asserted unchanged by the audit that
measures them; the module is asserted to contain no SQL that writes, and that
detector is asserted against a synthetic violation.

`infra/terraform/modules/guardrails` was validated against the real
`hashicorp/aws` 6.65.0 schema — the version the platform env's lock file pins —
and reports `Success! The configuration is valid.` The check was confirmed
capable of failing: renaming `guardrail_id` to a non-existent attribute
produces `Unsupported attribute`. `terraform fmt -check -recursive` is clean.

Two things could **not** be run here and are recorded as gaps rather than
passed over. `infra/terraform/envs/platform/tests/guardrails.tftest.hcl` gained
five runs and thirteen assertions that have never executed: `terraform test`
needs core ≥ 1.6 and the installed binary is 1.5.7, against a module that
requires ≥ 1.10. Its HCL parses and is canonically formatted, which is a syntax
check and not a behaviour one. And the platform root cannot be validated at
1.5.7 at all, for a reason that predates this change — `restore_targets` uses a
cross-variable `validation` condition, which is a 1.9 language feature.

The measurement itself has **not been run**: it needs an AWS account and a
deployed guardrail, and neither exists in this environment. No audit number is
claimed anywhere in this record.
