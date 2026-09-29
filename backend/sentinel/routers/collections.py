from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status

from forgejo_client import ForgejoClient
from playbook_migration import PlaybookMigrationError, finalize_database_profiles

from ..collections import queue_profile_run
from ..dependencies import get_forgejo_client
from ..playbooks import (
    create_or_update_profile_draft,
    pin_profile_revision,
    profile_playbook_history,
    profile_review_status,
    read_profile_playbook,
    require_runnable_source,
    syntax_check_pinned_profile,
)
from ..profiles import build_profile, public_profile, resolve_profile_source
from ..config import DATABASE_URL
from ..records import records, store_record
from ..validation import current_record_or_404


router = APIRouter(tags=["collections"])


@router.post("/api/profiles", status_code=status.HTTP_201_CREATED)
def create_profile(
    payload: dict[str, Any], client: ForgejoClient = Depends(get_forgejo_client)
) -> dict[str, Any]:
    return public_profile(store_record("profiles", build_profile(payload, resolve_profile_source(payload, client))))


@router.put("/api/profiles/{profile_id}")
def update_profile(
    profile_id: str,
    payload: dict[str, Any],
    client: ForgejoClient = Depends(get_forgejo_client),
) -> dict[str, Any]:
    return public_profile(
        store_record(
            "profiles",
            build_profile(
                payload,
                resolve_profile_source(payload, client),
                current_record_or_404("profiles", profile_id),
            ),
        )
    )


@router.get("/api/profiles/{profile_id}/playbook")
def get_profile_playbook(
    profile_id: str,
    revision: str | None = Query(default=None),
    client: ForgejoClient = Depends(get_forgejo_client),
) -> dict[str, Any]:
    return read_profile_playbook(current_record_or_404("profiles", profile_id), client, revision)


@router.get("/api/profiles/{profile_id}/playbook/history")
def get_profile_playbook_history(
    profile_id: str, client: ForgejoClient = Depends(get_forgejo_client)
) -> dict[str, Any]:
    return profile_playbook_history(current_record_or_404("profiles", profile_id), client)


@router.patch("/api/profiles/{profile_id}/playbook")
@router.post("/api/profiles/{profile_id}/playbook/draft")
def save_profile_playbook(
    profile_id: str, payload: dict[str, Any], client: ForgejoClient = Depends(get_forgejo_client)
) -> dict[str, Any]:
    profile = current_record_or_404("profiles", profile_id)
    return public_profile(store_record("profiles", create_or_update_profile_draft(profile, payload, client)))


@router.get("/api/profiles/{profile_id}/playbook/review-status")
def get_profile_playbook_review_status(
    profile_id: str, client: ForgejoClient = Depends(get_forgejo_client)
) -> dict[str, Any]:
    return profile_review_status(current_record_or_404("profiles", profile_id), client)


@router.post("/api/profiles/{profile_id}/playbook/pin")
def pin_profile_playbook_revision(
    profile_id: str, payload: dict[str, Any], client: ForgejoClient = Depends(get_forgejo_client)
) -> dict[str, Any]:
    profile = current_record_or_404("profiles", profile_id)
    return public_profile(store_record("profiles", pin_profile_revision(profile, payload, client)))


@router.post("/api/profiles/{profile_id}/syntax-check")
def syntax_check_profile(
    profile_id: str,
    payload: dict[str, Any] | None = None,
    client: ForgejoClient = Depends(get_forgejo_client),
) -> dict[str, Any]:
    if payload and "source" in payload:
        raise HTTPException(
            status_code=422,
            detail="Browser playbook source is not accepted. Syntax checks use the selected pinned Git revision.",
        )
    return syntax_check_pinned_profile(current_record_or_404("profiles", profile_id), client)


@router.post("/api/profiles/{profile_id}/run")
def run_profile(profile_id: str) -> dict[str, Any]:
    profile = current_record_or_404("profiles", profile_id)
    require_runnable_source(profile)
    if profile.get("state") == "paused":
        raise HTTPException(status_code=409, detail="Paused collection profiles cannot be run.")
    result = queue_profile_run(profile)
    return {"profile": public_profile(result.pop("profile")), **result}


@router.post("/api/playbook-migrations/{batch_id}/finalize")
def finalize_playbook_migration(
    batch_id: str, client: ForgejoClient = Depends(get_forgejo_client)
) -> dict[str, Any]:
    try:
        return finalize_database_profiles(DATABASE_URL, batch_id, client)
    except PlaybookMigrationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/api/collections/run")
def run_collection() -> dict[str, Any]:
    active_hosts = [host for host in records("hosts") if host.get("lifecycle", "active") == "active"]
    if not active_hosts:
        raise HTTPException(status_code=409, detail="Add at least one active host before running a collection.")
    candidates = []
    for profile in records("profiles"):
        if profile.get("state") != "enabled":
            continue
        try:
            require_runnable_source(profile)
        except HTTPException:
            continue
        candidates.append(profile)
    if not candidates:
        raise HTTPException(
            status_code=409,
            detail="Create an enabled profile pinned to a reviewed Git revision before running a collection.",
        )
    result = queue_profile_run(candidates[0])
    return {"count": len(active_hosts), **result}
