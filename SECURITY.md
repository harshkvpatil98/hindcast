# Security policy

Hindcast holds two kinds of thing worth protecting: the infrastructure it
deploys, and the integrity of its results. A forecast that could see the answer
is not a forecast, so a way around the time lock is treated here as a security
defect, not as a bug.

## Supported versions

There are no releases. Only `main` is supported, and fixes land there.

## Reporting a vulnerability

**Do not open a public issue.** Report privately through GitHub:
[Security → Report a vulnerability](https://github.com/harshkvpatil98/hindcast/security/advisories/new).
The report is visible only to the maintainers until an advisory is published.

Include:

- what you found, and which control it gets around (an invariant in
  [CONTRIBUTING.md](CONTRIBUTING.md), a row of the README's *Security by design*
  table, or an ADR);
- the smallest reproduction you have: a command, a query, a Terraform plan
  excerpt or a test;
- the commit you tested (`git rev-parse HEAD`) and the output of
  `cascade doctor`.

**Redact before you send.** No AWS account IDs, access keys, API keys,
`.env` contents or Langfuse keys. If a finding depends on one, describe its
shape (`arn:aws:iam::<account-id>:role/...`) instead.

The maintainers aim to acknowledge a report within seven days, and will tell
you whether it is accepted, what the fix is, and when it lands. Credit is given
in the advisory unless you ask otherwise.

## In scope

| Area | Examples |
|---|---|
| **Time-lock leakage** | Any path that lets retrieval, an agent tool or a prompt reach text published at or after a scenario's `as_of`; a way to call `chronofence_search` without an `as_of`; the simulation role (`cascade_sim`) reading `scenario_labels` or the outcomes table |
| **Prompt injection** | Retrieved text that escapes the quoted-evidence frame ([ADR-0045](docs/adr/0045-quoted-evidence.md)) and is obeyed as an instruction |
| **Replay and cache integrity** | A recording served for a request it was not made for; an event log that can be changed without its hash changing |
| **Cloud permissions** | A Terraform policy that grants more than [docs/architecture](docs/architecture/README.md) documents: a writer that can delete, an analyst that can write or query outside the workgroup, a bucket that can be made public, storage that is not encrypted with the platform key |
| **Secret handling** | A credential written to a log, the call log, a recording, the event log, a report, or a process's argv |
| **CI and supply chain** | A workflow that runs untrusted code with write permissions or a credential |

## Out of scope

- **Forecast quality.** A Brier score you cannot reproduce is a
  [measurement discrepancy](.github/ISSUE_TEMPLATE/measurement_discrepancy.md),
  and belongs in a public issue.
- Attacks that need an already-compromised workstation or AWS administrator
  access.
- Load against the local Postgres or Langfuse containers, which are bound to the
  developer's machine.
- Vulnerabilities in third-party services (AWS, Anthropic, GitHub) or pinned
  dependencies, unless Hindcast's use of them makes the problem worse. Report
  those to their maintainers.

## Testing against the live deployment

The AWS deployment belongs to the maintainers. Do not test against it. Deploy
the [portfolio profile](infra/terraform/README.md) to an account you own;
`cascade aws smoke-test` then exercises the same S3, KMS, Glue, Athena and IAM
path as the maintainers' runs.

Good-faith research that follows this policy, stays within your own account,
and does not touch anyone else's data will not be pursued by the maintainers.
