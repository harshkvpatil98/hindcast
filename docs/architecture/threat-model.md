# Threat model

What could make the study's numbers wrong, leak its secrets, or spend its
budget — and what stands in the way. "Tested" means a test fails if the control
is removed; several were verified by removing them on purpose.

## Assets

| Asset | Why it matters |
|---|---|
| Resolution labels | If the simulation can see them, every forecast is worthless |
| The time lock (`as_of`) | If post-cutoff evidence leaks in, the backtest measures hindsight |
| The sealed registry and its hash | Every reported number is keyed to one frozen split |
| The event log | Provenance and replay rest on it being complete and unaltered |
| Model and database credentials | Spend, and everything above |
| The budget | A study that exhausts it does not finish |

## Threats and controls

| # | Threat | Control | Status |
|---|---|---|---|
| T1 | Simulation code reads the labels | Postgres grant: `cascade_sim` has no privilege on `scenario_labels` (ADR-0005); aggregation runs as `cascade_sim` (ADR-0022); labels are never exported to the lake | tested against a live database (M1) |
| T2 | Post-cutoff evidence reaches an agent | `published_at < as_of` inside a `SECURITY DEFINER` function; `as_of` has no default in Python or SQL; poison-pill probe: 0 of 500 planted documents retrievable (M3) | tested |
| T3 | **Prompt injection through the corpus.** Evidence is scraped news text placed in the agent's prompt; a document could carry instructions | Partial. Agents act only through one schema-constrained tool; an action the actor cannot take is coerced to WAIT and recorded (ADR-0018); the arbiter that folds actions into the world is deterministic Python with no model call (invariant 3). An injected instruction can bias an agent's *choice among legal actions*; it cannot make it take an illegal one or touch the arbiter | **residual risk — not measured.** No probe plants adversarial instructions in the corpus the way the poison-pill probe plants post-cutoff dates. That probe is the missing control |
| T4 | Event log altered after the fact | INSERT-only grant locally; on AWS, Object Lock plus an explicit Deny on delete and lock overrides for the writer | tested (both AWS controls removed by mutation and caught) |
| T5 | Database credentials exposed | TLS required by the server and verified by the client; secrets percent-encoded in URLs; **never on a command line** — they were, until M11; RDS-managed master password never in Terraform state; IAM tokens for the app roles | tested |
| T6 | Role passwords in Terraform state | State bucket encrypted with its own CMK, versioned, TLS-only, not public | tested; **residual**: anyone who can read state can read them — IAM tokens (`cascade db enable-iam`) remove them from use |
| T7 | Spend redirected or runaway | Region and endpoint explicit, never ambient (decoys planted in tests); per-phase ceilings abort in-process; an account-level budget alarms from outside; SCP denies regions nobody chose | tested |
| T8 | A batched phase silently runs unbatched at ~2× cost | `complete_batch` refuses a provider without a batch API before any spend | tested |
| T9 | Supply chain: a dependency or base image changes underneath | `uv.lock`; Terraform provider locks with hashes for three platforms; container base pinned by digest; ECR tags immutable; pgvector pinned via the Aurora minor version | tested where static; **residual**: the RDS CA bundle is fetched at image build over TLS and not pinned, because AWS rotates it |
| T10 | Data exfiltration from the VPC | No internet gateway, NAT or public IP; the S3 endpoint policy names its buckets; task egress limited to endpoints, S3 and PostgreSQL | tested |
| T11 | Audit trail switched off from inside the account | The trail exists (`modules/audit`: multi-region CloudTrail with log validation to an Object-Locked bucket, GuardDuty, Config); an SCP denies stopping any of them, and denies the root user | tested; SCPs **not attached** — needs an organization's management account |
| T12 | A subscription credential used where an IAM identity belongs | `claude_code` is local-only and excluded from every AWS path (ADR-0031) | by construction |

## Out of scope

Multi-tenancy (there is one tenant), end-user authentication (there is no
service yet — live mode, proposed as ADR-0032 on the `m13/live-mode` branch, would add both), and physical or
provider-level compromise of AWS or Anthropic.
