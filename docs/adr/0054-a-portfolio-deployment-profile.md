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

## Not verified

Nothing has been applied: these are plans. And **the lake will be empty**.
No code in this repository exports the event log to Parquet, so after an
apply Athena has a table and no rows until events are written under
`events/config_id=<cell>/` and the partitions registered. The deployment
proves the wiring of S3, Glue and Athena; it does not yet prove a query.
