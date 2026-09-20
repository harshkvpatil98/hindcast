# ADR-0048 — A second record of spend, written by someone else

- **Status:** accepted
- **Milestone:** M15
- **Corrects a defect in this build: §12.4's gate has one record of spend and calls it two**

## Context

M8's criterion 3 — "cost ledger reconciles with Langfuse within 2%" — has
exited 3 since it was written, and the build log records why the first
implementation was worse than a failure:

> A first implementation reported `difference: 0.0000% — cost ledger
> reconciles within tolerance` over 452,328 model calls costing $0.00 against
> a Langfuse total of $0.00.

`LedgerReconciliation.vacuous` fixed the zero-against-zero half. It did not
touch the other half, which was not noticed at the time: **there is
effectively one record.** `ledger.py`'s own docstring claimed two —

> Two independent records of the same spend exist by construction. The
> **meter** prices every call from the pinned per-token table … The **tracer**
> emits one Langfuse generation per call carrying the same cost. They are
> produced by different code from the same event, which is what makes
> agreement evidence rather than tautology.

— and "different code from the same event" is doing more work there than it
can. Both records are written by *this process*, from the same in-memory
`Usage` object, inside the same `LLMClient` call. A call the client never made
is absent from both. A usage figure the SDK reported wrongly is wrong in both.
A phase that ran entirely from the recording cache books nothing in either.
Their agreement tests whether `tracing.py` drifted from `meter.py`; it cannot
test whether the study's calls happened or cost what it says.

§12.4 makes this gate the thing that guards the study's cost claim. A gate
that compares a number to itself is the same defect M8 already found, at one
remove.

## Decision

**1. Independence is a property of who wrote the record.** Every source now
declares `written_by`, and only a record written outside this process counts as
independent. Langfuse stays, marked `"this process"`: it *corroborates* the
meter and is reported as doing exactly that.

**2. Bedrock model-invocation logging is the first independent source.**
`cascade/trace/aws_spend.py` reads it from a CloudWatch Logs group named by
`observability.aws_invocation_log_group`. AWS writes those records from the
service side; this process only ever reads them.

**3. The record is compared on tokens, not on dollars.** A model-invocation log
entry carries `input.inputTokenCount` and `output.outputTokenCount` and no
price. Pricing those tokens from `llm/meter.py`'s table to manufacture a dollar
figure would compare the price table with itself — the same argument that keeps
`local_spend` from re-pricing the run ledger — so `PhaseSpend.total_usd` is
`None` for this source and `SourceComparison.basis` is `"tokens"`.

**4. Three states, kept apart, and the verdict names all of them.**
*not configured* (never asked — does not block, because a deployment with no
AWS account still has §12.4's gate), *unreachable* (asked and missed — blocks),
and a measured total. None of them is `0`. `reconciled` is conjunctive: at
least one source compared, **every** source that was asked reachable, nothing
vacuous, all of them within tolerance.

**5. `independently_verified` is a separate property from `reconciled`.**
§12.4's criterion names Langfuse, and Langfuse is written here.

## Rationale

**A reconciliation that cannot say which sources it compared is a gate that
cannot fail.** Adding a source introduces a failure the single-source version
could not have: one record agrees, another is never read, and the output says
"reconciles within tolerance". That reads stronger than the M8 version while
checking less. So the shape is N comparisons rather than a `remote`, every one
of them named in `compared`, `unreachable` or `not_configured`, and
`reconciled` refuses while anything that was asked went unanswered.

**Unreachable blocks; unconfigured does not.** The asymmetry is deliberate and
is the one place this could have been got wrong in either direction. Making an
unconfigured source block would make §12.4's criterion unmeetable for every
deployment without an AWS account — relaxing nothing, but breaking a criterion
that is being *strengthened*. Letting an unreachable source pass would let a
source drop out of the gate without changing its verdict, which is the whole
failure being fixed. A log group configured without a region is read as
**unreachable**, not unconfigured: someone who meant to add the second record
and mistyped one field must not get a green gate that checked one fewer source
than they believe.

**Absence is never zero.** An unset log group, a missing region, absent
credentials, a log group that does not exist, and a `boto3` that is not
installed all return an explicitly unread `SourceReading` carrying the reason.
A zero would compare as a 100% discrepancy and fail the gate for an
observability outage, inverting the rule that observability must never fail a
run; or, against an empty ledger, it would agree perfectly and pass.

**Routing explicit, identity ambient (ADR-0028).** The region is passed from
configuration to both the client constructor and its `Config`, and
`ignore_configured_endpoint_urls` closes the endpoint door, because botocore
reads `AWS_ENDPOINT_URL` and `AWS_ENDPOINT_URL_CLOUDWATCHLOGS`. Without it a
variable in a developer's shell would decide which account's record answers
the question "was the study's spend real".

**AWS Cost Explorer was the other candidate and was not adopted.** It is
billing-grade, written by AWS, and denominated in the currency §12.4's
threshold is stated in — but it aggregates at account-and-day granularity,
includes every other service in the account, and lags by up to a day. It cannot
be scoped to a phase, which is the unit §12.4 reconciles. Invocation logging is
per call, which is the granularity the run ledger has. Cost Explorer remains
the right third source for a whole-study figure and would slot in as another
`SourceReading` with `basis="usd"` without changing anything here.

## Consequences

**The constraint that matters, and it blocks M8's criterion 3 for a new
reason.** AWS's own documentation for model invocation logging states:

> Model invocation logging is only supported for calls made through the
> `bedrock-runtime` endpoint. … Calls made through other endpoints, such as the
> same APIs on `bedrock-mantle`, are not currently captured by invocation
> logging.

ADR-0028 routes this project's `bedrock` provider at
`https://bedrock-mantle.{region}.api.aws/anthropic`. **Model-invocation logging
therefore does not cover the endpoint this project's Bedrock provider uses**,
and the `anthropic`, `aws` and `claude_code` providers are not Bedrock at all
and write no invocation log either. As things stand, this source would read
*empty* for every provider the study can currently run.

An empty read is a measurement, not an absence, and it is refused from both
directions: against a ledger with calls it is a 100% discrepancy, and against
an empty one it is vacuous. Neither passes. The detail line says which of the
two plausible causes to check rather than leaving a reader to infer it from a
zero. So criterion 3 stays blocked — and now names exactly what would unblock
it, which is more than it could do before.

**`cascade trace cost` needs its wording updated, and until it is, one message
is wrong.** Measured against the unmodified command with `reconcile` patched:
an agreeing AWS record exits **0**; an unconfigured one exits **0**; an
unreachable one exits **3** — the correct refusal — but prints "ledger and
Langfuse differ by 0.0000%, above the 2% tolerance", because the command has
one source's vocabulary. The refusal is right and the diagnosis is not.
`LedgerReconciliation.verdict` renders the correct sentence for every outcome
and is what the command should print.

**`PhaseSpend.total_usd` is now `Decimal | None`.** A record that states no
price says so rather than claiming zero. `LedgerReconciliation.remote` survives
as a property returning Langfuse's total, so the M8-era caller keeps working.

**Verified without an AWS account.** The record shape is AWS's documented log
entry, asserted through a pure accumulator using the documentation's own
example counts (25 input, 150 output). The API shape is driven through a real
`boto3` CloudWatch Logs client under `botocore.stub.Stubber`, so botocore
validates the operation, its parameter names and their types against its own
service model before any stub answers — a hand-built double would accept a call
AWS rejects. Routing is asserted against four decoy environment variables, and
every guard is checked against a synthetic violation. Measured: 30 tests in
`tests/unit/test_aws_spend.py` and 37 in `tests/unit/test_trace_ledger.py`,
which held 14 before; **2,148 offline tests pass, 1 skipped**, with mypy strict
clean over 120 source files.

## What would change this

This source starts carrying real numbers the moment anything in the study
reaches `bedrock-runtime` — a provider added for it, or AWS extending
invocation logging to the mantle endpoint, which the "not currently" in their
own note leaves open. Nothing in `aws_spend.py` changes when that happens; the
log group fills and the gate compares it.

If the study never runs on Bedrock, the honest outcome is that §12.4's gate is
corroboration and the report says so — `independently_verified` exists to make
that statement checkable rather than rhetorical. Closing it properly would then
mean a different independent record: Cost Explorer for a whole-study dollar
figure, or the provider's own usage export, each of which is another
`SourceReading` and no change to the reconciliation.

If a future source records dollars *and* is written elsewhere, it should be
added with `basis="usd"` and `written_by` naming its writer — and the
temptation to price a token-only source from the meter's own table in order to
get a dollar column should be refused every time it comes up. It is the same
mistake as reconciling the ledger by re-pricing it.
