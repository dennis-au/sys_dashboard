# Production Deployment

Sentinel can be deployed as one Docker Compose application on one Linux server.
The API and worker remain processes of the same modular monolith. Caddy is the
only public ingress; PostgreSQL and Forgejo stay on private Docker networks.

## Prerequisites

- Docker Engine with Docker Compose v2
- a DNS name whose A and, if used, AAAA record points to the server
- inbound TCP ports 80 and 443 permitted to Caddy
- a user allowed to access the Docker daemon
- outbound access to pull the base images and obtain a TLS certificate

The deployment is the operating base for Sentinel's read-only infrastructure
information gathering and dashboards. Before enabling any source, provision its
scoped secret reference, approved network route, source-specific adapter
configuration, playbook revision, inventory scope, retention policy, and
operator authorization. A source that is not configured or unavailable must be
reported as such; it must not generate synthetic collection success.

## Deploy

From the project directory on the server, run:

```sh
./scripts/deploy-production
```

On first use, the command asks for the public DNS name, ACME contact email, and
an administrator username and password. It creates a mode-`0700` local runtime
directory, generates a PostgreSQL password file, generates a Caddy Basic Auth
hash without retaining the raw administrator password, builds the production
application images, and starts the stack.

The script stores deployment settings, the portal administrator username, the
Caddy password hash, the database password file, and generated Grafana
administrator/configuration passwords under
`.sentinel-production/`, which is ignored by image builds and must be included
in the server backup policy. Do not commit that directory. Later runs reuse the
stored settings and do not prompt for the administrator password unless
`SENTINEL_ADMIN_PASSWORD` is deliberately supplied.

### Runtime Secrets

The deployer creates `.sentinel-production/secrets/` with mode `0700`, assigns
its contents to Sentinel's container UID/GID `1000:1000`, and mounts it
read-only at `/run/sentinel-secrets` for the API and worker. It is not
source-controlled. Create one mode-`0600` JSON file per opaque
`secret://sentinel/<path>` reference. For example,
`secret://sentinel/inventory/linux-prod-01` resolves to
`.sentinel-production/secrets/inventory/linux-prod-01`.

Each file is a JSON object containing only the runtime fields required by the
approved adapter: `username`, `password`, `token`, or `privateKey`. Do not add
secret values to Sentinel forms, PostgreSQL, Git, diagnostics exports, or
playbook source. The worker uses a temporary filesystem for SSH material and
retains only sanitized fact artifacts.

For strict SSH host verification, create a mode-`0600` `known_hosts` file in
the same runtime secrets directory. Verify target host fingerprints through an
approved out-of-band channel before adding them. Sentinel passes that file to
Ansible with `StrictHostKeyChecking=yes`; it does not accept host keys
automatically.

Sentinel-generated Ed25519 keys are different from operator-supplied runtime
references. Their private material is stored in the Docker-managed
`sentinel-managed-ssh` volume, mounted writable only by the API at
`/var/lib/sentinel/managed-ssh` and read-only by the worker. Include this
volume in the server backup policy. Sentinel records and displays the public
key and fingerprint only; it never offers private-key download or export.

### Grafana

Production Compose includes Grafana and its separate `grafana_db` database.
Grafana is available only through the authenticated Sentinel origin at
`https://<SENTINEL_DOMAIN>/grafana/`; it has no direct host-port mapping.
Grafana's datasource uses the `grafana_reader` PostgreSQL role, restricted to
read-only `sentinel_db.reporting` views.

On first startup, `grafana-bootstrap` creates a Viewer service account for the
Sentinel API and stores its token in the Docker-managed
`grafana-service-credentials` volume. The API reads only Grafana health and
dashboard catalog metadata with that token. Dashboard edits, users, and Grafana
login remain Grafana responsibilities. Include `grafana-data` and
`grafana-service-credentials` in the protected backup policy; never export the
service-account token from the server.

For unattended provisioning, provide the four first-run values in the command
environment. Prefer an approved secret-injection mechanism over shell history:

```sh
SENTINEL_DOMAIN=sentinel.example.com \
SENTINEL_ACME_EMAIL=ops@example.com \
SENTINEL_ADMIN_USERNAME=operator \
SENTINEL_ADMIN_PASSWORD='replace-with-a-unique-16-character-minimum-password' \
./scripts/deploy-production
```

## Properties

- HTTPS is automatically managed by Caddy for the configured DNS name.
- Caddy Basic Auth protects the portal, `/api/` endpoints, and the Grafana
  handoff at `/grafana/`.
- Application source is copied into build images; production Compose has no
  source bind mounts.
- API, worker, and dashboard run as non-root UIDs with read-only root
  filesystems and temporary writable filesystems.
- PostgreSQL receives its password through a Docker file secret. Sentinel builds
  its database URL in memory and does not place that password in Compose
  environment variables or a checked-in file.
- Docker volumes persist PostgreSQL, Forgejo, Grafana data, Caddy certificates,
  Caddy config, and the internal Forgejo and Grafana service credentials.
- Services use restart policies, health checks where an HTTP or database health
  probe exists, internal networks for data/playbook services, and bounded local
  container logs.
- Production collection remains read-only to target systems. Sentinel records
  source identity, immutable playbook provenance, collection outcomes, and
  validated fact artifacts before projecting data to the portal or Grafana.

## Operations

Use the generated environment file for Compose commands:

```sh
docker compose --env-file .sentinel-production/deployment.env -f compose.production.yaml ps
docker compose --env-file .sentinel-production/deployment.env -f compose.production.yaml logs -f
docker compose --env-file .sentinel-production/deployment.env -f compose.production.yaml down
```

`down` preserves persistent volumes. Back up the PostgreSQL, Forgejo, Grafana,
Caddy, and `.sentinel-production/` runtime data together and test restoration before
depending on the deployment operationally.

Changing the administrator password requires a deliberate redeploy:

```sh
SENTINEL_ADMIN_PASSWORD='replace-with-a-new-16-character-minimum-password' ./scripts/deploy-production
```

Avoid supplying this value through shell history or shared process listings;
the script otherwise prompts without echoing the password. Each deployment
recreates Caddy after the stack starts so an updated Basic Auth configuration is
applied immediately while its certificate and configuration volumes persist.
