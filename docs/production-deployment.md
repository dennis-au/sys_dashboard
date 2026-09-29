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

No target infrastructure credentials or live collection configuration are part
of this deployment. The deployed application remains simulation-only until a
separate source-specific execution approval is implemented.

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

The script stores only non-secret deployment settings, the administrator
username, the Caddy password hash, and the database password file under
`.sentinel-production/`, which is ignored by image builds and must be included
in the server backup policy. Do not commit that directory. Later runs reuse the
stored settings and do not prompt for the administrator password unless
`SENTINEL_ADMIN_PASSWORD` is deliberately supplied.

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
- Caddy Basic Auth protects both the portal and `/api/` endpoints.
- Application source is copied into build images; production Compose has no
  source bind mounts.
- API, worker, and dashboard run as non-root UIDs with read-only root
  filesystems and temporary writable filesystems.
- PostgreSQL receives its password through a Docker file secret. Sentinel builds
  its database URL in memory and does not place that password in Compose
  environment variables or a checked-in file.
- Docker volumes persist PostgreSQL, Forgejo, Caddy certificates, Caddy config,
  and the internal Forgejo service token.
- Services use restart policies, health checks where an HTTP or database health
  probe exists, internal networks for data/playbook services, and bounded local
  container logs.

## Operations

Use the generated environment file for Compose commands:

```sh
docker compose --env-file .sentinel-production/deployment.env -f compose.production.yaml ps
docker compose --env-file .sentinel-production/deployment.env -f compose.production.yaml logs -f
docker compose --env-file .sentinel-production/deployment.env -f compose.production.yaml down
```

`down` preserves persistent volumes. Back up the PostgreSQL, Forgejo, Caddy,
and `.sentinel-production/` runtime data together and test restoration before
depending on the deployment operationally.

Changing the administrator password requires a deliberate redeploy:

```sh
SENTINEL_ADMIN_PASSWORD='replace-with-a-new-16-character-minimum-password' ./scripts/deploy-production
```

Avoid supplying this value through shell history or shared process listings;
the script otherwise prompts without echoing the password. Each deployment
recreates Caddy after the stack starts so an updated Basic Auth configuration is
applied immediately while its certificate and configuration volumes persist.
