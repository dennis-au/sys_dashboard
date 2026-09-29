# Production Test-Drive Gates

## Status

All implementation-resolvable items from the prior backend-enabled development
backlog are complete as of September 29, 2026. Sentinel now has a Git-owned
playbook workflow, target-free syntax checks, structured reporting, live
Overview and Alerts summaries, a dedicated scheduler worker, and a truthful
unavailable-state Grafana catalog.

The production test-drive image now starts with an empty operational database.
It does not add sample hosts, managers, credential references, profiles,
dashboards, alerts, runs, capacity facts, or playbook source. Docker Compose
requires an operator-provided PostgreSQL password through an untracked `.env`
file; `.env.example` documents the non-secret shape.

The initial migration approval sequence is complete. The remaining items are
separately approved production-deployment design gates, not defects that can
safely be resolved in the local development stack.

## Completed Implementation

- Profiles resolve playbook content from the private Forgejo repository. PostgreSQL stores Git metadata and review metadata, never playbook source.
- Profile edits create or update a Forgejo draft branch and pull request. Only a protected-`main`, immutable commit in `source.state: pinned` is runnable.
- Migration-pending and draft sources return `409` from collection-run and syntax-check APIs. The portal renders the backend source and review state instead of browser fixtures.
- Syntax checks fetch the selected pinned commit and run `ansible-playbook --syntax-check` with an empty inventory. They do not contact target hosts, OLVM, SSH, OpenBao, or Grafana.
- Collection runs, per-host outcomes, capacity facts, and alerts use structured immutable records and curated `sentinel_db.reporting` views. `/api/summary` drives Overview and Alerts, including stale and unavailable states.
- The Dashboards workspace presents Grafana as planned/unavailable locally. Its controls do not claim configured SSO, an embedded instance, or an external link.
- Canonical NDJSON facts and manifests are validated before projection. Secret-bearing fields are rejected, source and immutable playbook provenance are checked, receipt and record-ledger entries are durable, and artifact replay is idempotent.
- Sanitized artifact storage uses atomic gzip writes and checksum-verified reads. Linux filesystem current and snapshot projections are available through curated reporting views; no runner or external target is connected to this path.
- The dedicated Sentinel worker is the sole local schedule authority. It reserves profile/minute slots before queueing the target-free simulation, while the API no longer evaluates schedules in-process.
- An opt-in internal audit mode records redacted API and worker errors as fingerprinted PostgreSQL events with occurrence counts. Its local-only NDJSON exporter is intended for controlled production test-drive diagnosis.
- A clean Forgejo volume provisions the private, protected `sentinel-playbooks` repository but does not add playbook content. A profile can only be created after an authorized operator adds a reviewed immutable revision.

## Production Design Gates

- Design and approve the Linux deployment's reviewer identity, protected-branch policy, service credential scope, audit retention, export handling, backup, and recovery process before enabling normal operator use.
- Design the production Ansible runner, OpenBao access, authorization, audit logging, target inventory isolation, failure handling, and retention model before enabling live OLVM, SSH, or collection execution.
- Run integration tests against approved non-production target hosts only after that execution environment exists. Local tests deliberately do not connect to infrastructure.
- Approve a Grafana deployment separately. Grafana needs its own `grafana_db` and read-only access only to curated `sentinel_db.reporting` views; do not add embedding, SSO, or public/outbound links beforehand.
- Retain existing manual-host and OLVM ownership rules. Manual-to-OLVM conversion, automatic OLVM pruning, and hard deletion remain separate future workflows requiring an explicit retention and confirmation design.

## Validation Record

Validated locally on September 29, 2026:

- `docker compose exec -T api pytest -q tests` - 68 passed.
- Internal audit regression coverage verified API error redaction, request
  correlation IDs, deduplication, and one-event handling for an unhandled 500.
- A live, temporary audit-mode 404 was exported as redacted NDJSON and then
  removed; the local stack returned to its default disabled audit mode.
- Disposable Forgejo integration harness - 1 passed, with its temporary Forgejo resources cleaned up.
- `python -m py_compile sentinel/*.py sentinel/routers/*.py main.py`, `node --check app.js`, and `docker compose config --quiet` completed successfully.
- The `api`, `worker`, `dashboard`, `postgres`, and internal `forgejo` services were rebuilt and running locally.

No actual target host, OLVM engine, SSH endpoint, OpenBao instance, Grafana instance, or production Ansible execution was contacted.
