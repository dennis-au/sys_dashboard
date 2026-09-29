# Sentinel Dashboard

Sentinel is an infrastructure inventory dashboard. The local Docker Desktop stack contains:

- Nginx dashboard at `http://localhost:8080`
- FastAPI service proxied at `/api/`
- PostgreSQL `sentinel_db` for local operational data
- Forgejo, available only to Sentinel's internal API and scheduler worker at
  `http://forgejo:3000`, for the self-hosted `sentinel-playbooks` repository

The browser UI loads and persists hosts, OLVM managers, external secret-reference metadata, collection profiles, run history, and Grafana catalog entries through the API. Sentinel uses internal Forgejo only for Git-owned playbook source and target-free syntax checks. It does not contact OLVM, SSH, an external secret provider, Grafana, or other production infrastructure: connection tests and collection actions are explicit simulations.

## Production Deployment

Deploy Sentinel on one Linux server with Docker Compose by running:

```sh
./scripts/deploy-production
```

The command creates the local deployment runtime, builds the production images,
and starts the complete application behind HTTPS and Basic Auth. Read the
[production deployment guide](docs/production-deployment.md) for prerequisites,
backup expectations, password rotation, and operational commands.

## OLVM Provenance

Each OLVM-owned host records its configured manager ID, immutable Engine resource type and ID, last authoritative run ID, and reconciliation state. PostgreSQL enforces one host per `(sourceManagerId, engineResourceType, engineResourceId)` through `sentinel.olvm_host_identities`; display names and management addresses remain renameable attributes and cannot become an ownership key.

Legacy OLVM records that cannot be mapped deterministically remain `legacy-unresolved` and render an actionable provenance state in the portal; Sentinel does not assign a guessed manager. Manual hosts remain outside the OLVM identity table, and a future OLVM reconciliation must reject, never overwrite, a manual name or address collision.

Missing authoritative runs do not remove a host. The internal reconciliation model records `missing` state first, reaches `retained` only after the configured threshold, and marks a resource `pending-prune` only when a caller explicitly requests pruning. No live collector or pruning route is enabled in this build.

## Playbook Repository

Forgejo is the internal self-hosted Git service for playbook source. It has no host HTTP or SSH port mapping and shares an internal Docker network only with the Sentinel API and worker; neither the dashboard nor the host can reach `http://forgejo:3000`.

On a fresh `forgejo-data` volume, `forgejo-bootstrap` locks Forgejo installation, creates the internal non-admin `sentinel` repository owner, and creates the private `sentinel-playbooks` repository with a `main` branch. No browser-based installation or administrator setup is required. The bootstrap uses a runtime-only random password and does not write it to Compose, logs, or the repository.

The dashboard and API share the `application` network so browser requests to `/api/` continue to work. PostgreSQL and Forgejo each use separate internal networks that have the API as their only common service.

Forgejo data, including its SQLite database and Git repositories, is stored in the Docker-managed `forgejo-data` volume. Embedded SSH is disabled. The API reads its narrowly scoped service token from the internal secret mount only when it checks Forgejo readiness or resolves Git-owned playbook metadata. `GET /api/integrations/forgejo/readiness` verifies Forgejo health and authenticated access to the prepared private repository; it never returns credential material.

### Playbook Migration

A clean installation creates an empty private Forgejo repository and no collection profiles. Sentinel only runs the legacy JSONB-to-Git migration when an existing database contains profiles with `playbookContent`. In that case it stages the original bytes on a review branch and opens a pull request to protected `main`; it never writes directly to protected `main`.

While a migration pull request awaits review, the profile source exposes the repository, path, full immutable commit SHA, migration branch, pull-request number, and `migration-pending-review` state. Source text is removed from normal profile API responses and from the `sentinel.collection_profiles` profile payload. The temporary JSONB backup table is used only for rollback; it is not a runtime source of playbook content.

To roll back a `pending_review` batch before that approval, identify its batch ID from the profile `source.migration.batchId`, then run:

```sh
docker compose exec -T api python -m playbook_migration rollback --batch-id <batch-id>
```

Rollback restores the original legacy profile payloads from `sentinel.collection_profile_playbook_backups` and marks the batch `rolled_back`; it does not delete the Forgejo branch or pull request. Resolve the cause, then set `SENTINEL_PLAYBOOK_MIGRATION_RESET=1` for a deliberate subsequent startup migration. Keep the backup until the reviewed Git revision is accepted and an operational retention policy has been approved.

Creating or updating a profile requires `source.repository`, `source.path`, and an existing immutable 40-character `source.commitSha`; Sentinel verifies the path at that exact commit. Browser playbook edits are committed only to a deterministic profile draft branch, with a pull request to protected `main`; PostgreSQL retains draft metadata but never a local working copy of source text. After an authorized merge, an operator explicitly pins the protected-main commit.

## Run locally

```sh
cp .env.example .env
# Replace POSTGRES_PASSWORD in .env with a long random alphanumeric value.
docker compose up --build -d
```

Open `http://localhost:8080`.

The HTML, CSS, and JavaScript are bind-mounted into the dashboard container. Refresh the browser after editing any of those files. Rebuild the stack after changing backend code, dependencies, Dockerfiles, Nginx, or Compose configuration.

A new PostgreSQL volume starts empty: no hosts, managers, credential references, profiles, dashboards, alerts, runs, or generated playbooks are inserted. Start by recording an external secret reference, then add an OLVM manager or a manual host. Actions remain clearly labelled simulations and do not contact target infrastructure.

## Internal Audit Mode

For a controlled production test drive, enable **Internal diagnostics** from the Sentinel Settings workspace. The setting applies immediately to the API and scheduler and persists in PostgreSQL; it does not require a Compose or environment change. Sentinel then stores API and scheduler errors in `sentinel.internal_audit_events`. Each record is structured, fingerprinted, and deduplicated with an occurrence count; HTTP request bodies and headers are never stored, and secret-like values in exception text or context are redacted.

Use **Export NDJSON** in Settings to download the retained redacted diagnostics. The command-line exporter remains available for a server-side collection path:

```sh
docker compose exec -T api python -m sentinel.audit_export --limit 1000 > sentinel-audit.ndjson
```

The browser export is a download only; the portal never renders audit event content. Keep the exported file within the approved incident-handling path and share only the redacted export needed for diagnosis. The table retains at most 10,000 distinct error fingerprints.

## Regression tests

Run the API regression suite against the Docker services:

```sh
docker compose exec -T api pytest -q
```

The suite covers empty bootstrap behavior, manual-host validation and persistence, external-secret-reference cascades, Git-pinned profile metadata, source-resolution failures, byte-preserving migration staging and finalization, OLVM provenance and reconciliation collision rules, artifact ingestion replay and rollback, scheduler ownership, playbook syntax checks, and simulated manager/profile actions. Browser regression should additionally verify a create/edit action survives a reload and that the browser console is clean.

The Forgejo client unit suite uses a mocked Forgejo transport and runs with the standard command. The readiness endpoint has been verified against the local prepared private repository, including a denied anonymous repository read.

The full Forgejo write-path integration test is opt-in. It starts a separate
rootless Forgejo Compose project with its own Docker volumes, creates a private
repository and repository-limited runtime token, exercises the real client,
then removes that project and its volumes even if the test fails. It has no
published host port and never reads, writes, or stops the development stack.

```sh
./tests/forgejo-integration/run.sh
```

It verifies Forgejo health and authenticated private repository access, branch
creation, byte-preserving file commit/read, pull request creation, denied
direct writes to protected `main`, and denied anonymous or invalid-token
repository access. The disposable token is generated only inside the bootstrap
container and is never printed.

```sh
docker compose exec -T api pytest -q tests/test_forgejo_client.py
```

The unit suite is safe to run against any development environment because it has no Forgejo network or repository side effects.

## Stop

```sh
docker compose down
```

To discard only this project's local test-drive data and return to a clean first-run state:

```sh
docker compose down -v
```
