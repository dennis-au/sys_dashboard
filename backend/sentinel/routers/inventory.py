from typing import Any

from fastapi import APIRouter, HTTPException, status

from ..collections import queue_profile_run
from ..execution import CollectionExecutionError, test_linux_host_connection
from ..inventory import build_host
from ..playbooks import require_runnable_source
from ..records import get_record, records, store_record
from ..validation import current_record_or_404


router = APIRouter(tags=["inventory"])


def _runnable_profile() -> dict[str, Any]:
    for profile in records("profiles"):
        if profile.get("state") != "enabled":
            continue
        try:
            require_runnable_source(profile)
        except HTTPException:
            continue
        return profile
    raise HTTPException(
        status_code=409,
        detail="Create an enabled profile pinned to a reviewed Git revision before requesting collection.",
    )


@router.post("/api/hosts", status_code=status.HTTP_201_CREATED)
def create_host(payload: dict[str, Any]) -> dict[str, Any]:
    return store_record("hosts", build_host(payload))


@router.put("/api/hosts/{host_name}")
def update_host(host_name: str, payload: dict[str, Any]) -> dict[str, Any]:
    existing = current_record_or_404("hosts", host_name)
    if existing.get("sourceType", "olvm") != "manual":
        raise HTTPException(status_code=403, detail="Only manually managed hosts can be edited.")
    return store_record("hosts", build_host(payload, existing), previous_id=host_name)


@router.post("/api/hosts/test")
def test_unsaved_host(payload: dict[str, Any]) -> dict[str, str]:
    original_name = str(payload.get("originalName", "")).strip()
    existing = get_record("hosts", original_name) if original_name else None
    if existing and existing.get("sourceType", "olvm") != "manual":
        raise HTTPException(status_code=403, detail="Only manually managed hosts can be edited.")
    host = build_host(payload, existing)
    try:
        return test_linux_host_connection(host)
    except CollectionExecutionError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/api/hosts/{host_name}/collect")
def collect_host(host_name: str) -> dict[str, Any]:
    host = current_record_or_404("hosts", host_name)
    if host.get("lifecycle", "active") != "active":
        raise HTTPException(status_code=409, detail="Only active hosts can be collected.")
    profile = _runnable_profile()
    result = queue_profile_run(profile, hosts=[host])
    return {"host": host, **result}
