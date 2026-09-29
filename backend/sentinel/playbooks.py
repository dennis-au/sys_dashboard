"""Git-backed collection-profile source and local syntax-check workflow.

The functions in this module deliberately keep playbook bytes transient.  A
profile stores immutable Git metadata and optional draft/PR metadata only;
source text is fetched from Forgejo when it is needed and is never written to
PostgreSQL.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable

from fastapi import HTTPException

from forgejo_client import (
    ForgejoClient,
    ForgejoConfigurationError,
    ForgejoError,
    ForgejoFile,
    ForgejoHistoryEntry,
    ForgejoPullRequest,
    ForgejoRequestError,
)


PINNED_SOURCE_STATE = "pinned"
SYNTAX_CHECK_MODE = "ansible-playbook-syntax-check"
MAX_PLAYBOOK_BYTES = 1024 * 1024


def require_commit_sha(value: Any) -> str:
    if not isinstance(value, str):
        raise HTTPException(status_code=422, detail="An immutable 40-character Git commit SHA is required.")
    normalized = value.strip().lower()
    if len(normalized) != 40 or any(character not in "0123456789abcdef" for character in normalized):
        raise HTTPException(status_code=422, detail="An immutable 40-character Git commit SHA is required.")
    return normalized


def profile_source(profile: dict[str, Any]) -> dict[str, Any]:
    source = profile.get("source")
    if not isinstance(source, dict):
        raise HTTPException(status_code=409, detail="Collection profile source metadata is unavailable.")
    return source


def source_state(profile: dict[str, Any]) -> str:
    state = profile_source(profile).get("state")
    return state if isinstance(state, str) else "unresolved"


def require_runnable_source(profile: dict[str, Any]) -> dict[str, Any]:
    source = profile_source(profile)
    if source.get("state") != PINNED_SOURCE_STATE:
        raise HTTPException(
            status_code=409,
            detail="Collection profile source is awaiting Git review and cannot be run or syntax checked.",
        )
    _source_path(source)
    require_commit_sha(source.get("commitSha"))
    return source


def _source_path(source: dict[str, Any]) -> str:
    value = source.get("path")
    if not isinstance(value, str):
        raise HTTPException(status_code=409, detail="Collection profile source metadata is unavailable.")
    path = value.strip().lstrip("/")
    if not path or any(part in {"", ".", ".."} for part in path.split("/")):
        raise HTTPException(status_code=409, detail="Collection profile source metadata is unavailable.")
    return path


def _repository(client: ForgejoClient, source: dict[str, Any], *, require_main: bool = False):
    try:
        repository = client.repository_metadata()
    except ForgejoConfigurationError as exc:
        raise HTTPException(status_code=503, detail="Forgejo service credential is unavailable.") from exc
    except ForgejoError as exc:
        raise HTTPException(status_code=503, detail="Forgejo playbook source is unavailable.") from exc
    requested = source.get("repository")
    if not isinstance(requested, str) or requested != repository.full_name:
        raise HTTPException(status_code=409, detail="Collection profile source repository is unavailable.")
    if not repository.private:
        raise HTTPException(status_code=503, detail="Forgejo playbook repository is not available for Sentinel.")
    if require_main and repository.default_branch != "main":
        raise HTTPException(status_code=503, detail="Forgejo protected main branch is unavailable.")
    return repository


def _read_file(client: ForgejoClient, path: str, commit_sha: str) -> ForgejoFile:
    try:
        revision = client.revision(commit_sha)
        resolved = require_commit_sha(revision.sha)
        if resolved != commit_sha:
            raise HTTPException(status_code=422, detail="The selected Git revision did not resolve to the requested commit.")
        return client.read_file(path, resolved)
    except HTTPException:
        raise
    except ForgejoConfigurationError as exc:
        raise HTTPException(status_code=503, detail="Forgejo service credential is unavailable.") from exc
    except ForgejoRequestError as exc:
        if exc.status_code == 404:
            raise HTTPException(status_code=422, detail="The selected Git playbook path or commit is unavailable.") from exc
        raise HTTPException(status_code=503, detail="Forgejo playbook source is unavailable.") from exc
    except ForgejoError as exc:
        raise HTTPException(status_code=503, detail="Forgejo playbook source is unavailable.") from exc


def read_profile_playbook(
    profile: dict[str, Any], client: ForgejoClient, requested_revision: str | None = None
) -> dict[str, Any]:
    """Return Forgejo content for the profile's selected immutable source.

    A historic SHA may only be read when it is in the file history from the
    protected main branch.  The current migration SHA remains readable so an
    operator can inspect the PR before approving it, but it never becomes a
    runnable revision.
    """
    source = profile_source(profile)
    path = _source_path(source)
    selected_sha = require_commit_sha(source.get("commitSha"))
    repository = _repository(client, source)
    revision = selected_sha
    if requested_revision is not None:
        revision = require_commit_sha(requested_revision)
        if revision != selected_sha:
            try:
                history = client.file_history(path, repository.default_branch)
            except ForgejoConfigurationError as exc:
                raise HTTPException(status_code=503, detail="Forgejo service credential is unavailable.") from exc
            except ForgejoError as exc:
                raise HTTPException(status_code=503, detail="Forgejo playbook history is unavailable.") from exc
            if revision not in {entry.sha.lower() for entry in history}:
                raise HTTPException(status_code=422, detail="The selected revision is not in protected main history.")
    file = _read_file(client, path, revision)
    try:
        content = file.content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=422, detail="The selected Git playbook is not UTF-8 text.") from exc
    return {
        "profileId": profile["id"],
        "source": {
            "repository": repository.full_name,
            "path": file.path,
            "commitSha": revision,
            "state": source.get("state", "unresolved"),
        },
        "content": content,
    }


def profile_playbook_history(profile: dict[str, Any], client: ForgejoClient) -> dict[str, Any]:
    source = profile_source(profile)
    path = _source_path(source)
    repository = _repository(client, source)
    selected_sha = require_commit_sha(source.get("commitSha"))
    state = source.get("state", "unresolved")
    try:
        if state != PINNED_SOURCE_STATE:
            revision = client.revision(selected_sha)
            history = [
                ForgejoHistoryEntry(
                    sha=require_commit_sha(revision.sha), message=revision.message, authored_at=None
                )
            ]
        else:
            history = client.file_history(path, repository.default_branch)
            if selected_sha not in {entry.sha.lower() for entry in history}:
                revision = client.revision(selected_sha)
                history.insert(
                    0,
                    ForgejoHistoryEntry(
                        sha=require_commit_sha(revision.sha), message=revision.message, authored_at=None
                    ),
                )
    except ForgejoConfigurationError as exc:
        raise HTTPException(status_code=503, detail="Forgejo service credential is unavailable.") from exc
    except ForgejoError as exc:
        raise HTTPException(status_code=503, detail="Forgejo playbook history is unavailable.") from exc
    return {
        "profileId": profile["id"],
        "source": {
            "repository": repository.full_name,
            "path": path,
            "commitSha": selected_sha,
            "state": state,
        },
        "revisions": [
            {
                "commitSha": require_commit_sha(entry.sha),
                "message": entry.message,
                "authoredAt": entry.authored_at,
                "selected": entry.sha.lower() == selected_sha,
            }
            for entry in history
        ],
    }


def _draft_branch(profile_id: Any) -> str:
    identifier = re.sub(r"[^a-z0-9._-]+", "-", str(profile_id).lower()).strip(".-")
    if not identifier:
        raise HTTPException(status_code=409, detail="Collection profile has no stable identifier for a Git draft.")
    return f"sentinel/profile-{identifier[:48]}"


def _source_bytes(payload: dict[str, Any]) -> bytes:
    source = payload.get("source")
    if not isinstance(source, str):
        raise HTTPException(status_code=422, detail="Draft playbook source is required.")
    source_bytes = source.encode("utf-8")
    if not source_bytes:
        raise HTTPException(status_code=422, detail="Draft playbook source is required.")
    if len(source_bytes) > MAX_PLAYBOOK_BYTES:
        raise HTTPException(status_code=422, detail="Draft playbook source exceeds the 1 MiB limit.")
    return source_bytes


def _ensure_draft_branch(client: ForgejoClient, branch: str, base_branch: str) -> None:
    try:
        client.create_branch(branch, base_branch)
    except ForgejoRequestError as exc:
        if exc.status_code != 409:
            raise
        # The name alone is not proof that the branch is usable. Resolve it
        # before writing so a stale/conflicting Forgejo response cannot become
        # a silent update target.
        resolved = require_commit_sha(client.revision(branch).sha)
        if not resolved:
            raise HTTPException(status_code=409, detail="Collection profile draft branch is unavailable.")


def create_or_update_profile_draft(
    profile: dict[str, Any], payload: dict[str, Any], client: ForgejoClient
) -> dict[str, Any]:
    """Commit browser-supplied source only to a profile draft branch and PR."""
    source = require_runnable_source(profile)
    draft_bytes = _source_bytes(payload)
    path = _source_path(source)
    repository = _repository(client, source, require_main=True)
    branch = _draft_branch(profile.get("id"))
    try:
        _ensure_draft_branch(client, branch, repository.default_branch)
        current_sha = None
        try:
            existing = client.read_file(path, branch)
        except ForgejoRequestError as exc:
            if exc.status_code != 404:
                raise
            existing = None
        if existing is not None and existing.content == draft_bytes:
            commit_sha = require_commit_sha(client.revision(branch).sha)
        else:
            commit = client.create_or_update_file(
                path,
                branch,
                draft_bytes,
                f"Update collection profile {profile['id']} draft",
                current_sha=existing.sha if existing is not None else current_sha,
            )
            commit_sha = require_commit_sha(commit.commit_sha)
        pull_request = client.find_open_pull_request(branch, repository.default_branch)
        if pull_request is None:
            pull_request = client.create_pull_request(
                branch,
                repository.default_branch,
                f"Update collection profile {profile['name']}",
                "Sentinel-created collection playbook draft. An authorized human reviewer must approve and merge it.",
            )
    except HTTPException:
        raise
    except ForgejoConfigurationError as exc:
        raise HTTPException(status_code=503, detail="Forgejo service credential is unavailable.") from exc
    except ForgejoRequestError as exc:
        if exc.status_code in {409, 422}:
            raise HTTPException(status_code=409, detail="Forgejo rejected the collection profile draft update.") from exc
        raise HTTPException(status_code=503, detail="Forgejo playbook draft is unavailable.") from exc
    except ForgejoError as exc:
        raise HTTPException(status_code=503, detail="Forgejo playbook draft is unavailable.") from exc

    metadata = {
        "branch": branch,
        "pullRequestNumber": pull_request.number,
        "commitSha": commit_sha,
        "state": pull_request.state,
        "base": repository.default_branch,
    }
    updated = dict(profile)
    updated_source = dict(source)
    updated_source["draft"] = metadata
    updated["source"] = updated_source
    return updated


def _pull_request_metadata(pull_request: ForgejoPullRequest) -> dict[str, Any]:
    return {
        "number": pull_request.number,
        "state": pull_request.state,
        "merged": pull_request.merged,
        "head": pull_request.head,
        "base": pull_request.base,
        "mergeCommitSha": pull_request.merge_commit_sha,
        "mergedAt": pull_request.merged_at,
    }


def _review_request(source: dict[str, Any]) -> tuple[dict[str, Any], str] | None:
    draft = source.get("draft")
    if isinstance(draft, dict):
        return draft, "draft"
    migration = source.get("migration")
    if isinstance(migration, dict):
        return migration, "migration"
    return None


def profile_review_status(profile: dict[str, Any], client: ForgejoClient) -> dict[str, Any]:
    source = profile_source(profile)
    review = _review_request(source)
    response: dict[str, Any] = {
        "profileId": profile["id"],
        "sourceState": source.get("state", "unresolved"),
        "review": None,
    }
    if review is None:
        return response
    metadata, kind = review
    number = metadata.get("pullRequestNumber")
    branch = metadata.get("branch")
    if not isinstance(number, int) or number <= 0 or not isinstance(branch, str) or not branch:
        raise HTTPException(status_code=409, detail="Collection profile review metadata is unavailable.")
    try:
        pull_request = client.pull_request(number)
    except ForgejoConfigurationError as exc:
        raise HTTPException(status_code=503, detail="Forgejo service credential is unavailable.") from exc
    except ForgejoRequestError as exc:
        if exc.status_code == 404:
            raise HTTPException(status_code=422, detail="The collection profile review request is unavailable.") from exc
        raise HTTPException(status_code=503, detail="Forgejo review status is unavailable.") from exc
    except ForgejoError as exc:
        raise HTTPException(status_code=503, detail="Forgejo review status is unavailable.") from exc
    response["review"] = {"kind": kind, "branch": branch, "pullRequest": _pull_request_metadata(pull_request)}
    return response


def pin_merged_profile_draft(
    profile: dict[str, Any], payload: dict[str, Any], client: ForgejoClient
) -> dict[str, Any]:
    """Pin a protected-main revision after any outstanding draft was merged."""
    source = require_runnable_source(profile)
    draft = source.get("draft")
    if not isinstance(draft, dict):
        raise HTTPException(status_code=409, detail="Collection profile has no pending draft review to pin.")
    number = draft.get("pullRequestNumber")
    if not isinstance(number, int) or number <= 0:
        raise HTTPException(status_code=409, detail="Collection profile draft review metadata is unavailable.")
    requested_number = payload.get("pullRequestNumber")
    if requested_number is not None and requested_number != number:
        raise HTTPException(status_code=409, detail="The selected review request does not match the current profile draft.")
    branch = draft.get("branch")
    if not isinstance(branch, str) or not branch:
        raise HTTPException(status_code=409, detail="Collection profile draft review metadata is unavailable.")
    repository = _repository(client, source, require_main=True)
    try:
        pull_request = client.pull_request(number)
        if not pull_request.merged:
            raise HTTPException(status_code=409, detail="Collection profile draft must be merged by an authorized reviewer before pinning.")
        if pull_request.base != repository.default_branch or pull_request.head != branch:
            raise HTTPException(status_code=409, detail="Collection profile draft review does not target protected main.")
        requested_sha = payload.get("commitSha")
        commit_sha = (
            require_commit_sha(requested_sha)
            if requested_sha is not None
            else require_commit_sha(client.revision(repository.default_branch).sha)
        )
        if requested_sha is not None:
            history = client.file_history(_source_path(source), repository.default_branch)
            if commit_sha not in {require_commit_sha(entry.sha) for entry in history}:
                raise HTTPException(status_code=422, detail="The selected revision is not in protected main history.")
        file = _read_file(client, _source_path(source), commit_sha)
    except HTTPException:
        raise
    except ForgejoConfigurationError as exc:
        raise HTTPException(status_code=503, detail="Forgejo service credential is unavailable.") from exc
    except ForgejoError as exc:
        raise HTTPException(status_code=503, detail="Forgejo playbook source is unavailable.") from exc
    updated = dict(profile)
    updated_source = dict(source)
    updated_source.pop("draft", None)
    updated_source["commitSha"] = commit_sha
    updated_source["path"] = file.path
    updated_source["state"] = PINNED_SOURCE_STATE
    updated_source["merge"] = {
        "pullRequestNumber": pull_request.number,
        "mergeCommitSha": pull_request.merge_commit_sha or commit_sha,
        "mergedAt": pull_request.merged_at,
    }
    updated["source"] = updated_source
    updated["playbook"] = file.path
    updated["revision"] = commit_sha
    return updated


def pin_profile_revision(
    profile: dict[str, Any], payload: dict[str, Any], client: ForgejoClient
) -> dict[str, Any]:
    """Select an immutable revision already reachable from protected main."""
    source = require_runnable_source(profile)
    if isinstance(source.get("draft"), dict):
        return pin_merged_profile_draft(profile, payload, client)
    requested_sha = require_commit_sha(payload.get("commitSha"))
    repository = _repository(client, source, require_main=True)
    path = _source_path(source)
    try:
        history = client.file_history(path, repository.default_branch)
        if requested_sha not in {require_commit_sha(entry.sha) for entry in history}:
            raise HTTPException(status_code=422, detail="The selected revision is not in protected main history.")
        file = _read_file(client, path, requested_sha)
    except HTTPException:
        raise
    except ForgejoConfigurationError as exc:
        raise HTTPException(status_code=503, detail="Forgejo service credential is unavailable.") from exc
    except ForgejoError as exc:
        raise HTTPException(status_code=503, detail="Forgejo playbook history is unavailable.") from exc
    updated = dict(profile)
    updated_source = dict(source)
    updated_source["commitSha"] = requested_sha
    updated_source["path"] = file.path
    updated_source["state"] = PINNED_SOURCE_STATE
    updated["source"] = updated_source
    updated["playbook"] = file.path
    updated["revision"] = requested_sha
    return updated


def _safe_syntax_result(return_code: int) -> dict[str, Any]:
    if return_code == 0:
        return {"valid": True, "diagnostics": []}
    return {
        "valid": False,
        "diagnostics": [f"Ansible syntax check failed (exit code {return_code})."],
    }


def run_ansible_syntax_check(
    source: bytes,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    """Run target-free Ansible syntax validation without returning command output.

    ``--syntax-check`` parses a temporary file only.  The generated inventory
    contains no hosts, so no SSH, OLVM, or secret-provider connection data can be used.
    """
    with tempfile.TemporaryDirectory(prefix="sentinel-syntax-") as workspace:
        path = Path(workspace) / "playbook.yml"
        inventory = Path(workspace) / "inventory.ini"
        path.write_bytes(source)
        inventory.write_text("[all]\n", encoding="utf-8")
        try:
            completed = runner(
                ["ansible-playbook", "--syntax-check", "--inventory", str(inventory), str(path)],
                cwd=workspace,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except FileNotFoundError:
            return {
                "valid": False,
                "diagnostics": ["ansible-playbook is not installed in the execution image."],
            }
        except subprocess.TimeoutExpired:
            return {
                "valid": False,
                "diagnostics": ["Syntax check exceeded the local time limit."],
            }
        return _safe_syntax_result(completed.returncode)


def syntax_check_pinned_profile(
    profile: dict[str, Any], client: ForgejoClient, runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run
) -> dict[str, Any]:
    source = require_runnable_source(profile)
    file = _read_file(client, _source_path(source), require_commit_sha(source.get("commitSha")))
    result = run_ansible_syntax_check(file.content, runner)
    return {
        **result,
        "mode": SYNTAX_CHECK_MODE,
        "commitSha": require_commit_sha(source.get("commitSha")),
        "message": "Syntax checked the selected pinned Git revision. No infrastructure was contacted.",
    }
