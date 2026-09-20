"""A record of spend that this process did not write (spec §12.4; M15).

M8's criterion 3 has exited 3 since it was written, and the reason recorded
there is not only "no credential". It is that the two records §12.4 names --
the meter's run ledger and Langfuse -- are both produced by *this* process. A
call the client never made is absent from both; a usage figure the SDK reported
wrongly is wrong in both. Agreement between them catches a tracer that drifted
from the meter and nothing else.

**Bedrock model-invocation logging is written by AWS**, from the service side,
to a CloudWatch Logs group this process only ever reads. That is what makes it
independent, and it is the only property that matters here (ADR-0048).

What the record does and does not carry, read from AWS's own documented log
entry format rather than assumed:

* ``schemaType`` is ``"ModelInvocationLog"``, and ``input.inputTokenCount`` /
  ``output.outputTokenCount`` carry the token counts.
* **There is no price in it.** Pricing those tokens from `llm/meter.py`'s table
  to produce a dollar figure would compare the price table with itself and
  agree by construction -- the same argument that keeps `local_spend` from
  re-pricing the ledger. So this source's basis is ``"tokens"`` and its
  ``total_usd`` is ``None``, never ``Decimal(0)``.

Absence is never zero. An unset log group, a missing region, no credentials, a
log group that does not exist, and a `boto3` that is not installed all return a
:class:`~cascade.trace.ledger.SourceReading` that is explicitly *unreachable*
or *not configured*, carrying the reason. A silent zero is the failure M8
already found once: zero agreed with zero to 0.0000% over 452,328 model calls.

Routing is explicit and never ambient (ADR-0028). The region is passed from
``observability.aws_region`` and configured endpoint URLs are ignored, so a
stray ``AWS_REGION`` or ``AWS_ENDPOINT_URL`` in a shell cannot decide which
account's record the study's spend is checked against. Credentials stay
ambient, as everywhere else.

**And the record is scoped by time only.** A log group holds whatever it has
held since it was created, so a phase's ledger set against it unwindowed is a
comparison of two different quantities. The window is pushed into
`FilterLogEvents`; the *configuration* cannot be pushed anywhere, because a
model-invocation record names the model, the account and the identity and
nothing that names an ablation cell. Every reading therefore declares the slice
it covers and `ledger.at_scope` refuses one that covers more than was asked.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any

from cascade.config import Settings
from cascade.trace.ledger import PhaseSpend, ReadingStatus, Scope, SourceReading, epoch_ms

__all__ = [
    "AWS_INVOCATION_LOG_SOURCE",
    "MODEL_INVOCATION_SCHEMA_TYPE",
    "InvocationTotals",
    "accumulate",
    "invocation_log_reading",
    "logs_client",
]

#: The source's name, used for the reading, its total and the verdict alike.
AWS_INVOCATION_LOG_SOURCE = "bedrock invocation logs"

#: AWS stamps every model-invocation record with this. Anything else in the
#: group -- another subscription writing to the same place, a delivery error
#: notice -- is counted and excluded rather than parsed hopefully.
MODEL_INVOCATION_SCHEMA_TYPE = "ModelInvocationLog"

_PAGINATED_OPERATION = "filter_log_events"


@dataclass(frozen=True, slots=True)
class InvocationTotals:
    """What a log group's model-invocation records add up to.

    Every rejection is counted rather than dropped. A group whose records all
    failed to parse sums to zero exactly like an empty one, and the difference
    between "AWS logged nothing" and "this parser understood nothing" is the
    difference between a finding and a bug in this file.
    """

    records: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    models: tuple[str, ...] = ()
    malformed: int = 0
    foreign_schema: int = 0
    without_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def rejected(self) -> int:
        return self.malformed + self.foreign_schema


def accumulate(messages: Iterable[str]) -> InvocationTotals:
    """Sum model-invocation records, counting everything it refuses.

    Pure, so the shape AWS documents can be asserted against hand-written
    records without an account. Summation is over integers and so is
    independent of the order the pages arrived in; ``models`` is sorted before
    it leaves (invariant 7), because it reaches a report.

    A record missing its token counts is counted in ``without_tokens`` and
    contributes zero -- a failed invocation is still an invocation, and
    silently treating it as absent would understate ``records`` against a
    ledger that counted the call.
    """
    records = input_tokens = output_tokens = 0
    malformed = foreign = without_tokens = 0
    models: set[str] = set()

    for message in messages:
        try:
            record = json.loads(message)
        except (TypeError, ValueError):
            malformed += 1
            continue
        if not isinstance(record, dict):
            malformed += 1
            continue
        if record.get("schemaType") != MODEL_INVOCATION_SCHEMA_TYPE:
            foreign += 1
            continue

        record_input = record.get("input") or {}
        record_output = record.get("output") or {}
        if not isinstance(record_input, dict) or not isinstance(record_output, dict):
            malformed += 1
            continue
        raw_in = record_input.get("inputTokenCount")
        raw_out = record_output.get("outputTokenCount")
        try:
            counted_in = 0 if raw_in is None else int(raw_in)
            counted_out = 0 if raw_out is None else int(raw_out)
        except (TypeError, ValueError):
            malformed += 1
            continue

        records += 1
        input_tokens += counted_in
        output_tokens += counted_out
        if raw_in is None and raw_out is None:
            without_tokens += 1
        model = record.get("modelId")
        if isinstance(model, str) and model:
            models.add(model)

    return InvocationTotals(
        records=records,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        models=tuple(sorted(models)),
        malformed=malformed,
        foreign_schema=foreign,
        without_tokens=without_tokens,
    )


def logs_client(settings: Settings, *, timeout_s: float = 30.0) -> Any:
    """A CloudWatch Logs client routed from configuration, never from the shell.

    Preserves ADR-0028's rule that routing is explicit and identity ambient.
    The region is passed twice -- to the constructor and in the ``Config`` --
    because either alone still leaves an ambient value in the resolution chain,
    and ``ignore_configured_endpoint_urls`` closes the other door: botocore
    reads ``AWS_ENDPOINT_URL`` and ``AWS_ENDPOINT_URL_CLOUDWATCHLOGS``, so
    without it a variable in a developer's shell decides which service answers
    the question "was the study's spend real".
    """
    import boto3
    from botocore.config import Config

    region = settings.observability.aws_region
    if not region:
        raise ValueError(
            "observability.aws_region is not set; refusing to let the ambient "
            "environment choose the region (ADR-0028)"
        )
    return boto3.client(
        "logs",
        region_name=region,
        config=Config(
            region_name=region,
            ignore_configured_endpoint_urls=True,
            connect_timeout=timeout_s,
            read_timeout=timeout_s,
            retries={"max_attempts": 3, "mode": "standard"},
        ),
    )


def _window(scope: Scope) -> dict[str, Any]:
    """`FilterLogEvents` bounds for a half-open scope, translated not assumed.

    Preserves the property that makes two adjacent phases partition the spend
    rather than both claim a call on the boundary. Read from botocore's own
    service model for `FilterLogEvents` (botocore 1.43.98): *"Events with a
    timestamp before this time are not returned"* for ``startTime`` and
    *"Events with a timestamp later than this time are not returned"* for
    ``endTime`` -- a **closed** range on both ends, where :class:`Scope` and
    Langfuse's ``toTimestamp`` are half-open. So ``endTime`` is the last
    millisecond before ``until``, and the boundary event is counted once, in
    the window that comes after it.
    """
    bounds: dict[str, Any] = {}
    if scope.since is not None:
        bounds["startTime"] = epoch_ms(scope.since)
    if scope.until is not None:
        bounds["endTime"] = epoch_ms(scope.until) - 1
    return bounds


def _messages(pages: Iterable[Any]) -> Iterator[str]:
    """Every log event's message, page by page, in the order AWS returned them.

    Order does not affect the totals -- they are integer sums -- but the walk
    is over lists rather than mappings, so nothing here reintroduces the
    nondeterminism invariant 7 guards.
    """
    for page in pages:
        for event in page.get("events") or []:
            message = event.get("message")
            if isinstance(message, str):
                yield message


def _unread(status: ReadingStatus, note: str) -> SourceReading:
    return SourceReading(
        name=AWS_INVOCATION_LOG_SOURCE,
        written_by="aws",
        basis="tokens",
        status=status,
        note=note,
    )


def invocation_log_reading(
    settings: Settings,
    *,
    client: Any | None = None,
    scope: Scope = Scope(),
    timeout_s: float = 30.0,
) -> SourceReading:
    """What AWS's own record says one slice cost in tokens, or why it cannot say.

    Preserves the distinction that M8's criterion 3 rests on: *not configured*
    (a deployment with no AWS account still has §12.4's gate), *unreachable*
    (asked and missed -- the gate cannot pass) and a measured total are three
    different answers, and none of them is ``0``.

    A log group named without a region is **unreachable**, not unconfigured.
    Dropping a half-configured source would let someone who meant to add the
    second record, and mistyped one field, read a green gate that checked one
    fewer source than they believe.

    The window is pushed into `FilterLogEvents` rather than filtered here: a
    study-scale group is millions of records, and reading all of them to
    discard most is the difference between a gate an operator runs and one
    they skip. **The configuration cannot be pushed anywhere** -- a
    model-invocation record carries the model, the account and the identity,
    and nothing that names an ablation cell -- so the reading declares that it
    covers the window alone and `at_scope` refuses to set it against a
    configuration-scoped ledger.

    ``client`` is a seam for tests: with no AWS account, the only honest
    standard is verified-against-a-mock, so the caller may hand in a stubbed
    CloudWatch Logs client. Production passes nothing and gets
    :func:`logs_client`.
    """
    group = settings.observability.aws_invocation_log_group
    region = settings.observability.aws_region

    if not group:
        return _unread(
            "not configured",
            "observability.aws_invocation_log_group is not set, so no record written "
            "outside this process was asked for",
        )
    if not region:
        return _unread(
            "unreachable",
            f"observability.aws_invocation_log_group is {group!r} but "
            "observability.aws_region is not set; routing never comes from the "
            "ambient environment (ADR-0028)",
        )

    request: dict[str, Any] = {"logGroupName": group, **_window(scope)}

    try:
        logs = logs_client(settings, timeout_s=timeout_s) if client is None else client
        paginator = logs.get_paginator(_PAGINATED_OPERATION)
        totals = accumulate(_messages(paginator.paginate(**request)))
    except Exception as exc:  # noqa: BLE001 -- an unreadable log is not a discrepancy
        # The same shape as `langfuse_reading`'s guard and for the same reason:
        # a record that cannot be read must not fail the gate as a 100%
        # disagreement, and must not pass it as a zero. The reason is carried,
        # never swallowed -- a missing `aws` extra, absent credentials and a
        # log group that does not exist are different problems and the note
        # names which one happened.
        return _unread("unreachable", f"{type(exc).__name__}: {exc}")

    return SourceReading(
        name=AWS_INVOCATION_LOG_SOURCE,
        written_by="aws",
        basis="tokens",
        spend=PhaseSpend(
            source=AWS_INVOCATION_LOG_SOURCE,
            # The record counts tokens and states no price; see the module
            # docstring for why a derived dollar figure would be worthless.
            total_usd=None,
            calls=totals.records,
            input_tokens=totals.input_tokens,
            output_tokens=totals.output_tokens,
            detail=_detail(totals, group=group, region=region, scope=scope),
        ),
        covers=scope.window,
    )


def _detail(totals: InvocationTotals, *, group: str, region: str, scope: Scope) -> str:
    """One line a reader can act on, including when the group is empty.

    An empty group is a *read*, and the arithmetic refuses it -- against a
    ledger with calls it is a 100% discrepancy, against an empty one it is
    vacuous -- so this line's job is to say which of the two plausible causes
    to check rather than to change the verdict. With a window applied there is
    a third, and naming it matters more than the others: the group can be
    correct, logging enabled, and the operator's bounds simply wrong.
    """
    parts = [f"{totals.records:,} model invocation(s) in {group} in {region}"]
    if scope.windowed:
        parts.append(scope.window.describes())
    if totals.records == 0:
        parts.append(
            "no model-invocation records: check that invocation logging is enabled, "
            "and that the calls were made through the bedrock-runtime endpoint, "
            "which is the only one it covers (ADR-0048)"
            + (", and that the window covers when they were made" if scope.windowed else "")
        )
    if totals.models:
        parts.append("models " + ", ".join(totals.models))
    if totals.without_tokens:
        parts.append(f"{totals.without_tokens:,} without token counts")
    if totals.rejected:
        parts.append(
            f"{totals.malformed:,} unparseable, {totals.foreign_schema:,} not "
            f"{MODEL_INVOCATION_SCHEMA_TYPE}"
        )
    return "; ".join(parts)
