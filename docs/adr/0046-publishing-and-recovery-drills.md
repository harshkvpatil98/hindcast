# ADR-0046 — Publishing the study's deliverable, and proving the recovery path

- **Status:** accepted as a design — gated offline, not applied
- **Milestone:** M14 infrastructure follow-up
- **Closes three of the items on ADR-0042's "Where the design still stops" list**

## Context

ADR-0042 gave the ingest a way out, the study a model and both caches a place
in the recovery plan. It ended with a list of what was still missing. Three
items on it are about the same thing from different sides — *what the study
produces, and whether what it keeps can be got back*:

- **Nothing published what `cascade report` writes.** The deliverable is
  Appendix D as a directory — `headline.md`, `manifest.json`, `metrics.json`,
  five CSVs, three JSON files and four SVG figures — and it existed only in a
  Fargate task's scratch storage, which dies with the task. The one artifact
  the whole platform exists to produce had nowhere to go.
- **No restore has ever been run.** `docs/architecture/dr-runbook.md` opens by
  saying its targets are "design targets, not measurements", and every tier-0
  mechanism — the Object Lock, the cross-region replication, the DataSync
  archive — had been reasoned about and never exercised. A recovery target
  that has not been exercised is a hope.
- **The source-cache upload is a person.** The cache that reproduces the
  sealed split reaches tier 0 only when someone remembers to run `aws s3
  sync`, and the failure mode of that is silence.

Each is a decision about exposure, cost or identity. This record makes them,
names what was rejected, and writes down what could not be verified without an
account.

## Decisions

### 1. Reports land in a private, versioned, Object-Locked bucket — fetched, never served

`modules/reports` creates one bucket in the **platform** root, beside the
recovery bucket and for the same reason: the sandbox is built to be destroyed,
and a published report must outlive it. The study task writes into
`reports/<sandbox>/study_<ts>/` with an add-only grant (`modules/study`); a
reader takes the directory whole with `aws s3 sync` under a managed policy the
module exports and attaches to nobody.

**The finding that shaped this: a report carries the labels.** It reads like a
summary, and it is not. `baselines.csv` is
`(config_id, scenario_id, p_hat, outcome)` and `ablation_grid.csv` is
`(cell_id, scenario_id, p_hat, sigma, outcome)` — one row per scenario, with
how that scenario resolved. So a report directory is a label-bearing store in
exactly the sense `modules/recovery` already uses for `registry/` and
`source-cache/`, and two things follow.

The first is that **the simulation is denied reading it, and the simulation is
the writer.** The study task role appears in the platform root's
`simulation_principal_arns`, so the reports bucket's policy Denies it
`s3:GetObject`; its own identity policy holds `s3:PutObject` and no `Get`. It
produces reports and cannot read one back. That is not a tidy side-effect of
least privilege — a simulation that could read a previous report could read
the outcome column straight out of `ablation_grid.csv`, which is the leak the
Postgres grant (invariant 2) and the recovery bucket's Deny exist to prevent.
Write-only is the requirement.

The second is the publishing decision itself.

**Rejected: a static site on CloudFront with origin access control.** OAC does
work — it keeps the bucket private, supports SSE-KMS, and AWS recommends it
over the legacy OAI — and it is still the wrong answer here, for three
reasons in the order they matter:

1. A distribution is a **standing public endpoint for the labels**. §1.3's
   frozen split and §4.4's memorisation probe both rest on the resolution
   labels not being crawlable; putting this study's own answer key on the open
   web is a leakage vector for the next run of this study and for anyone
   else's. CloudFront *can* be closed with signed URLs and a trusted key
   group — which is the presigned-URL model rebuilt with a key pair and a
   rotation story to own.
2. **A presigned URL expires with the credential that made it.** AWS is
   explicit: "IAM role credentials — the presigned URL expires when the role
   session expires, even if you specify a longer expiration time", and an
   `AssumeRole` session is an hour by default. Short-lived by construction is
   the right default for a bearer token over label-bearing data; a
   distribution is the opposite, existing until someone deletes it.
3. The artifact is a directory read by a handful of people a handful of times.
   A distribution bills and must be maintained between those times — and
   `headline.md` is Markdown, so a browser would download it rather than
   render it. The "site" would not be a site without a generator this project
   does not have.

**What the rejection costs, stated rather than hidden:** the four SVG figures
are referenced from `headline.md` by relative name, so a per-object presigned
URL does not render them inline. The reader syncs the directory and opens it
locally, which is how a Markdown-plus-CSV artifact is read anyway. Presigned
URLs remain for handing one file to one person who cannot assume a role.

**Object Lock is warranted, and its cost is written down.** A report is not
byte-reproducible — `manifest.json` carries a timestamp and a git sha — and
more to the point a published report is the study's *claim*. §1 says that if
the true Brier lands at 0.168 the report says 0.168; a claim that can be
quietly withdrawn and replaced is a claim whose history can be edited. So
every version is locked in **GOVERNANCE** mode for
`report_lock_retention_days`, which has no default. For that whole period:

- a report uploaded with a wrong number cannot be removed, only superseded;
- `terraform destroy` cannot empty the bucket, exactly as it cannot empty the
  trail, the lake or the recovery buckets (the README already documents the
  class);
- every upload must carry a checksum — S3 requires `Content-MD5` or
  `x-amz-sdk-checksum-algorithm` "for any request to upload an object with a
  retention period configured using Amazon S3 Object Lock". The CLI and the
  SDKs send one; a hand-rolled PUT would not;
- GOVERNANCE's escape hatch, `s3:BypassGovernanceRetention`, is **denied in
  the bucket policy to every principal**, so withdrawing a published figure
  costs two acts — editing that policy, then deleting — and the platform root
  lists this bucket among the trail's data events, so both are recorded. That
  is the Config bucket's argument (ADR-0035) applied *on top of* a lock rather
  than instead of one.

What Object Lock does **not** claim, and the module says so where someone
would otherwise assume it: retention protects versions, not keys. AWS: a
retention "doesn't prevent new versions of the object from being created".
Re-uploading `headline.md` writes a new version and the old one survives.
Immutability here means the history is complete, not that the latest bytes
never change.

**ADR-0035's Object Lock lesson was checked and does not apply.** That lesson
is that *AWS Config* cannot deliver to a bucket with a default retention, and
ADR-0042 carried the same unknown for DataSync. Nothing delivers Config or
DataSync into the reports bucket; its only writer is a task running the CLI.

### 2. A restore drill: an ordered procedure, and a role that cannot write its own source

`docs/architecture/dr-runbook.md` gains a drill that restores the registry
archive and both caches into an empty account, with the check that proves each
step, and `modules/recovery` gains the identity it runs as.

**The restore role reads tier 0 in both regions and writes nothing there.** A
region-loss drill restores from the replica, so a role that could only read
the primary would be useless in the one scenario cross-region replication was
paid for. Its write half is empty unless `restore_targets.bucket_arns` names
somewhere, and the default is empty — the documented restore is to a machine,
which is where this project's one real loss happened and why ADR-0042 rejected
AWS Backup.

**A restore that can overwrite its own source is not a restore**, so three
independent controls say so and a `terraform test` asserts each:

1. the identity policy grants no write action on either recovery bucket
   (asserted over *every* statement, not a named one, so a widened policy
   cannot hide behind a different sid);
2. naming a recovery bucket as a restore target is refused at plan — a
   validation on the root variable that matches the whole `<name>-recovery`
   family without needing the account or the region, and a
   `lifecycle.precondition` inside the module against the exact ARNs;
3. the recovery bucket's own policy Denies the restore role `PutObject`,
   `DeleteObject`, `DeleteObjectVersion`, `BypassGovernanceRetention`,
   `PutBucketPolicy` and `PutBucketObjectLockConfiguration`. Controls 1 and 2
   live in files a later change could widen; a Deny in the resource policy
   outranks whatever those files come to say.

Trust on the role is the account root — AWS's standard delegation to IAM, the
same statement this module's replica key policy already carries. It grants
nobody anything by itself; who may assume it is the account owner's decision,
as the archivist policy already is.

**The targets, and where the numbers come from.** They remain *design targets,
not measurements* — that is the runbook's own opening line and nothing here
changes it — but each now says what bounds it:

| artifact | RPO | RTO | bound by |
|---|---|---|---|
| sealed registry archive | 0 from the moment the archivist uploads; the gap between `ledger seal` and that upload is unbounded, and is the risk §3 names | 1 hour (unchanged design target) | bytes — one file of a few MB, then `ledger restore` and `ledger verify` |
| source-response cache | as the registry | unmeasured | bytes — **2.9 GB measured at M1**. No transfer rate has been measured from an AWS account, so no minute figure is claimed |
| LLM recording cache | 0 for a killed task (EFS fsync durability, ADR-0042); ≤ `sync_schedule_expression` for a lost file system; + replication lag for a lost region, which has no SLA without Replication Time Control (not enabled) | unmeasured | **requests, not bytes** — hundreds of thousands of small entries, and `aws s3 sync` issues at least one GET each. The inventory in §3 is what supplies the object count the estimate needs |
| published reports | 0 once uploaded (versioned and locked) | minutes | bytes — one directory of about fifteen files |

**The checks that prove the drill worked** are the project's own mechanisms,
not new ones:

- the registry archive verifies its own manifest hash *and* a digest over
  every field before `ledger restore` writes anything, and `ledger verify`
  re-hashes the database afterwards;
- the source cache is proved by rebuilding from it: `ledger build` with no
  `--refresh`, then `ledger seal`, must produce **the same sha256** the
  archive is filed under. That is the exact check the 2026-08 incident would
  have needed, and the only one that shows the cache is the right one;
- the LLM cache is content-addressed, so every restored entry is verified by
  re-hashing it against its own filename — and then functionally, by
  `cascade trace replay --runs 25`, which exits **0** on byte-identical
  event-log hashes and **4** on a cache miss in replay. A short restore is
  therefore an exit code, not an impression;
- and the drill ends by attempting one `aws s3 cp` into the recovery bucket
  while holding the restore role, and confirming `AccessDenied`.

### 3. The source-cache upload is **not** scheduled, and here is what replaces it

`ledger build` runs on a person's machine and the cache that matters is the
one the sealed split was derived from, **at the moment it was derived**. That
makes a clock the wrong trigger, not merely an inconvenient one.

**Rejected: EventBridge Scheduler driving an ECS task that runs `ledger build`
on a timer, with the cache on EFS and DataSync archiving it** — the LLM
cache's own pattern, and the obvious one. Re-deriving the pool later archives
the markets' responses *as they are then*, which correspond to no seal: the
split is already frozen (`91ccd314…`), and M1's whole finding is that a cache
rebuilt after markets resolve produces a different set. It would put a file
that cannot reproduce the split exactly where the restore procedure looks for
the one that can. That is worse than no archive, because it looks like one.

**Rejected: a scheduled S3→S3 copy from the sandbox's artifacts bucket into
tier 0.** It still waits on the person's first upload, and adds a hop, a role
and a second copy of the labels to protect.

**Rejected: DataSync with an agent on the operator's machine.** An agent VM
and a standing credential on a laptop, for a 2.9 GB dataset uploaded a handful
of times in the project's life. ADR-0042 already chose plain objects over an
agent-shaped path for the same reason.

So the upload stays a person, and two things change so that the person's step
is checkable and the omission is visible.

**The key names the seal, and IAM enforces it.** Both archives go under a
first segment that is the sealed manifest's sha256 —
`registry/<sha>.json` and `source-cache/<sha>/…` — which the DR runbook has
prescribed since ADR-0042 and nothing enforced. IAM resource ARNs support `?`
as "any single character", so the archivist's grant now requires a
64-character first segment. An upload that does not name a seal is refused by
IAM rather than noticed later by someone reading a listing; a runbook sentence
is not a mechanism, and the resource ARN is. It also makes "is *this* split
archived?" a prefix lookup rather than a judgement, and makes one seal's
upload landing on top of another's impossible.

**What is scheduled is the check.** `modules/recovery` configures an S3
Inventory of the whole tier-0 bucket on `inventory_schedule` (`Daily` or
`Weekly`, no default), delivered as Parquet. It answers, without a
`ListObjects` page and without reading a single object:

- whether the sealed split's `source-cache/<sha>/` exists at all — the failure
  a manual step actually has;
- how many objects each archive holds and how large they are, which is the
  drill's completeness check. The DR runbook has listed "a check that the
  archive's object count matches the file system's entry count" as *not built*
  since ADR-0042, and this is it;
- that no version lost its lock, because the report carries version ids and
  each object's retain-until-date.

An inventory report carries keys, sizes, dates and lock metadata and **never
object contents**, so listing the label-bearing prefixes exports no label. The
simulation is denied it anyway: a key is not a label, but the simulation has
no reason to read a listing of an archive it is denied.

**The report lands in a bucket of its own, not a fourth prefix in the locked
one.** An inventory is derived and regenerated on the next run, so WORM
protects nothing and an expiry rule — which AWS's own guidance asks for —
would be a standing attempt to delete locked objects. And delivery by an AWS
service into a bucket with a default Object Lock retention is precisely the
class of unknown ADR-0035 hit with Config and ADR-0042 already carries once
for DataSync; this record declines to carry it twice. The protection is the
Config bucket's instead: versioned, with permanent deletion denied to everyone
in the bucket policy, and the trail recording both acts it would take to get
around that.

**A staleness alarm was considered and rejected on arithmetic.** The obvious
"nothing has been uploaded in N days" control is a CloudTrail metric filter
and a CloudWatch alarm with missing data treated as breaching. Two facts kill
it: a CloudWatch alarm's evaluation range is capped at **seven days** for
periods of an hour or more, and — decisively — the source cache is uploaded
once per seal, so after a legitimate upload the alarm would fire forever. An
alarm that is always on is not a control.

## What could and could not be verified

Verified against AWS's own pages on 2026-09-20:

- S3 Object Lock requires versioning; GOVERNANCE is overridden only with
  `s3:BypassGovernanceRetention` plus an explicit
  `x-amz-bypass-governance-retention:true` header; COMPLIANCE cannot be undone
  by anyone including the root user; retention "doesn't prevent new versions
  of the object from being created"
  (docs.aws.amazon.com/AmazonS3/latest/userguide/object-lock-overview.html).
- `Content-MD5` **or** `x-amz-sdk-checksum-algorithm` "is required for any
  request to upload an object with a retention period configured using Amazon
  S3 Object Lock"
  (docs.aws.amazon.com/AmazonS3/latest/API/API_PutObject.html,
  .../userguide/object-lock-managing.html).
- The choice is one-way for the bucket's life: "After you enable Object Lock
  on a bucket, you can't disable Object Lock or suspend versioning for that
  bucket"; and "a locked version of an object cannot be deleted by a S3
  Lifecycle expiration policy", which is why every locked bucket here carries
  no lifecycle rule and the inventory bucket — which needs one — carries no
  lock (docs.aws.amazon.com/AmazonS3/latest/userguide/object-lock-managing.html).
- A presigned URL made with IAM role credentials "expires when the role
  session expires, even if you specify a longer expiration time"; the CLI/SDK
  maximum is 7 days with an IAM user's key, the console's is 12 hours;
  presigned URLs are bearer tokens, and `s3:signatureAge` exists to bound them
  (docs.aws.amazon.com/AmazonS3/latest/userguide/using-presigned-url.html,
  .../ShareObjectPreSignedURL.html).
- CloudFront OAC keeps an S3 origin private, is recommended over OAI, supports
  SSE-KMS, and requires Bucket-owner-enforced ownership
  (docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/private-content-restricting-access-to-s3.html).
- IAM `Resource` elements support `*` and `?`, where `?` is "any single
  character", within ARN segments
  (docs.aws.amazon.com/IAM/latest/UserGuide/reference_policies_elements_resource.html).
- S3 Inventory runs **Daily or Weekly** only; the destination may be the
  source bucket but must be in the same Region; it needs a destination bucket
  policy admitting `s3.amazonaws.com` with `aws:SourceArn`, `aws:SourceAccount`
  and `s3:x-amz-acl: bucket-owner-full-control`; SSE-KMS with a customer
  managed key needs a `kms:GenerateDataKey` grant to `s3.amazonaws.com` and
  the AWS managed `aws/s3` key is **not supported**; the report contains keys,
  sizes, dates, version ids and lock metadata — never object contents; AWS
  recommends a lifecycle policy that deletes old inventory lists
  (docs.aws.amazon.com/AmazonS3/latest/userguide/storage-inventory.html,
  .../configure-inventory.html).
- A CloudWatch alarm's evaluation period is capped at **seven days** for
  periods of at least one hour, and one day below that; a metric filter turns
  matching log events into a metric an alarm can watch, and reports nothing at
  all during a minute in which no logs are ingested
  (docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/AlarmThatSendsEmail.html,
  .../logs/MonitoringLogData.html).
- CloudWatch alarms are **$0.10 per alarm metric per month**, shown for US
  East (N. Virginia) (aws.amazon.com/cloudwatch/pricing).

**Not verified**, and therefore not relied on or not transcribed:

- **Every S3 price.** The S3 pricing page did not render a regional table when
  this was written — the same failure ADR-0042 recorded for the Service
  Authorization Reference — so no figure is given for S3 Inventory per million
  objects listed, for GET/PUT requests, or for Standard storage. The inventory
  schedule is an operator decision precisely because its price has not been
  measured from this account.
- **`s3:if-none-match` as an IAM condition key.** The Service Authorization
  Reference page did not render, so the design does not lean on a conditional
  write to make the archivist unable to overwrite. What it leans on instead is
  verified: the sha-keyed prefix, and Object Lock keeping every earlier
  version.
- Whether S3 Inventory delivers into a bucket with a default Object Lock
  retention — untested, and deliberately not depended on (see §3).
- **Whether denying `s3:PutObjectRetention` and `s3:PutObjectLegalHold` to
  every principal interferes with an ordinary `PutObject` that inherits the
  bucket's default retention.** No AWS page states it outright either way. The
  evidence is indirect but consistent: AWS's own documented way to cap
  retention periods is a bucket policy that Denies `s3:PutObjectRetention`, so
  denying it cannot be incompatible with normal uploads into a bucket with a
  default. If an apply proves otherwise, the fix is to scope the Deny by
  principal rather than to drop it.
- Whether `terraform destroy` behaves as documented against a bucket whose
  policy denies `s3:BypassGovernanceRetention` to the account root as well.
- Every RTO and RPO in the table above: they are design targets, and the drill
  exists to turn them into measurements.

## Residual risk

- **The reader's copy is unprotected.** Once a report is synced to a laptop it
  is a directory of CSVs carrying the labels, outside every control here. The
  same is already true of `cascade ledger export` output, and the mitigation
  is the same: the credential that fetches is read-only and named.
- **A presigned URL is a bearer token.** Anyone holding the link can fetch
  until it expires. The bound is the session, and `s3:signatureAge` could
  tighten it further; it is not configured, because no presigned URL has ever
  been issued here and a condition nobody has tested against a real fetch is
  a way to discover a broken link during an incident.
- **The drill has not been run.** Everything above is designed and gated
  offline. Until it is run, "1 hour" for the registry is arithmetic about one
  small file, and the LLM cache's RTO is not even that.
- **The source cache still depends on a person.** The inventory makes the
  omission *visible to someone who looks*; nothing makes it visible to someone
  who does not. Closing that needs the check to live where the seal happens,
  which is `cascade/` and not infrastructure.
- **The replica has no inventory of its own**, so a region-loss drill counts
  the replica by listing it. A second inventory configuration in the replica
  region would fix it and was not added: it is a second thing to keep in step
  for a scenario that already costs time by design.

## What mutation testing changed

Fourteen mutants, each restored by hash afterwards; thirteen were killed on
the first run and one survived for a reason worth recording. Killed: the
reports bucket made public; its versioning suspended; `BypassGovernanceRetention`
dropped from the immutability Deny; the simulation's report Deny narrowed to
`GetObjectVersion` alone; the restore role granted `s3:PutObject` on the
archive; the archive's Deny on the restore role renamed away; the archivist's
grant loosened back to `registry/*`; the inventory redirected into the locked
bucket; the inventory schedule given a default; the report retention given a
default; a CloudFront resource added to `modules/reports`; the study task
granted `s3:DeleteObject` on reports; the study task granted `s3:GetObject` on
reports.

**The survivor was the harness, not the code.** "Unpin a third-party action"
was applied to `actions/checkout`, which `tests/unit/test_infra_invariants.py`
skips on purpose — GitHub's own first-party actions are excluded from the SHA
rule. Re-aimed at `aws-actions/configure-aws-credentials` it was killed
immediately. Worth the line: a mutant that lands on an exclusion proves
nothing, and looks exactly like a gap.

## Where the design still stops

Not built: a second inventory in the replica region; an `s3:signatureAge`
bound on presigned URLs; the check that ties a seal to its archive at the
moment of sealing (that is `cascade/`, not infrastructure); the
`cascade_ingest` Postgres role ADR-0042 named. Not possible without an
account: running the drill, and every price the decisions above declined to
transcribe.

## Verified by

`terraform test` — 62 runs in `envs/sandbox` (was 60), 107 in `envs/platform`
(was 89), mock providers; TFLint clean over eighteen directories; Checkov
1,027 passed, 0 failed, 73 justified skips (was 925 / 0 / 66);
`tests/unit/test_infra_invariants.py` — 23 checks, including three that refuse
a CloudFront distribution, a `cloudfront.amazonaws.com` principal or an S3
website configuration anywhere in the tree, and two more decision inputs that
are never defaulted.
