# ADR-0029 — The API providers share one cache namespace, conditional on measurement

- **Status:** accepted, conditionally — pending `cascade eval equivalence` against live providers
- **Milestone:** M10
- **Extends ADR-0007**

## Context

The cache key is computed over `LLMRequest.cache_domain()`, which includes
`model`. Bedrock expects `anthropic.claude-sonnet-4-6` where the other two
providers expect `claude-sonnet-4-6`. If the wire id entered the key, a study
recorded through Anthropic would miss entirely through Bedrock, and switching
provider would force a paid re-record of everything.

## Decision

Requests carry the **logical** model everywhere — in the key, the price
lookup and the event log. The provider renders its wire id at the payload
boundary and nowhere else (`providers.render_model`). The three API providers
therefore share one namespace, and every key recorded before this ADR is
unchanged (pinned by a literal digest in the tests).

This is conditional. Sharing a namespace is only sound if the providers serve
the same model, and that is measured, not assumed:
`cascade eval equivalence --reference R --candidate C` asks each question
twice of R and once of C and bootstraps `mean(|a1-b| - |a1-a2|)`. The
reference's disagreement with itself is the control: temperature makes even
one provider inconsistent, so cross-provider disagreement means something only
relative to it. **If the interval lies wholly above zero, the provider joins
the cache key domain** and the command exits 3.

## Rationale

This is ADR-0007's argument one level down. A `cache_control` marker is a
billing directive that does not change what the model sees; the wire spelling
of a model id is a routing directive that does not change which model
answers — *if* the operators serve the same weights. ADR-0007 could assert its
premise; this one cannot, because Bedrock is partner-operated and its release
schedule can differ. Hence the probe.

The probe runs in `live` mode on purpose. Under the shared namespace it tests,
a cached run would answer the candidate's question with the reference's
recording and report perfect agreement by construction.

## Cost of being wrong

If two providers did serve different snapshots, a recording from one would
replay as if it came from the other, and nothing would flag it. That is why
this ADR is conditional, and why the probe is a gate rather than a report.
"No divergence detected" at small n may only mean the probe was underpowered;
the command prints n and the interval so that cannot be mistaken for proof.

## Verified by

`tests/unit/test_llm_providers.py` — a recording made through `anthropic`
replays through `bedrock` with no network; pre-ADR keys are unchanged.
`tests/unit/test_eval_equivalence.py` — the probe calls a same-model candidate
not divergent and an offset candidate divergent, drops unparseable answers
without imputing them, and runs live. **Not yet measured**: no two API
providers have been available in one environment.
