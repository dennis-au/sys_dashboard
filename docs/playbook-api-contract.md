# Playbook API Contract

Sentinel exposes profile playbooks through the private Forgejo repository. No
endpoint returns or stores a service token, external secret material, or
playbook source in PostgreSQL.

## Source States

Only `source.state: "pinned"` is runnable. `migration-pending-review`, draft,
and unresolved states receive `409` from collection-run and syntax-check APIs.
After source gating succeeds, Sentinel creates a read-only collection run for
the approved executor. A run records immutable source provenance and must
return an explicit unavailable or failed outcome when execution cannot begin.

## Read And History

- `GET /api/profiles/{profileId}/playbook`
  returns `{ profileId, source, content }` from the selected immutable Forgejo
  commit. `?revision={40-char-sha}` may inspect a commit in protected `main`
  history; it does not update the profile.
- `GET /api/profiles/{profileId}/playbook/history`
  returns the protected-`main` path history and the currently selected commit.
- `GET /api/profiles/{profileId}/playbook/review-status`
  returns migration or draft PR state observed from Forgejo. It performs no PR
  approval or merge operation.

## Draft And Revision Selection

- `PATCH /api/profiles/{profileId}/playbook` and
  `POST /api/profiles/{profileId}/playbook/draft` accept `{ "source": "..." }`.
  Sentinel writes the source to a deterministic profile draft branch, creates
  or reuses an open PR to protected `main`, and persists only draft metadata:
  branch, PR number, commit SHA, base, and state.
- `POST /api/profiles/{profileId}/playbook/pin` accepts
  `{ "commitSha": "..." }`. For a profile without a draft it selects a commit
  already reachable from protected `main`. For a profile with a draft, its PR
  must already be human-merged before pinning.

## Migration Finalization

`POST /api/playbook-migrations/{batchId}/finalize` observes the existing
migration PR. It proceeds only when the PR was merged by an authorized human,
then verifies the bytes of each legacy playbook at protected `main`, records
merge provenance, and changes profile source state to `pinned`. It is
idempotent and cannot approve or merge a PR.

## Syntax Check

`POST /api/profiles/{profileId}/syntax-check` takes no browser source; a
request containing `source` is rejected. Sentinel fetches the selected pinned
commit and invokes `ansible-playbook --syntax-check` in a temporary workspace
with an empty inventory. The response has `valid`, sanitized `diagnostics`,
`commitSha`, and `mode: "ansible-playbook-syntax-check"`; no target host,
OLVM, SSH, external secret provider, or Grafana service is contacted.
