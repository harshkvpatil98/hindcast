# Disaster recovery

Recovery targets are **design targets, not measurements** — no restore has been
exercised on AWS. They are set from what each dataset costs to lose, which this
project learned the hard way. [The drill](#the-restore-drill) below is what
would turn them into measurements; it has not been run, and until it is, every
figure in the table is arithmetic.

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
| 3 — derived | Event lake | Reproducible by replay (M8: byte-identical) | n/a | hours | Object Lock protects against deletion; replay is the recovery |
| 3 — published | Study reports (Appendix D) | Derived, but **not byte-reproducible** — `manifest.json` carries a timestamp and a git sha — and a published report is the study's claim, not a copy of one | 0 once uploaded | minutes (one directory of ~15 files) | `modules/reports`: versioned, encrypted, Object-Locked in GOVERNANCE, with the lock's escape hatch denied in the bucket policy so withdrawing a figure costs two acts and the trail records both (ADR-0046) |

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
  person uploads it: **nothing schedules it, and nothing will** — the cache
  that matters is the one the sealed split was derived from at the moment it
  was derived, so a clock is the wrong trigger (ADR-0046 names the three
  alternatives it rejected). What is scheduled instead is the *check*: an S3
  Inventory of the whole tier-0 bucket, `Daily` or `Weekly`
  (`inventory_schedule`, no default), which says whether
  `source-cache/<manifest-sha>/` exists and how many objects it holds without
  listing a key or reading an object. The archivist's grant now requires that
  sha-named first segment, so an upload that does not name a seal is refused by
  IAM. Both prefixes replicate to the second region under the same rule as the
  registry.
- **Built, not exercised on AWS:** `cascade ledger export` writes the sealed
  registry to one file carrying two independent checks -- the manifest hash and
  a digest over every field, because a reworded question leaves the manifest
  intact -- and `cascade ledger restore` verifies both before writing and
  re-hashes the database afterwards. `modules/recovery` is the bucket it goes
  to: Object-Locked, replicated to a second region without delete markers, and
  explicitly denied to the simulation's principals, because the archive holds
  the labels.
- **Not yet built:** a second S3 Inventory in the replica region, so a
  region-loss drill counts the replica by listing it rather than by reading a
  report; a bound on presigned-URL age (`s3:signatureAge`); a check that ties a
  seal to its archive at the moment of sealing, which lives in `cascade/` and
  not in infrastructure. **Not verified:** that DataSync writes into a bucket
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

**Source cache and registry to archive** (after every `cascade ledger seal`,
by a person holding the platform's `recovery_archive_write_policy_arn`). Both
keys begin with the manifest sha256 that `ledger seal` and `ledger export`
print, and **IAM requires it** — a 64-character first segment, so an upload
that does not name its seal is refused rather than mis-filed (ADR-0046):

```bash
cascade ledger export --to ~/cascade-registry.json   # prints "manifest sha256: <sha>"
SHA=<that sha>                                       # `cascade ledger status` shows it too
aws s3 cp ~/cascade-registry.json "s3://<recovery-bucket>/registry/$SHA.json"
aws s3 sync .cache/sources "s3://<recovery-bucket>/source-cache/$SHA/"
```

The policy can add and list, not read or delete: a laptop credential that is
stolen cannot read the labels back out. Confirm it landed against the archive
listing (`terraform output recovery_inventory_uri`), which is regenerated on
`inventory_schedule` — or immediately, with
`aws s3 ls "s3://<recovery-bucket>/source-cache/$SHA/" --recursive --summarize`.

**Report to the reports bucket** (after `cascade report`, from the study task,
whose role may add objects there and may not read one back) →
`terraform output publish_report` prints the exact command; replace `STUDY_ID`
with the `study_<ts>` directory the CLI named. Every version is locked for
`report_lock_retention_days`: a figure that turns out to be wrong is
**superseded by a new report, never withdrawn**.

**Report needed by a reader** → attach the platform's
`reports_read_policy_arn`, then
`aws s3 sync "$(terraform output -raw reports_uri)<sandbox>/<study_id>/" ./study/`
and open `headline.md` locally. There is no site and no public endpoint, by
decision: a report carries the resolution labels per scenario (ADR-0046). For
one file to one person who cannot assume a role, `aws s3 presign` — and note
that from an assumed role the URL dies with the session, whatever `--expires-in`
says.

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

## The restore drill

Nothing above has ever been exercised on AWS. This is the procedure that would
change that, in order, with the check that proves each step. **It has not been
run**; when it is, the measured wall-clock, object counts and exit codes go in
the build log and the tier table stops saying "target".

Run it into an **empty account** — a second account, or the same one after the
platform has been destroyed and re-applied. Restoring into the account that
still holds the data proves nothing.

**Assume the restore role, and nothing else.** `terraform output
recovery_restore_role_arn`. It reads tier 0 in both regions and cannot write
either; every later step is done as that role, so the drill measures the
identity a real incident would use rather than an administrator's.

**0. Preconditions.** `envs/bootstrap` applied; `envs/platform` applied with
`restore_targets` naming the new sandbox's artifacts bucket and its CMK (or
left empty, if the drill restores only to a machine). Fetch the newest
inventory manifest from `terraform output recovery_inventory_uri` and note,
per prefix, the object count and total size. That is the number every
completeness check below compares against.

**1. The registry.** One small file; this is the hour in the tier table.

```bash
aws s3 cp "s3://<recovery-bucket>/registry/<sha>.json" ./registry.json
cascade ledger restore --from ./registry.json
cascade ledger verify
```

*Check:* `ledger restore` verifies the manifest hash **and** a digest over
every field before it writes anything — a reworded question leaves the
manifest intact, which is why there are two — and `ledger verify` re-hashes
the database afterwards. The sha it prints must equal the one in the key.
**If it does not, stop.** Every downstream number is keyed to that split.

**2. The source cache — the step that proves the archive is the right one.**

```bash
aws s3 sync "s3://<recovery-bucket>/source-cache/<sha>/" .cache/sources/
cascade ledger build          # no --refresh: from the cache only
cascade ledger seal
```

*Check:* the sealed sha256 must equal `<sha>`. This is the exact check the
2026-08 incident needed and did not have: a source cache that does not
re-derive its own split is not a backup of anything. Also compare the file
count against the inventory's count for that prefix.

**3. The LLM cache.** To a machine:

```bash
aws s3 sync "$(terraform output -json study | jq -r .llm_cache_archive)" .cache/llm/
```

To a new file system instead: create one with the sandbox (`terraform apply`
with `study` set) and run a DataSync task with `modules/cache`'s two locations
swapped — `s3_location_arn` as source, `efs_location_arn` as destination,
`overwrite_mode NEVER`, `preserve_deleted_files PRESERVE`.

*Check A, count:* restored entries equal the inventory's count for
`llm-cache/<sandbox>/`. The archive excludes `cache.py`'s `.tmp` files at the
source, so the two should agree exactly.
*Check B, integrity:* every entry is named for the hash of what it answers, so
re-hashing each restored file against its own name is a complete verification.
`cache.py` rejects an entry filed under the wrong key, so a corrupted object is
a loud miss and never a wrong answer.
*Check C, the one that matters:* `cascade trace replay --runs 25` must exit
**0** with byte-identical event-log hashes. A short or damaged restore exits
**4** — cache miss in replay. The drill's pass/fail is an exit code, not an
impression.

**4. The reports.** `aws s3 sync "$(terraform output -raw reports_uri)<sandbox>/<study_id>/" ./study/`.

*Check:* `manifest.json`'s scenario hash equals the sha restored in step 1. A
report whose scenario hash does not match the restored split is a report about
a different study.

**5. The negative check — do this one, it is the point.** Still holding the
restore role:

```bash
echo probe | aws s3 cp - "s3://<recovery-bucket>/registry/probe.txt"
```

*Check:* `AccessDenied`. A restore that can overwrite its own source is not a
restore, and three separate controls should produce that refusal — the role's
own policy, the bucket policy's `RestoreNeverWritesTheArchive` Deny, and (for
a target named in Terraform) the plan-time precondition. The drill exercises
the first two; the third is a `terraform test`.

**6. The region.** Repeat steps 1–3 against
`<name>-recovery-<account>-<replica-region>`. The replica has no inventory of
its own, so its counts come from `aws s3 ls --summarize`. RPO here is the
DataSync interval plus replication lag, which has no SLA without Replication
Time Control (not enabled).

**7. Record it.** Wall-clock per step, object counts, the exit code from
`trace replay`, and anything that refused. Then destroy the drill account: an
archive with a second live copy of the labels in it is a second thing to
protect.
