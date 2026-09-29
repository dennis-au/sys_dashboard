# Sentinel Development Guardrails

Read [docs/system-design.md](docs/system-design.md) for the complete system
design, data model, deployment plan, and future-state decisions. These rules
apply to every change.

## Architecture

- Sentinel is a modular monolith: keep one dashboard/API stack and split code
  by domain boundary, not into separately deployed services, queues, or APIs
  unless explicitly requested.
- Keep `backend/main.py` as the stable Uvicorn entry point. New application
  code belongs in `backend/sentinel` and must not import `main`.
- Routers translate HTTP only; domain modules own business rules; shared
  persistence, migrations, and integration clients stay below those layers.
- Preserve `/api` request and response contracts unless the change is designed,
  documented, and covered by regression tests.

## Integration And Secrets

- The local stack may persist Sentinel metadata in PostgreSQL and resolve
  Git-owned playbook metadata through internal Forgejo.
- Do not persist or expose raw passwords, SSH keys, tokens, or other secret
  material. Sentinel records OpenBao references under `kv/sentinel` only.
- Forgejo writes use either the one-time legacy-profile migration or a
  deterministic per-profile draft branch and protected-main review workflow.
  Browser source must pass through that workflow transiently and must never be
  persisted in PostgreSQL.
- Connection tests, syncs, and collection actions are local simulations. They
  must not contact OLVM, SSH, OpenBao, Grafana, or production infrastructure.

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
- Profile source remains Git-owned. Browser drafts and local preflight must not
  persist source to PostgreSQL.
- Credentials remain references only and must never be rendered, logged, or
  exported as secret values.
- Grafana owns dashboard composition. Sentinel provides a catalog and handoff,
  not an in-app dashboard builder.

## Verification And Tooling

- Run focused regression tests for changed domain and API contracts. Report
  unrun or failing relevant tests with their cause.
- Graphify is local development tooling only. Its output, cache, corpus, and
  service state are excluded from the dashboard image.
