# Results: what was measured, and the boundaries of each number

This repository's first rule is that reports print measured values and that
no target is ever written into a report code path; a static check enforces it.
This page applies the same rule to the project as a whole. Every figure is one
this repository produced, with where it came from.

Figures marked **2026-10-02** were re-measured that day against `main`, the
local database and the live AWS account (dates are UTC). The others were
measured at the milestone shown and are recorded in the [build log](../CLAUDE.md).

## The AWS analytics plane

| Quantity | Measured | When |
|---|---|---|
| Terraform apply, portfolio profile | **21 added, 0 changed, 0 destroyed**, one region | 2026-10-02 |
| Drift | **No changes** on the plan that followed | 2026-10-02 |
| Round trip, `cascade aws smoke-test` | **3 synthetic rows written, 3 equal rows returned** | 2026-10-02 |
| Object written | 4,384 bytes of Parquet, 13 columns, `aws:kms` | 2026-10-02 |
| Athena query | SUCCEEDED, **953 bytes scanned**, 427 ms engine time | 2026-10-02 |
| Writer role `DeleteObject` | **AccessDenied** | 2026-10-02 |
| Account's own trail, budget, anomaly monitor, guardrail | Present and unchanged | 2026-10-02 |

## Engineering gates

| Quantity | Measured | When |
|---|---|---|
| Offline test suite | **2,533 passed**, 2 skipped with the standard install, which is what CI runs (2,534 and 1 once the embedding extra is installed); ruff, black and mypy strict clean over 124 modules | 2026-10-02 |
| Terraform tests, mock providers | **131** platform, **62** sandbox; fmt clean; 3 roots valid; TFLint clean | 2026-10-02 |
| Checkov | **1,027 passed, 0 failed**, 73 skipped, each skip justified in place | 2026-10-02 |
| CI on `main` | All three jobs green | 2026-10-02 |
| Replay determinism | **25 of 25** runs byte-identical across processes | 2026-10-02 |
| Provenance | A complete chain from an outcome to its root cause, in well under a second | 2026-10-02 |

## The system

| Quantity | Measured | When |
|---|---|---|
| Backtest scenarios | **180**, YES rate 0.5000, no domain above 25%; sealed and re-hashed before any label is read. 15 are exchange placeholder legs, excluded and counted, so 165 are scored: 40 development, 125 test, declared before any forecast existed | 2026-10-02 (registry); M14 (split) |
| Evidence corpus | **1,998,127** chunks across **317,780** documents; no null or future dates; 100% embedded | 2026-10-02 |
| Time lock | **0 of 500** planted post-cutoff documents retrieved across all 180 cutoffs, through the vector, keyword and rerank paths | M3, M14, M15 |
| Retrieval quality | recall@20 **0.9675** against exhaustive search (criterion above 0.92, met) | M8 |
| Retrieval latency | p95 **90.92 ms** against a 15 ms criterion written for per-step retrieval; retrieval now runs once per scenario and actor, so the study's total retrieval time is minutes. The criterion is reported as missed | M8 |
| Compiled causal graphs | **155 of 165** scored scenarios; mean 12.34 actors, 7.46 factors; every stored graph re-hashes and re-validates | M14 |
| Prompt injection through the corpus | Obeyed on 20 of 30 scenarios before quoting documents in a frame they cannot forge; **0 of 30** after | M14 |
| Market benchmark | Brier 0.2078 over the 107 test scenarios with a usable price | M14 |
| Stored simulation | **2,850** runs, **520,455** logged decisions | 2026-10-02 |
| Arbiter properties | Bounded, conserving, permutation-invariant, monotone, under Hypothesis | M5 |

## The first full study

The whole pipeline ran with a model deciding every turn: 36 development
scenarios, 10 replicates each, **360 runs and 66,231 model calls**, through
the Claude Code CLI provider.

| | Brier | Cascade minus this row, 95% CI | p |
|---|---|---|---|
| Base rate (climatology) | 0.2500 | [−0.097, +0.043] | 0.39 |
| Single model, same evidence | 0.2069 | [−0.085, +0.110] | 0.80 |
| **Cascade** | **0.2197** | — | — |

At 36 scenarios no pairwise comparison is distinguishable from zero. The
study cannot separate the three, and it says so. Detecting the effect that was
measured would need on the order of 2,000 scenarios. The 125 test scenarios
are deliberately unspent.

Two operating figures from that study are outside their design band: the
activation rate measured 0.63 against 0.347 ± 0.04, and the action-cache hit
rate 0.07 against 0.88. Both follow from the compiler assigning higher factor
volatility than the cost model assumed. Neither was tuned toward its target.

## Boundaries

What these numbers do and do not cover.

1. **Inference provider.** Every model-produced number came through the
   Claude Code CLI, which cannot set `temperature` or `max_tokens`. Nothing
   has been measured under the pinned API configuration.
2. **What is live on AWS.** The state bucket and the platform root's
   portfolio profile: one KMS key, the event lake and an alerts topic. The
   full platform (recovery, Object Lock, GuardDuty, Config, reports) and the
   sandbox (VPC, Aurora, Fargate) are in the Terraform and not deployed.
3. **What is in the lake.** Three synthetic events from the smoke test. No
   code exports the real event log to the lake yet.
4. **Bedrock.** Rerank and Guardrails are integrated and tested against the
   service models; the account's guardrail and the rerank model resolve live.
   Neither has carried live traffic, and Claude Platform on AWS has not been
   enabled.
5. **Evaluation breadth.** The 12-cell ablation grid, the self-consistency
   baseline and the test partition have not been run.
6. **Cost reconciliation.** The CLI provider bills no tokens, so there is no
   spend to reconcile and `cascade trace cost` reports that rather than
   passing on zero against zero.
7. **Scenario sources.** The curated scenarios are not independently
   verified; Metaculus needs a token and contributed nothing; GDELT
   contributed nothing.

The evidence behind the dated figures is in the [evidence pack](evidence/README.md).
