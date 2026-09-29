from typing import Any

from fastapi import APIRouter, HTTPException, status

from ..collections import create_run, simulated_result
from ..managers import build_manager
from ..records import get_record, records, store_record
from ..validation import current_record_or_404


router = APIRouter(tags=["managers"])


@router.post("/api/managers", status_code=status.HTTP_201_CREATED)
def create_manager(payload: dict[str, Any]) -> dict[str, Any]:
    return store_record("managers", build_manager(payload))


@router.put("/api/managers/{manager_id}")
def update_manager(manager_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    return store_record("managers", build_manager(payload, current_record_or_404("managers", manager_id)))


@router.post("/api/managers/test")
def test_unsaved_manager(payload: dict[str, Any]) -> dict[str, str]:
    manager_id = str(payload.get("id", "")).strip()
    build_manager(payload, get_record("managers", manager_id) if manager_id else None)
    return simulated_result("OLVM manager connection test")


@router.post("/api/managers/{manager_id}/{action}")
def manager_action(manager_id: str, action: str) -> dict[str, Any]:
    if action not in {"test", "sync"}:
        raise HTTPException(status_code=404, detail="Action not found.")
    manager = current_record_or_404("managers", manager_id)
    manager["state"] = "connected"
    if action == "sync":
        manager["lastSync"] = "Simulation queued"
        managed_hosts = [
            host
            for host in records("hosts")
            if host.get("sourceType") == "olvm" and host.get("sourceManagerId") == manager_id
        ]
        create_run(
            f"Simulated OLVM sync: {manager['name']}",
            "Inventory sync request accepted · no Engine contacted",
            collection_context={
                "hosts": managed_hosts,
                "manager_id": manager_id,
                "source_type": "olvm",
            },
        )
    store_record("managers", manager)
    return {"manager": manager, **simulated_result(f"{action} for {manager['name']}")}


@router.post("/api/managers/sync-all")
def sync_all_managers() -> dict[str, Any]:
    managers = records("managers")
    if not managers:
        raise HTTPException(status_code=409, detail="Configure at least one OLVM manager before synchronizing inventory.")
    managed_hosts = [host for host in records("hosts") if host.get("sourceType") == "olvm"]
    for manager in managers:
        manager["state"] = "connected"
        manager["lastSync"] = "Simulation queued"
        store_record("managers", manager)
    create_run(
        "Simulated OLVM sync: all managers",
        f"{len(managers)} sources queued · no Engines contacted",
        collection_context={"hosts": managed_hosts, "source_type": "olvm"},
    )
    return {"count": len(managers), **simulated_result("sync for all OLVM managers")}
