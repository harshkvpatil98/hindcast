# ADR-0054 — A portfolio deployment profile: the production tiers are switches, and the live deployment proves the path, not the controls

- **Status:** accepted — requested by the project owner, 2026-10-01
- **Milestone:** after M17
- **For this profile only, sets aside ADR-0035's recovery tiers and the lake's Object Lock, and ADR-0046's lock on the reports bucket. The defaults keep all three.**

## Context

The platform root planned **74 resources** against the owner's account
(us-west-2), after three switches kept it off the trail, the budget and the
cost-anomaly monitor the account already had. The owner's reading of that
plan: this is a portfolio implementation, not a production deployment. A live
deployment should prove the architecture works and stay cheap and removable;
the production-grade modules should stay in the repository, intact.

Most of the 74 served production concerns: 28 were the tier-0 recovery
archive (a second region, a second CMK, replication, inventory, a restore
role), 16 were GuardDuty and AWS Config, and four buckets carried a 365-day
Object Lock, which is what makes a deployment hard to remove.

## Decision

**The defaults stay the platform as designed. The tiers become switches at the
platform root, each on by default, and a committed profile turns them off.**

| Switch | Default | Off means |
|---|---|---|
| `enable_recovery` | true | no `modules/recovery`: no replica bucket, no replica-region CMK, no replication, no inventory, no restore role; every recovery output is null |
| `enable_reports` | true | no reports bucket and no read policy; both reports outputs are null |
| `enable_object_lock` | true | the lake and the reports bucket are created without Object Lock, and the reports bucket's Deny on removing versions goes with it |
| `enable_guardduty` | true | no detector, no findings rule, no delivery alarm, and no publish grant for them on the alerts topic |
| `enable_config` | true | no recorder, channel, role or history bucket, and no data-event selector naming that bucket |
| `disposable` | **false** | (on) `terraform destroy` may empty the lake and reports buckets; refused while Object Lock is on |

`envs/platform/portfolio.tfvars` is the profile: all five tiers off,
`disposable` on, and the four `create_*` lines that describe the owner's
account. It is copied to the gitignored `terraform.tfvars`, so the ordinary
commands plan it.

What it deploys is **21 resources**: the platform CMK; the event lake (the
events bucket and the Athena results bucket with their settings, the Glue
database and table, the Athena workgroup, the writer and analyst roles); and
the alerts topic with its policy.

## What the profile gives up, stated rather than implied

- **Invariant 6 on the lake is one control, not two.** The writer role's
  explicit Deny on every delete stays. Object Lock does not. The profile does
  not claim the lake is immutable, and the source of truth for the event log
  is still Postgres, where append-only is a grant.
- **No tier-0 recovery on AWS.** ADR-0035's finding stands -- the sealed
  registry and the source cache are the irreplaceable data -- and in this
  profile they are protected by `cascade ledger export` to a file, not by a
  replicated archive.
- **No access record for the lake.** That went with the account's existing
  trail (`create_audit_trail = false`), which records management events only.
- **No GuardDuty and no Config.** Neither demonstrates anything about Cascade.
  A detector is one resource whose value is findings somebody reads; Config's
  value is a change history somebody consults. Both bill on activity, and
  Config's history bucket denies permanent deletion to everyone, so it is the
  one bucket here a destroy could not empty. The wiring that is worth showing
  -- findings to one encrypted topic, admitted by ARN, with an alarm on failed
  delivery -- is in the repository and verified offline.
- **No reports bucket.** Nothing this profile deploys writes one, and
  `cascade report` writes its directory locally. `enable_reports = true` adds
  seven resources and no standing cost.

## Found while building it

- **A splat on a counted module is a dependency on the whole module.**
  `module.recovery[*].required_key_policy_statements_json` in the key's policy
  made the key depend on every recovery output, and recovery depends on the
  key: a cycle. `module.recovery[0].<output>` behind the flag depends on one
  output.
- **The reports bucket's Deny on `s3:DeleteObjectVersion` is the lock by
  another name.** Left in with the lock off, the bucket would have been
  undeletable by anyone, including a destroy -- the opposite of what the
  switch asks for. It is now conditional on the lock.
- **A trail with no bucket to name must have no data selector.** With Config
  off and no caller buckets the selector's prefix list was empty, which the
  provider refuses. The selector is now present only when there is something
  to select.
- **`alltrue([])` is true.** With every audit tier off, the module's
  `buckets_private` reported true over no buckets. It reports null.
- **The real provider and the mock provider disagree about an empty policy
  document** (found in the previous change): its statements are null to one
  and an empty list to the other, and only a real plan shows it.

## Measured

| | full platform (this account) | portfolio profile |
|---|---|---|
| `terraform plan`, resources to add | 74 | **21** |
| to change / to destroy | 0 / 0 | **0 / 0** |
| regions touched | us-west-2, us-west-1 | us-west-2 |
| customer managed keys | 2 | 1 |
| buckets under Object Lock | 4 | 0 |

Ten properties of the switches were broken on purpose, one at a time, and each
failed a test.

## Applied, 2026-10-01

The profile was applied to the owner's account from `main`, from a saved plan
that had been read in full: **21 added, 0 changed, 0 destroyed**, all in
us-west-2. The plan that followed showed no changes. The account's own trail,
budget, anomaly monitor and guardrail were read before and after and are as
they were.

`cascade aws smoke-test` then sent three events that say they are synthetic
round the lake, each step as the identity that should take it:

| Step | Identity | Result |
|---|---|---|
| put one Parquet file (4,384 bytes, 13 columns) under `config_id=SMOKE` | the writer role | stored, under the CMK |
| delete it | the writer role | **refused, AccessDenied** -- the Deny that is the lake's one append-only control here, asserted live |
| register the partition | the operator | registered |
| query the partition through the workgroup | the analyst role | **refused** on the first run; 3 rows, 953 bytes scanned, after the fix |
| compare | -- | the 3 rows returned equal the 3 written |

**The first run found what no offline gate could.** The analyst role had no
`glue:GetPartition`, which is what Athena calls for a query that filters on
the partition key -- and `config_id` is the partition every analysis filters
on. The role could have scanned the whole table and could not read one cell.
CloudTrail recorded `BatchGetTable` allowed and `GetPartition` denied for the
role. The policy gained that one action (0 added, 1 changed, 0 destroyed), a
test holds it there, and the smoke test passed on the next run.

## Still not verified

**The smoke test is not the event export.** No code here moves the real event
log from Postgres to the lake, so apart from three synthetic rows the table is
empty. An export needs two things this deployment does not have: a writer of
Parquet for real runs, and an identity allowed to register partitions --
neither lake role may change the catalog, by design. The full platform
(recovery, Object Lock, the audit tier, the reports bucket) and the sandbox
remain unapplied.
