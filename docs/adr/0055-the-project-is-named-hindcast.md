# ADR-0055 — The project is named Hindcast; the engine keeps the name Cascade

- **Status:** accepted — requested by the project owner, 2026-10-12
- **Milestone:** after M17
- **Changes the project's name only. No identifier in code, configuration, the database or the live AWS account changes.**

## Context

The project was called Cascade from its first commit. The name described the
mechanism (actors reacting to one another) but did a poor job as a name:

- **It does not say what is distinctive.** Many systems simulate agents. What
  this one adds is that every forecast is made as of a cutoff date, from
  evidence the database will not return if it was published later, and is then
  scored against what happened. The README spends its first paragraph
  explaining that, because the name does not.
- **It is crowded.** "Cascade" already names a widely used AI coding agent, a
  core CSS concept, and a referential action in SQL. A search for the project
  by name returns other things first.

The owner asked for a better name on 2026-10-12.

## Decision

**The project is Hindcast.** A *hindcast* is a forecast made for a period
that has already happened, using only the information that existed before it,
and then checked against the record. That is this project's method exactly:
180 sealed backtest scenarios, retrieval time-locked by
`published_at < as_of` inside Postgres, and Brier scores against the real
resolution. The name states the discipline the code enforces.

**Cascade remains the name of the engine**: the multi-agent simulation and
everything it is addressed by. In the results tables, *Cascade* is the method
under test, compared against a single model and the base rate.

What was renamed:

| Surface | Before | After |
|---|---|---|
| GitHub repository | `harshkvpatil98/cascade` | `harshkvpatil98/hindcast` (GitHub redirects the old URL) |
| README, contributor guide, architecture, demo script, infrastructure runbook | Cascade | Hindcast |
| Diagrams and banner | `docs/assets/cascade-*.svg`, `.png` | `docs/assets/hindcast-*.svg`, `.png` |
| Citation metadata, security policy, code of conduct | none | under the new name |

## What keeps the name Cascade, and why

| Identifier | Value | Why it stays |
|---|---|---|
| Python package, CLI | `cascade`, `cascade …` | It is 124 modules, 125 test files import it, and every command in the evidence pack was captured as `cascade …`. Renaming the CLI would make the evidence describe commands that no longer exist |
| Distribution name | `cascade` in `pyproject.toml` | It names the package it builds. Nothing is published to an index (see the changelog), so the distribution name never reaches a user |
| Environment prefix | `CASCADE_*`, `CASCADE_ENV_FILE` | Every operator's `.env` uses it. `config.py` is the only reader (invariant: no bare environment reads), and a rename would silently drop every setting it did not translate |
| Database and roles | database `cascade`; `cascade_admin`, `cascade_sim`, `cascade_eval` | The 2.0M-chunk corpus and the 2,850 stored runs live in a database with these names. Invariant 2 is a grant on `cascade_sim`, and migrations are forward-only, so renaming a role means a migration that rewrites the grants the integration suite checks |
| Terraform `name` | `"cascade"` at the platform root | It derives the live names: `cascade-events-…` and `cascade-athena-results-…` buckets, the Glue database `cascade`, the Athena workgroup `cascade`, the `cascade-lake-writer` and `cascade-lake-analyst` roles, and the `cascade-cost-alerts` topic. S3 bucket names cannot be changed in place, so a new value plans a destroy-and-create of the lake and ends the zero-drift record |
| Bedrock guardrail | `cascade-audit-guardrail` | Created by hand in the account, outside Terraform, and referenced by ID through `CASCADE_GUARDRAIL_ID`. Renaming it means recreating it, under a new ID |
| Docker Compose | project `cascade`, container `cascade-postgres`, volume `cascade-pgdata` | The volume holds the corpus. The project name is now pinned in `docker-compose.yml`, so a checkout in a folder named anything else, `hindcast` included, attaches to the same containers and volume |
| Metric namespaces, tags | `Cascade/<env>`, `Project = "cascade"` | They belong to the infrastructure above |
| Historical records | ADRs 0001–0054, changelog entries before this one, `Documents/Cascade_Technical_Specification.docx` | They record what was true when written. The specification is what the code cites as "spec §…", under that title |

## Consequences

- A reader meets two names: Hindcast for the project, `cascade` for what they
  type. The README says so in one sentence beside the title, and this record
  lists every place the old name stays.
- Links to `github.com/harshkvpatil98/cascade` keep working through GitHub's
  redirect. The redirect stops working if a new repository named `cascade` is
  created under the same account.
- A full identifier rename is still possible later and would be a milestone,
  not an edit: a forward-only migration for the roles, a new environment prefix
  with the old one kept as an alias in `config.py`, `moved` blocks where
  Terraform allows them, and a planned replacement of the lake. None of that
  buys anything a reader can see today, so it is not done.
