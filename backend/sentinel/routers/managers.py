from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, status

from ..collections import create_run
from ..inventory import reconcile_authoritative_olvm_host, record_olvm_resource_missing
from ..managers import build_manager
from ..olvm import OlvmClient, OlvmError
from ..records import get_record, records, store_record
from ..reporting import complete_live_collection, mark_collection_running, record_host_outcomes
from ..validation import current_record_or_404


router = APIRouter(tags=["managers"])


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _olvm_error(exc: OlvmError) -> HTTPException:
    return HTTPException(status_code=503, detail=str(exc))


def _test_manager(manager: dict[str, Any]) -> dict[str, Any]:
    client = OlvmClient(manager)
    try:
        client.test_connection()
    finally:
        client.close()
    return {"mode": "live", "message": f"Connected to OLVM manager {manager['name']}."}


def _sync_manager(manager: dict[str, Any]) -> dict[str, Any]:
    run = create_run(
        f"OLVM inventory sync: {manager['name']}",
        "OLVM inventory synchronization queued",
        collection_context={
            "manager_id": str(manager["id"]),
            "source_type": "olvm",
            "expected_source_instances": [{"type": "olvm", "id": str(manager["id"])}],
        },
    )
    mark_collection_running(str(run["id"]))
    manager["state"] = "syncing"
    store_record("managers", manager)
    outcomes: list[dict[str, Any]] = []
    observed_ids: set[str] = set()
    try:
        client = OlvmClient(manager)
        try:
            virtual_machines = client.list_vms()
        finally:
            client.close()
        for virtual_machine in virtual_machines:
            observed_ids.add(virtual_machine.resource_id)
            if not virtual_machine.address:
                outcomes.append(
                    {
                        "host_id": virtual_machine.name,
                        "host_name": virtual_machine.name,
                        "source_type": "olvm",
                        "manager_id": manager["id"],
                        "state": "skipped",
                        "reason": "OLVM did not report a management IP address for this virtual machine.",
                    }
                )
                continue
            try:
                reconcile_authoritative_olvm_host(
                    {
                        "name": virtual_machine.name,
                        "address": virtual_machine.address,
                        "ips": list(virtual_machine.addresses),
                        "role": "Virtual machine",
                        "environment": "OLVM",
                        "status": "healthy" if virtual_machine.state == "up" else "review",
                        "os": virtual_machine.operating_system,
                        "tags": ["olvm", virtual_machine.state],
                        "sourceManagerId": manager["id"],
                        "engineResourceType": "vm",
                        "engineResourceId": virtual_machine.resource_id,
                        "lastAuthoritativeRunId": run["id"],
                    }
                )
            except HTTPException as exc:
                outcomes.append(
                    {
                        "host_id": virtual_machine.resource_id,
                        "host_name": virtual_machine.name,
                        "source_type": "olvm",
                        "manager_id": manager["id"],
                        "state": "failed",
                        "reason": str(exc.detail),
                    }
                )
            else:
                outcomes.append(
                    {
                        "host_id": virtual_machine.name,
                        "host_name": virtual_machine.name,
                        "source_type": "olvm",
                        "manager_id": manager["id"],
                        "state": "success",
                    }
                )

        # Reconciliation is authoritative only after a complete successful
        # inventory response. It records a missing streak but never deletes.
        for host in records("hosts"):
            if (
                host.get("sourceType") == "olvm"
                and host.get("sourceManagerId") == manager["id"]
                and host.get("engineResourceType") == "vm"
                and host.get("engineResourceId") not in observed_ids
            ):
                record_olvm_resource_missing(
                    str(manager["id"]), "vm", str(host["engineResourceId"]), str(run["id"])
                )
        if not outcomes:
            state = "completed"
            message = f"OLVM manager {manager['name']} reported no virtual machines."
        elif any(item["state"] == "failed" for item in outcomes):
            state = "partial" if any(item["state"] == "success" for item in outcomes) else "failed"
            message = f"OLVM manager {manager['name']} synchronized with review items."
        elif any(item["state"] == "skipped" for item in outcomes):
            state = "partial"
            message = f"OLVM manager {manager['name']} synchronized; some virtual machines need inventory review."
        else:
            state = "completed"
            message = f"OLVM manager {manager['name']} inventory synchronized."
        record_host_outcomes(str(run["id"]), outcomes, manager_id=str(manager["id"]))
        complete_live_collection(str(run["id"]), state)
        manager["state"] = "connected"
        manager["lastSync"] = _now()
        manager["inventory"] = f"{sum(1 for item in outcomes if item['state'] == 'success')} virtual machines synchronized"
        store_record("managers", manager)
        return {"manager": manager, "run": run, "mode": "live", "state": state, "message": message}
    except OlvmError as exc:
        complete_live_collection(str(run["id"]), "failed", failure_reason=str(exc))
        manager["state"] = "ready"
        manager["lastSync"] = "Failed"
        manager["inventory"] = "No authoritative inventory result"
        store_record("managers", manager)
        raise _olvm_error(exc) from exc


@router.post("/api/managers", status_code=status.HTTP_201_CREATED)
def create_manager(payload: dict[str, Any]) -> dict[str, Any]:
    return store_record("managers", build_manager(payload))


@router.put("/api/managers/{manager_id}")
def update_manager(manager_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    return store_record("managers", build_manager(payload, current_record_or_404("managers", manager_id)))


@router.post("/api/managers/test")
def test_unsaved_manager(payload: dict[str, Any]) -> dict[str, str]:
    manager_id = str(payload.get("id", "")).strip()
    manager = build_manager(payload, get_record("managers", manager_id) if manager_id else None)
    try:
        return _test_manager(manager)
    except OlvmError as exc:
        raise _olvm_error(exc) from exc


@router.post("/api/managers/{manager_id}/{action}")
def manager_action(manager_id: str, action: str) -> dict[str, Any]:
    if action not in {"test", "sync"}:
        raise HTTPException(status_code=404, detail="Action not found.")
    manager = current_record_or_404("managers", manager_id)
    try:
        if action == "test":
            result = _test_manager(manager)
            manager["state"] = "connected"
            store_record("managers", manager)
            return {"manager": manager, **result}
        return _sync_manager(manager)
    except OlvmError as exc:
        raise _olvm_error(exc) from exc


@router.post("/api/managers/sync-all")
def sync_all_managers() -> dict[str, Any]:
    configured = records("managers")
    if not configured:
        raise HTTPException(status_code=409, detail="Configure at least one OLVM manager before synchronizing inventory.")
    results = []
    for manager in configured:
        try:
            results.append(_sync_manager(manager))
        except HTTPException as exc:
            results.append({"manager": manager, "mode": "live", "state": "failed", "message": str(exc.detail)})
    completed = sum(1 for result in results if result["state"] in {"completed", "partial"})
    return {
        "count": len(configured),
        "completed": completed,
        "results": results,
        "mode": "live",
        "message": f"Completed OLVM sync for {completed} of {len(configured)} configured managers.",
    }
