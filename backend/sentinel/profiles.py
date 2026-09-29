from typing import Any

from fastapi import HTTPException

from forgejo_client import ForgejoClient, ForgejoConfigurationError, ForgejoError, ForgejoRequestError
from playbook_migration import profile_source_metadata

from .validation import credential_exists, find_case_insensitive, require_reference, require_text, safe_identifier
from .cron import CronExpressionError
from .scheduling import schedule_metadata


def require_commit_sha(value: str) -> str:
    normalized = value.strip().lower()
    if len(normalized) != 40 or any(character not in "0123456789abcdef" for character in normalized):
        raise HTTPException(status_code=422, detail="An immutable 40-character Git commit SHA is required.")
    return normalized


def profile_request_path(payload: dict[str, Any]) -> str:
    source = payload.get("source")
    if isinstance(source, dict) and isinstance(source.get("path"), str):
        return require_text(source, "path")
    return require_text(payload, "playbook")


def profile_request_revision(payload: dict[str, Any]) -> str:
    source = payload.get("source")
    if isinstance(source, dict) and isinstance(source.get("commitSha"), str):
        return require_commit_sha(source["commitSha"])
    return require_commit_sha(require_text(payload, "revision"))


def resolve_profile_source(payload: dict[str, Any], client: ForgejoClient) -> dict[str, str]:
    path = profile_request_path(payload)
    requested_sha = profile_request_revision(payload)
    requested_source = payload.get("source")
    requested_repository = requested_source.get("repository") if isinstance(requested_source, dict) else None
    try:
        repository = client.repository_metadata()
        if requested_repository and requested_repository != repository.full_name:
            raise HTTPException(
                status_code=422,
                detail="Collection profiles must use Sentinel's configured playbook repository.",
            )
        revision = client.revision(requested_sha)
        resolved_sha = require_commit_sha(revision.sha)
        if resolved_sha != requested_sha:
            raise HTTPException(
                status_code=422,
                detail="The selected Git revision did not resolve to the requested commit.",
            )
        history = client.file_history(path, repository.default_branch)
        if resolved_sha not in {require_commit_sha(entry.sha) for entry in history}:
            raise HTTPException(
                status_code=422,
                detail="Collection profiles can only pin a revision reachable from protected main.",
            )
        resolved_file = client.read_file(path, resolved_sha)
    except HTTPException:
        raise
    except ForgejoConfigurationError as exc:
        raise HTTPException(status_code=503, detail="Forgejo service credential is unavailable.") from exc
    except ForgejoRequestError as exc:
        if exc.status_code == 404:
            raise HTTPException(
                status_code=422,
                detail="The selected Git playbook path or commit is unavailable.",
            ) from exc
        raise HTTPException(status_code=503, detail="Forgejo playbook source is unavailable.") from exc
    except ForgejoError as exc:
        raise HTTPException(status_code=503, detail="Forgejo playbook source is unavailable.") from exc
    return {
        "repository": repository.full_name,
        "path": resolved_file.path,
        "commitSha": resolved_sha,
        "state": "pinned",
    }


def resolve_profile_update_source(
    payload: dict[str, Any], existing: dict[str, Any], client: ForgejoClient
) -> dict[str, Any]:
    """Keep the trusted source when an update does not change its identity.

    A profile can be pinned to a protected-main merge commit that Forgejo's
    file-history endpoint does not list. Settings such as a cron schedule must
    not force that immutable source through fresh path-history validation.
    """
    existing_source = profile_source_metadata(existing)
    requested_source = payload.get("source")
    requested_repository = requested_source.get("repository") if isinstance(requested_source, dict) else None
    if (
        profile_request_path(payload) == existing_source.get("path")
        and profile_request_revision(payload) == existing_source.get("commitSha")
        and (not requested_repository or requested_repository == existing_source.get("repository"))
    ):
        return dict(existing_source)
    return resolve_profile_source(payload, client)


def public_profile(profile: dict[str, Any]) -> dict[str, Any]:
    result = dict(profile)
    result.pop("playbookContent", None)
    if "source" in result:
        result["source"] = dict(profile_source_metadata(result))
    return result


def build_profile(
    payload: dict[str, Any], source: dict[str, str], existing: dict[str, Any] | None = None
) -> dict[str, Any]:
    name = require_text(payload, "name")
    duplicate = find_case_insensitive("profiles", "name", name, existing["id"] if existing else None)
    if duplicate:
        raise HTTPException(status_code=409, detail="A collection profile with this name already exists.")
    credential = require_reference(require_text(payload, "credential"))
    credential_exists(credential)
    playbook = source["path"]
    try:
        schedule, schedule_description = schedule_metadata(payload.get("schedule"))
    except CronExpressionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "id": existing["id"] if existing else safe_identifier(name, "profile"),
        "icon": "network" if "olvm" in playbook.lower() else "package",
        "name": name,
        "description": require_text(payload, "description"),
        "playbook": playbook,
        "revision": source["commitSha"],
        "scope": require_text(payload, "scope"),
        "schedule": schedule,
        "scheduleDescription": schedule_description,
        "scheduleTimezone": "UTC",
        "credential": credential,
        "lastRun": existing.get("lastRun", "Not run") if existing else "Not run",
        "coverage": existing.get("coverage", "No targets") if existing else "No targets",
        "state": payload.get("state") if payload.get("state") in {"enabled", "paused"} else "enabled",
        "source": source,
    }
