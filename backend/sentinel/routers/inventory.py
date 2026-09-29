from typing import Any

from fastapi import APIRouter, HTTPException, status

from ..collections import create_run, simulated_result
from ..inventory import build_host
from ..records import get_record, store_record
from ..validation import current_record_or_404


router = APIRouter(tags=["inventory"])


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
    build_host(payload, existing)
    return simulated_result("SSH connection test")


@router.post("/api/hosts/{host_name}/collect")
def collect_host(host_name: str) -> dict[str, Any]:
    host = current_record_or_404("hosts", host_name)
    create_run(
        f"Simulated collection: {host['name']}",
        "Collection request accepted · no target contacted",
        collection_context={"hosts": [host], "source_type": str(host.get("sourceType", "inventory"))},
    )
    return simulated_result(f"collection request for {host['name']}")
