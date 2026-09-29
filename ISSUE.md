# Remaining Delivery Gates

## Implemented

Sentinel now has a live, read-only operational path rather than simulated
manager and collection actions:

- Runtime-only `secret://sentinel/...` resolution reads constrained JSON
  material from `/run/sentinel-secrets`; secret values do not enter PostgreSQL,
  browser responses, audit events, artifacts, or object representations.
- The OLVM adapter performs bounded REST connection tests and VM inventory
  reads. A successful authoritative response reconciles stable VM identities,
  preserves manual-host collisions, records missing-resource streaks, and
  never hard-deletes inventory.
- The worker claims queued pinned-profile runs, creates a temporary dynamic
  SSH inventory, executes a facts-only immutable Ansible playbook, emits a
  restricted fact subset, writes a checksum-verified artifact, and ingests
  filesystem and capacity facts. It records success, partial, unreachable,
  failed, and skipped target outcomes explicitly.
- The summary reports `operational`, `degraded`, or `unavailable` from live
  run state. Historical simulation rows remain readable but cannot make the
  portal look operational.
- Development and production Compose files use a target-facing collection
  network only for API/worker, an artifact volume for the worker, and a
  read-only runtime secret directory. The production deployer creates that
  directory with restrictive permissions.

## Required Before Production Enablement

- Run an approved non-production integration test against an OLVM Engine.
  The Linux SSH collection path is validated; unit tests continue to use mocks
  and must never contact customer or production infrastructure.
- Approve the actual secret-injection ownership, target-network ACLs,
  playbook-review identity, artifact retention, backup/recovery, and incident
  export process for the Linux server.
- Approve the Linux-server operational controls for the Grafana deployment:
  protected backup of `grafana-data` and its runtime service credential, Caddy
  access policy, Grafana administrator rotation, and recovery testing. SSO and
  unauthenticated public dashboard links remain out of scope.

## Planned Enhancements

- OLVM fact collection beyond VM inventory reconciliation.
- Kubernetes source adapters and high-cardinality metrics after retention and
  volume controls are approved.
- Explicit manual-to-OLVM conversion, operator-confirmed pruning, and
  retention-backed hard deletion workflows.

## Validation Record

Validated on September 29, 2026:

- Isolated full backend suite on macOS and `192.168.0.111`: `89 passed`; one
  Starlette deprecation warning. `scripts/test-isolated` created and removed
  temporary containers and volumes, leaving collected data intact.
- `docker compose config --quiet` and production Compose configuration check
  on `192.168.0.111`.
- `bash -n scripts/deploy-production`.
- Strict SSH host-key verification and Ansible connectivity from Sentinel on
  `192.168.0.111` to the non-production Linux target `192.168.0.110`.
- Facts-only pinned playbook collection run
  `run-6063fa01612448c8b2b7e5ed922ed172`, using reviewed commit
  `cca48ae32d55ac249b8a0e3b6a61a825a546d657`, completed successfully.
- PostgreSQL reporting views contain the target's Red Hat family, CentOS 9,
  kernel, architecture, CPU, and memory facts.
- Grafana `Sentinel Linux facts` dashboard and its reporting-only datasource
  returned the persisted target facts successfully.
- Sentinel's dashboard portal discovered Grafana through its Viewer service
  account, displayed the live dashboard catalog, and handed off through
  `/grafana/` without exposing a native Grafana host port or returning any
  Grafana credential material.
- The Grafana subpath proxy served the actual Grafana CSS and JavaScript assets
  and rendered the Grafana login form in a fresh browser session.
- Grafana administrator password reset was validated through Sentinel Settings:
  the API returned only the administrator name and status, Grafana accepted the
  replacement, and the retained private-volume credential survived a forced
  Grafana container recreation.

No live OLVM Engine or external secret provider was contacted during this
validation.
