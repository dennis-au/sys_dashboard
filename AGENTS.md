# Sentinel Development Guardrails

Read [docs/system-design.md](docs/system-design.md) for the complete system
design, data model, deployment plan, and operating decisions. Sentinel is an
infrastructure information-gathering and dashboard product. These rules apply
to every change.

## Architecture

- Sentinel is a modular monolith: keep one release and shared codebase,
  PostgreSQL schema, and domain contracts. The API and worker may run as
  separate processes, but are not independently owned or deployed services.
  Split code by domain boundary rather than adding service, queue, or API
  boundaries unless explicitly requested.
- Keep `backend/main.py` as the stable Uvicorn entry point. New application
  code belongs in `backend/sentinel` and must not import `main`.
- Routers translate HTTP only; domain modules own business rules; shared
  persistence, migrations, and integration clients stay below those layers.
- Preserve `/api` request and response contracts unless the change is designed,
  documented, and covered by regression tests.

## Integration And Secrets

- Sentinel gathers approved, read-only infrastructure information from OLVM,
  Linux/SSH, and future source adapters, then projects validated records for
  the portal and Grafana reporting views. Development and unit tests use
  isolated fakes or a dry-run executor; production collection requires explicit
  source configuration and an approved runtime.
- Sentinel persists operational metadata in PostgreSQL and resolves Git-owned
  playbook metadata through internal Forgejo.
- Do not persist or expose raw passwords, SSH keys, tokens, or other secret
  material. Sentinel records provider-neutral opaque references using
  `secret://sentinel/...` only.
- Forgejo writes use either the one-time legacy-profile migration or a
  deterministic per-profile draft branch and protected-main review workflow.
  Browser source must pass through that workflow transiently and must never be
  persisted in PostgreSQL.
- Connection tests, manager syncs, and collection runs are real operational
  workflows when their integration is enabled. They must be read-only against
  target infrastructure, use only a scoped secret reference at execution time,
  record provenance and outcomes, and never turn an unavailable integration
  into a fabricated success. Unit and browser tests must not contact external
  infrastructure.

## Inventory Ownership

- A host has one owner: `olvm` or `manual`. Display the source in list and
  detail views.
- Never let OLVM reconciliation overwrite a manual host. Conversion requires
  an explicit operator workflow and collision review.
- Reject duplicate host names and management addresses. Never silently merge
  or replace a host.
- OLVM hosts use stable manager/resource identity. Pruning is opt-in and only
  becomes eligible after the configured number of authoritative missing runs.
- Manual hosts support Linux/SSH only. Retire them through `disabled` or
  `decommissioned`; avoid hard deletion without an explicit retention flow.

## Collections And Dashboards

- Collection profiles are read-only fact gathering. They must never provision,
  mutate, or remediate infrastructure.
- Collected records require source identity, collection timestamp, collector
  and immutable playbook provenance, and validation before they reach current
  inventory, historical reporting, alerts, or dashboards.
- Profile source remains Git-owned. Browser drafts and local preflight must not
  persist source to PostgreSQL.
- Credentials remain references only and must never be rendered, logged, or
  exported as secret values.
- Sentinel provides inventory, freshness, run-state, and alert dashboards.
  Grafana owns composed analytical dashboards and reads only curated reporting
  views through a dedicated read-only database role.

## Verification And Tooling

- Run focused regression tests for changed domain and API contracts. Report
  unrun or failing relevant tests with their cause.
- Graphify is local development tooling only. Its output, cache, corpus, and
  service state are excluded from the dashboard image.
