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
| 0 — irreplaceable | Sealed registry + labels + manifest hash; the source-response cache that reproduces it; the LLM recording cache | Cannot be regenerated, or only by re-spending | registry and source cache: 0 after seal, **if the person uploads** (below); LLM cache: 0 for a killed task, ≤ the DataSync schedule for a lost file system, plus replication lag for a lost region | 1 hour for the registry; LLM cache **unmeasured** — bound by object count, not bytes (below) | `cascade ledger export` and `aws s3 sync` of the source cache, uploaded by a person with the add-only archive policy; the LLM cache on EFS, copied by a scheduled DataSync task into `llm-cache/<sandbox>/` (ADR-0042); one versioned, Object-Locked bucket, replicated cross-region without delete markers |
| 1 — expensive | Evidence corpus | Reproducible, ~20 h | 24 hours | 4 hours | `pg_dump` to S3 after ingest settles; Aurora snapshot before any re-partitioning |
| 2 — database state | Aurora cluster | Point-in-time recoverable | ~5 minutes (Aurora PITR) | ~30 minutes for a cluster restore, **unmeasured** | 7-day backup retention, KMS-encrypted; copy-on-write clones for experiments so the baseline is never the thing being modified |
| 3 — derived | Event lake, reports | Reproducible by replay (M8: byte-identical) | n/a | hours | Object Lock protects against deletion; replay is the recovery |

## Scope: what this design does not cover

- **Region failure.** Single-region by design. The study is a batch workload
  with no availability requirement; tier 0 is replicated cross-region so a
  region loss costs time, never the frozen split. A warm standby would spend
  more than the study.
- **The caches, as of ADR-0042.** The LLM cache lives on an EFS file system
  the study task mounts (`modules/cache`), so a task killed at its timeout
  loses at most the one call in flight — an fsync on EFS is durable when it
  returns, and `cache.py` fsyncs before it renames. The file system is copied
  into the recovery bucket by DataSync on a schedule the operator sets
  (`study.sync_schedule_expression`, no default; hourly is DataSync's
  minimum), add-only and never overwriting, so the archive is at worst one
  schedule interval old, and every object in it is a paid call. The source
  cache is made on a person's machine and reaches the bucket only when that
  person uploads it: **nothing schedules it.** Both prefixes replicate to the
  second region under the same rule as the registry.
- **Built, not exercised on AWS:** `cascade ledger export` writes the sealed
  registry to one file carrying two independent checks -- the manifest hash and
  a digest over every field, because a reworded question leaves the manifest
  intact -- and `cascade ledger restore` verifies both before writing and
  re-hashes the database afterwards. `modules/recovery` is the bucket it goes
  to: Object-Locked, replicated to a second region without delete markers, and
  explicitly denied to the simulation's principals, because the archive holds
  the labels.
- **Not yet built:** a scheduled upload of the source cache (it is a person
  with `aws s3 sync`); the restore drills that would turn the targets above
  into measurements; a check that the archive's object count matches the file
  system's entry count. **Not verified:** that DataSync writes into a bucket
  with a default Object Lock retention — AWS Config refused one, and S3
  requires a checksum header on such uploads; if DataSync refuses at apply,
  the fallback is the Config bucket's pattern (versioned, permanent deletion
  denied in the bucket policy) for the `llm-cache/` prefix in a bucket of its
  own, replicated into the locked replica.

## Procedures

**Registry or labels lost** → `cascade ledger restore --from <archive>`, then
`cascade ledger verify`. If the manifest hash does not match, **stop**: every downstream number
is keyed to that split, and a silently different split is the failure the
frozen-split mechanism exists to prevent.

**Source cache to archive** (after every `cascade ledger seal`, by a person
holding the platform's `recovery_archive_write_policy_arn`) →
`aws s3 sync .cache/sources s3://<recovery-bucket>/source-cache/<manifest-sha>/`.
The policy can add and list, not read or delete: a laptop credential that is
stolen cannot read the labels back out.

**LLM cache lost with the file system** (a sandbox destroyed, a bad `rm`) →
the archive is `terraform output study` → `llm_cache_archive`. To a laptop:
`aws s3 sync <archive> .cache/llm/`; then `cascade trace replay --runs 25`
proves the recordings serve. To a new file system: create one with the
sandbox (`terraform apply` with `study` set), then run a DataSync task with
the two locations swapped — `s3_location_arn` as source, `efs_location_arn`
as destination, `overwrite_mode NEVER`, `preserve_deleted_files PRESERVE` —
and confirm the entry count. `cache.py` rejects an entry filed under the
wrong key, so a corrupted object is a loud miss, never a wrong answer.
**Before `terraform destroy` of a sandbox with a study**, start the archive
task by hand (`sync_task_arn`) and wait for it: the file system goes with the
sandbox, and the archive is only as new as its last run.

**LLM cache lost in the region** → the replica bucket holds the same
prefixes; restore from `<name>-recovery-<account>-<replica-region>` as above.
RPO is the DataSync interval plus S3 replication lag, which has no SLA
without Replication Time Control (not enabled: it is priced per GB, for a
dataset whose loss costs re-spending, not downtime).

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
