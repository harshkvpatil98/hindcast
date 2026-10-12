# ADR-0030 — Bedrock Knowledge Bases rejected; Bedrock Guardrails deferred

- **Status:** accepted
- **Milestone:** M10
- **Records two decisions not to adopt a managed service, and why**

## Context

An AWS-native Gen AI design would reach first for Bedrock Knowledge Bases
(managed RAG) and Bedrock Guardrails. Both were evaluated against this
system's invariants before either was adopted.

## Decision

**Knowledge Bases: rejected.** Retrieval stays in Chronofence
(`chronofence_search`, migrations 004–015).

**Guardrails: deferred**, not wired in.

## Rationale

**Knowledge Bases cannot express the time lock.** The system's validity rests
on invariant 1 and on the M3 poison-pill result — 0 of 500 post-resolution
documents retrievable at any of 180 cutoffs. That guarantee is a
`published_at < as_of` predicate inside a `SECURITY DEFINER` function, with
`as_of` a required argument that has no default at either the Python or the
SQL boundary, and a leakage suite that tests it against the live corpus.
A managed knowledge base filters on metadata at query time, and the filter is
an optional parameter of the request. Moving retrieval there would turn a
database-enforced invariant into a caller's promise and a vendor's
implementation. It would also discard the HNSW tuning measured at M8
(ADR-0026). The service is not worse; this system needs a property it does not
provide.

**Guardrails have no path through the one door.** The installed SDK's Bedrock
client (`AnthropicBedrockMantle`) targets the Messages-API endpoint and has no
guardrail parameter — a search of the installed package for "guardrail" returns
nothing. Applying one would mean either sending an undocumented header, which
the project's rule against guessing SDK usage forbids, or calling the
standalone `ApplyGuardrail` API through boto3 as a second model-adjacent call
path outside `LLMClient`, which invariant 5 exists to prevent. There is also
an unmeasured confound: a guardrail that alters a single decomposition changes
the experiment.

## What would change this

Guardrails become worth adopting if the SDK's Bedrock client grows a guardrail
parameter, *and* a measurement shows it alters no compiled graph on the 180
scenarios — at which point it is a safety control with no effect on the study.
The natural shape is a post-hoc audit over stored graphs rather than a filter
in the call path.

## Verified by

The SDK search described above, against `anthropic` 0.121.0. Chronofence's
guarantees are verified by `tests/leakage/` as before.
