# Sentinel System Design

## Purpose And Scope

Sentinel is an infrastructure information-gathering and dashboard application.
It is implemented as a modular monolith and developed locally with Docker
Desktop. The stack contains an Nginx frontend, FastAPI API, PostgreSQL, a
separately supervised Sentinel scheduler worker, and an internal Forgejo
service for the private playbook repository.

Sentinel gathers approved read-only facts from source systems, validates the
resulting artifacts, and projects current inventory, historical reporting,
freshness, capacity, and alert data for the portal and Grafana. The production
execution path uses explicit source configuration, scoped secret references,
immutable playbook provenance, and source-specific adapters. A development
environment without a configured adapter reports unavailable sources or no
collected data; it does not generate a successful result as a substitute for
target contact.

## Application Architecture

The application remains one Sentinel deployment. The dashboard/API process and
separately supervised worker are processes of the same modular monolith, with
one codebase, release, PostgreSQL schema, and domain contracts.
Domain code is organized inside `backend/sentinel`:

- `routers`: HTTP request and response translation by workspace
- `inventory`: manual-host rules and OLVM reconciliation rules
- `managers`, `credentials`, `profiles`, and `collections`: domain behavior
- `records`, `validation`, and `bootstrap`: shared persistence, validation,
  startup, and migration support
- `dependencies`: integration-client construction
- `audit`: opt-in, redacted diagnostic-event persistence and local NDJSON
  export for controlled production test drives

`backend/main.py` is the stable Uvicorn entry point and compatibility facade.
New package modules must not import it. External clients, such as Forgejo,
remain isolated from UI code and unrelated domains.

The existing `/api` request and response contracts are stable. Contract changes
require an explicit design and matching regression coverage.

Sentinel already runs a dedicated scheduler worker using the same codebase and
PostgreSQL database. Production collection execution remains part of that
modular monolith; collection execution, ingestion, and reporting must not
become independently versioned microservices.

## Inventory Ownership

Every host has exactly one owner:

| Source type | Owner | Lifecycle and update rules |
| --- | --- | --- |
| `olvm` | OLVM dynamic inventory sync | The OLVM adapter discovers and reconciles VM inventory through stable Engine identity. It never overwrites manual hosts or hard-deletes a missing resource. |
| `manual` | Human operator | Operators create, edit, disable, and decommission the host. OLVM reconciliation cannot modify it. |

The UI identifies source ownership in host lists and details. OLVM hosts show
their originating manager; manually managed hosts show `Manual`.

At creation or source conversion, Sentinel rejects duplicate management
addresses and duplicate host names. It does not silently replace or merge
records. A future manual-to-OLVM conversion requires an explicit operator
action with collision review.

## OLVM Reconciliation

`backend/sentinel/olvm.py` performs bounded, read-only Engine REST connection
tests and VM listing. `backend/sentinel/routers/managers.py` applies the
authoritative successful response through the ownership, identity, collision,
and pruning rules in `backend/sentinel/inventory.py`.

The collector may manage only hosts with `sourceType: "olvm"` and stable Engine
identity metadata. Manual and unresolved hosts are preserved. An OLVM resource
is identified by manager ID, Engine resource type, and Engine resource ID; a
display-name or address change must update that same identity rather than claim
another host.

OLVM pruning is opt-in. Missing authoritative runs increment a counter. Only
after the configured threshold can a resource become retained or
pending-prune, and a separate explicit prune request is required before it is
eligible for pruning. No current operation hard-deletes an OLVM host.

## Manual Hosts

The initial supported manual connection type is Linux/SSH; Sentinel does not
imply Windows or WinRM support.

A manual host has:

- display name and management address
- SSH user and port
- role, environment, and tags/groups
- lifecycle state: `active`, `disabled`, or `decommissioned`
- an external secret reference
- live connection-test and collection outcomes, when configured

Manual hosts participate in the Linux/SSH collection path when an enabled
profile is pinned to a reviewed Git commit and the runtime secret and route are
available. A collection refresh may update gathered facts but does not alter
source ownership. Retirement uses `disabled` or `decommissioned` first,
retaining collection history; hard deletion requires a future retention and
confirmation workflow.

## Credentials

Sentinel is independent of a specific secrets product. It retains only opaque,
provider-neutral references using `secret://sentinel/...`; raw passwords, SSH
keys, tokens, and other secret material must never enter the database, source
control, logs, browser storage, or UI list/detail views.

Manager and manual-host forms select references instead of accepting secrets.
At execution time, a source adapter resolves the scoped reference and performs
only its approved read-only operation. Settings owns SSH key management: it can
retain an external reference or generate an Ed25519 key pair. For Sentinel-
generated keys, PostgreSQL and browser responses retain only the opaque
`secret://sentinel/managed-ssh/<id>` reference, public key, fingerprint,
algorithm, scope, state, and usage. The private half is written with mode
`0600` to the restricted runtime key volume; it is never returned, logged,
exported, or stored in PostgreSQL. The Credentials workspace owns API, token,
username/password, and other service-reference metadata. A saved reference
update cascades to dependent profiles, managers, and manual hosts, except that
a Sentinel-managed SSH key reference is immutable.

## Collection Profiles And Playbooks

Collection profiles belong in the Collections workspace alongside run history.
They are read-only fact gathering and must not provision, mutate, or remediate
infrastructure.

A profile references a versioned playbook path and Git revision, inventory
scope, a five-field Linux cron schedule in UTC, and an external secret
reference.
Schedules are persisted as cron expressions, translated into a human-readable
description in the portal, and are not represented by a single global interval.
An empty schedule is manual-only. The local scheduler may queue only enabled
profiles whose source is pinned to an immutable reviewed Git commit; it records
each profile/minute slot in PostgreSQL to prevent duplicate queueing.

Playbook source is Git-owned in the private `sentinel-playbooks` repository.
Profiles retain repository metadata, path, immutable commit SHA, scope,
schedule, and external secret reference, but not source text. A browser edit creates
or updates a deterministic Forgejo profile-draft branch and pull request to
protected `main`; PostgreSQL retains only branch, pull-request, commit, and
review metadata. Sentinel cannot approve or merge that pull request. After an
authorized human merge, an operator explicitly pins the protected-`main`
commit. Only `source.state: pinned` is runnable; migration-pending and draft
sources are rejected by collection-run and syntax-check routes.

Syntax check fetches the selected pinned Forgejo commit and runs
`ansible-playbook --syntax-check` with an empty inventory. It validates
playbook syntax without contacting a target host and is separate from a
collection run. The scheduler reserves an eligible profile/minute slot, then
dispatches a read-only collection run to the approved execution boundary.

### Information Gathering And Ingestion Design

This is Sentinel's operating design for read-only infrastructure collection.
The implemented Linux executor resolves runtime-only secrets, uses a temporary
Ansible inventory, runs an immutable Git-pinned facts-only Ansible playbook,
writes sanitized artifacts atomically, and projects typed Linux system identity,
filesystem, and capacity facts. The deployed OLVM adapter performs separate
inventory reconciliation.
Sources remain unavailable until their secret material and approved network
route are configured.

#### Module Boundaries

The implementation uses focused modules under `backend/sentinel`:

- `collections`: profile validation, collection-run lifecycle, and user-visible
  run history
- `scheduling`: due-profile evaluation and idempotent run reservation
- `execution`: dynamic-target Ansible dispatch with source-scoped credentials
  and sanitized artifact construction
- `artifacts`: atomic protected-filesystem storage and checksum-verified reads
  for sanitized immutable artifacts
- `ingestion`: artifact receipt, contract validation, idempotency, error
  handling, and database projection
- `facts`: Linux, OLVM, and Kubernetes record validation and projection rules
- `reporting`: Grafana-safe views over accepted operational data

These modules communicate through database records and versioned collection
artifacts, not through browser payloads or cross-domain table writes. The
scheduler worker is a separately supervised process of the Sentinel deployment
rather than a second application. It uses the same release, migrations, and
domain contracts as the API. The execution loop belongs in that same worker
boundary.

#### Schedule And Dispatch Ownership

Sentinel is the only authority that evaluates profile schedules and creates
collection runs. The scheduler reserves each `(profile_id, scheduled_for)` slot
before dispatch so retries or multiple worker starts cannot create duplicates.
The current Compose worker is the only local schedule authority; systemd will
supervise the same Sentinel worker on the production Linux server and must not
define a competing timer for each profile. Cron is not an additional scheduler.

AWX may later be selected as an Ansible execution backend. In that case,
Sentinel still creates and owns the run and AWX executes only the dispatched
job. AWX schedules must remain disabled for Sentinel-managed profiles.

Each runnable profile is pinned to a reviewed immutable Git commit. Profiles
must be split when their schedules, source types, target scopes, or credential
scopes differ. In particular, a Kubernetes credential reference must be bound
to the configured cluster or source instance it can access; one broad
`kubernetes_clusters` group must not imply universal access.

#### Execution And Artifact Contract

For every run, Sentinel records the profile ID, trigger, pinned repository
path and commit SHA, expected source instances, and an immutable run ID before
dispatch. The execution worker derives its target inventory from Sentinel's
owned configuration at that point. It does not trust a static inventory group
to establish ownership.

The current Linux runner allows only Ansible facts-only plays: every play must
set `gather_facts: true` and cannot contain tasks, privilege escalation,
includes, roles, or other execution controls. A controller-side callback emits
only a small allowlisted fact subset into a temporary NDJSON event file; normal
Ansible stdout is never an ingestion interface. Parallel target support must
keep isolated events and deterministically merge them before artifact creation.

The manifest records the run ID, artifact paths, schema versions, record counts,
checksums, collection window, source instances, collector version, and final
execution status. Artifacts are uploaded atomically before ingestion begins.
The raw archive is named a *sanitized immutable collection artifact*: it may
contain only validated and redacted canonical records, never unrestricted
Ansible output.

Every NDJSON record must include:

- `schemaVersion`, `recordType`, `runId`, deterministic `recordKey`, and
  sequence number
- `collectedAt` and, where distinct, `observedAt`
- Sentinel-owned source identity and source type
- stable resource identity, resource kind, and display attributes
- normalized typed payload and playbook/collector provenance

Identity rules are source-specific. Linux records bind to the Sentinel host ID
and may retain machine identity as a collected attribute. OLVM records bind to
manager ID, Engine resource type, and Engine resource ID. Kubernetes object
UIDs are valid only within a configured Sentinel cluster/source instance, so
the source instance is always part of the record key.

#### Ingestion And Projection

Ingestion is at-least-once and idempotent. It verifies the manifest checksum,
run/source binding, supported schema version, record type, source identity,
record-key uniqueness, timestamps, and normalized units before projecting any
record. CPU is stored in millicores and memory or storage in bytes.

The collection run moves through `queued`, `dispatched`, `running`,
`artifact-uploaded`, `ingesting`, and terminal `completed`, `partial`, or
`failed` states. A run is `completed` only after its artifact is accepted and
all required projections succeed. Per-target failures may produce `partial`;
they must be retained as explicit run results and errors rather than being
silently treated as absent data.

The database stores an artifact receipt and per-record ingestion ledger with
the artifact location, checksum, record key, acceptance state, and diagnostic
metadata. Replaying an accepted artifact must be harmless. Failed records are
quarantined with structured diagnostics; they cannot partially mutate a current
projection.

Current fact projections and historical snapshots are separate. Accepted facts
may update source-specific `*_current` tables and append capacity or usage
snapshots. A generic collector must never update `sentinel.hosts` ownership or
lifecycle fields. Only authoritative OLVM reconciliation may create or update
OLVM-owned inventory using stable Engine identity; it must preserve manual and
unresolved hosts and must not prune from a partial or failed run.

The existing collection-run and reporting tables are the migration starting
point. The artifact-ingestion migration is versioned as
`2026-09-28-artifact-ingestion-v1` and adds the receipt, ledger, structured
error, source-instance, projection, and Linux reporting-view foundation.
Execution-mode extensions must not create a parallel, competing `collector_run`
model. New ingestion tables and all constraint changes require explicit,
versioned PostgreSQL migrations with replay and rollback coverage.

#### Storage, Retention, And Reporting

Sanitized immutable artifacts are retained in controlled object storage or a
protected filesystem location. The filesystem writer performs atomic gzip
writes and checksum-verified reads; the production runner and retention
provider must use that contract. PostgreSQL retains artifact metadata,
checksums, receipt state, and operational projections; it does not need a
duplicate copy of every artifact payload. Access to artifacts follows
operational audit and retention policy.

Snapshot tables are partitioned by observation time and have a defined
retention and downsampling policy before high-cardinality Kubernetes
container/pod metrics are enabled. Large-directory collection has explicit
path allowlists, maximum depth, timeout, and frequency limits. A failed scan
must report its bounded failure without degrading unrelated collection runs.

Grafana receives a dedicated read-only database role with access only to the
curated `reporting` schema. Reporting views expose accepted current state,
historical capacity or usage, collection freshness, and failures; they do not
read raw artifacts, credentials, or operational write tables.

#### Credential And Output Safety

Only the execution environment resolves external secret references through an
approved deployment-specific mechanism. It uses source-scoped, short-lived
access where supported and must not pass secret material to the Sentinel API,
PostgreSQL, browser, logs, NDJSON artifacts, or Grafana. Collection tasks use
field allowlists and `no_log` where required; the ingestion contract rejects
unexpected secret-bearing fields.

#### Delivery Order

1. Define record schemas, manifests, stable source identities, redaction rules,
   retention, and PostgreSQL migrations.
2. Implement and test the Linux filesystem collection path, including
   interrupted runs, artifact replay, duplicate delivery, a reporting view, and
   a production runner that uses approved non-production targets first.
   The runner and isolated unit coverage are implemented; an approved
   non-production target integration test remains required before production
   enablement.
3. Add OLVM fact collection beyond the implemented VM inventory reconciliation.
4. Add Kubernetes object inventory and requested/allocatable capacity.
5. Enable Kubernetes pod or container metrics and large-directory scans only
   after their volume, retention, and failure limits are verified.
6. Provision Grafana with the reporting-only role after production views are
   validated.

### Legacy Playbook Migration

The legacy-profile migration and profile draft branches are the current Forgejo
write paths. The migration stages pre-existing JSONB source to an internal
migration branch and opens a pull request to protected `main`. It does not pin
profiles until an authorized human merges the pull request and Sentinel's
explicit finalization route verifies the protected-`main` bytes and merge
provenance. Normal profile edits may write only to their deterministic draft
branch and cannot approve, merge, or select that draft as runnable.

Migration failure leaves the original JSONB source in place. Temporary backup
data supports an explicit rollback and is not a runtime source for profiles.

## Data, Dashboards, And Deployment

The production target is a Linux server; Docker Desktop on macOS is for local
development only.

- PostgreSQL `sentinel_db` holds Sentinel operational data. Its curated
  `reporting` schema is read-only to Grafana.
- `grafana_db` is separate and holds Grafana configuration.
- Grafana is served through the authenticated Sentinel origin at `/grafana/`.
  The portal reads only Grafana health and dashboard catalog metadata through a
  Viewer service account stored in a Docker-managed runtime volume. Sentinel
  never proxies Grafana's write API or persists dashboard definitions.
- Forgejo hosts the private `sentinel-playbooks` repository inside the Compose
  stack. Its service token is available only through the internal secret mount.
- Sentinel owns operational inventory, freshness, run-state, integration
  status, and Grafana handoff. Grafana owns composed analytical dashboards,
  variables, panels, and layouts.
- Internal audit mode is disabled by default and enabled only from Sentinel's
  Settings workspace. When enabled for a controlled test drive, the API and
  scheduler record redacted errors in `sentinel.internal_audit_events`. Events
  are fingerprinted and deduplicated, retention is capped at 10,000 distinct
  fingerprints, and they can be exported as a redacted NDJSON file from the
  Settings workspace or API container. Request bodies and headers are never
  stored, and the portal never renders audit event contents.

Live OLVM, SSH, secret-provider, and Grafana integrations require explicit
deployment configuration, scoped access, source-specific failure handling,
audit coverage, and integration tests against approved non-production targets.

## Development Knowledge Graph

Graphify is local development tooling only. Its graph, cache, downloaded oVirt
corpus, and service state are ignored and excluded from the dashboard image.
Use `scripts/graphify-docker refresh` after changing meaningful source material
and `scripts/graphify-docker start` before graph-backed research.
