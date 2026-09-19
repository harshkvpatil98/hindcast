# Disaster recovery

Recovery targets are **design targets, not measurements** — no restore has been
exercised on AWS. They are set from what each dataset costs to lose, which this
project learned the hard way.

## The incident that shaped this

Before M10, the local Docker volume holding Postgres was lost, and the
repository's `.cache/` directory with it. What that cost:

| Lost | Consequence | Recoverable? |
|---|---|---|
| The sealed scenario registry (`30d9c61d…`) | The **frozen split was gone**. Re-fetching the sources produced a valid 180-scenario set — but a *different* one (`91ccd314…`), because markets resolved in the meantime change the pool | **No.** A sealed split can be re-derived only from the exact cached source responses |
| The source-response cache (`.cache/sources`, 2.9 GB) | This is what made the registry loss permanent: with the exact cached responses, `ledger build` would have re-derived the same split | **No** |
| The 1.95M-chunk evidence corpus | Most of a day to rebuild on one machine (measured during the rebuild: ~18 minutes per CC-NEWS unit) | Yes, slowly |
| Every stored run and forecast | Reproducible from seeds for stand-in runs; model runs would replay from the LLM cache | Yes, if the cache survives |
| The LLM recording cache | **Nothing, this time** — it was empty, because no paid model call had ever been made. Once the study runs it is the most expensive thing to lose: every recording is money already spent | **No**, without paying again |

The lesson is that the smallest datasets were the irreplaceable ones. The tiers
below follow from that, not from size.

## Tiers

| Tier | Data | Why | RPO target | RTO target | Mechanism |
|---|---|---|---|---|---|
| 0 — irreplaceable | Sealed registry + labels + manifest hash; the source-response cache that reproduces it; the LLM recording cache | Cannot be regenerated, or only by re-spending | 0 after seal / after each recorded phase | 1 hour | Exported to a versioned, Object-Locked S3 prefix at `ledger seal` and at the end of every recorded phase; cross-region replication **on** for this tier only |
| 1 — expensive | Evidence corpus | Reproducible, ~20 h | 24 hours | 4 hours | `pg_dump` to S3 after ingest settles; Aurora snapshot before any re-partitioning |
| 2 — database state | Aurora cluster | Point-in-time recoverable | ~5 minutes (Aurora PITR) | ~30 minutes for a cluster restore, **unmeasured** | 7-day backup retention, KMS-encrypted; copy-on-write clones for experiments so the baseline is never the thing being modified |
| 3 — derived | Event lake, reports | Reproducible by replay (M8: byte-identical) | n/a | hours | Object Lock protects against deletion; replay is the recovery |

## Scope: what this design does not cover

- **Region failure.** Single-region by design. The study is a batch workload
  with no availability requirement; tier 0 is replicated cross-region so a
  region loss costs time, never the frozen split. A warm standby would spend
  more than the study.
- **Not yet built:** the tier-0 export at `ledger seal`, and the restore drills
  that would turn the targets above into measurements.

## Procedures

**Registry or labels lost** → restore the tier-0 export; run `cascade ledger
verify`. If the manifest hash does not match, **stop**: every downstream number
is keyed to that split, and a silently different split is the failure the
frozen-split mechanism exists to prevent.

**Corpus lost** → restore the latest dump (runbook step 5), then
`cascade retrieval index --drop-legacy`, `retrieval verify`, `corpus verify`,
`corpus coverage`. All four must pass before any phase that retrieves.

**Aurora cluster damaged** → restore to a point in time as a *new* cluster and
repoint `database.host`; never restore over the damaged one, which is the
evidence.

**Suspected tampering with the event log** → compare object versions; under
Object Lock no version can have been removed, so the history is complete.
Re-run `cascade trace replay` against the affected runs: a byte-identical
replay proves the log is what the simulation produced.
