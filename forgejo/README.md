# Forgejo Service Access Contract

Forgejo is internal to Sentinel. It has no published host port and shares the
`playbooks` Docker network only with the Sentinel API, scheduler worker, and
bootstrap job.

## Identities

- `sentinel` owns the private `sentinel/sentinel-playbooks` repository. Its
  randomly generated bootstrap password is process-local and is never used as
  a Sentinel runtime credential.
- `sentinel-api` is a non-admin repository collaborator with `write` access to
  only `sentinel/sentinel-playbooks`. Its API token has only
  `read:repository` and `write:repository` scopes and is explicitly limited to
  that repository.

On the first development bootstrap, Forgejo creates the token and stores it in
the Docker-managed `forgejo-service-credentials` named volume. A one-shot
volume initializer assigns the rootless Forgejo UID access to that volume; the
API and scheduler worker mount it read-only at
`/run/secrets/forgejo_api_token` so either process can safely complete the
one-time startup migration. The `FORGEJO_API_TOKEN_FILE` setting is a path,
not a credential. The token must never be copied into Compose,
environment files, PostgreSQL, browser storage, HTTP responses, or logs.

The bootstrap retains the existing token on later runs. Rotation is an
explicit deployment action: revoke the old Forgejo token, remove the named
volume or its token file while the stack is down, then run the bootstrap job.

## Production Secret Source

Production replaces the development credential volume with a read-only secret
mount populated by the deployment runtime from OpenBao. The planned OpenBao
reference is `kv/sentinel/services/forgejo-api`; Sentinel configuration stores
that reference and the token-file path only. The deployment runtime resolves
the value and writes the mounted secret file without putting the raw token in
the Compose file, image, database, source tree, or service environment.

## Repository Governance

`main` is protected by Forgejo bootstrap policy:

- Direct pushes are disabled, including for administrators.
- A merge requires one approval.
- Rejected reviews, stale approvals, out-of-date branches, and unresolved
  official review requests block a merge.

The intended future workflow is: Sentinel writes each profile edit to its own
draft branch, opens a pull request, and exposes the PR status to an authorized
human reviewer. Only a reviewed merge or an explicitly pinned approved commit
may be selected for collection. The service identity never writes directly to
`main`.

Sentinel uses its scoped service credential to read pinned source and to create
or update a profile draft branch and pull request. It never approves or merges
that pull request, and the browser never receives a Forgejo credential. An
authorized reviewer performs approval and merge through the internal Forgejo
administration process. Production must provide that reviewer identity, audit
trail, backup, and recovery process; there is deliberately no public Forgejo
browser access from outside Docker.
