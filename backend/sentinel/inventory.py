from typing import Any
from uuid import uuid4

import psycopg
from fastapi import HTTPException
from psycopg.types.json import Jsonb

from .config import (
    DEFAULT_OLVM_PRUNE_AFTER_MISSING_RUNS,
    OLVM_LIFECYCLE_STATES,
    OLVM_RECONCILIATION_STATES,
)
from .records import database_connection, get_record, records, store_record
from .validation import (
    credential_exists,
    find_case_insensitive,
    host_addresses,
    normalized_positive_integer,
    require_reference,
    require_text,
)


HOST_ID_MIGRATION_VERSION = "2026-10-02-host-identity-v1"


def _new_host_id() -> str:
    return f"host-{uuid4().hex}"


def _host_identifier(host: dict[str, Any]) -> str:
    return str(host.get("id") or host.get("name") or "").strip()


def olvm_provenance_schema_statements() -> list[str]:
    return [
        """
        CREATE TABLE IF NOT EXISTS sentinel.olvm_host_identities (
            host_id TEXT PRIMARY KEY REFERENCES sentinel.hosts(id) ON DELETE CASCADE,
            source_manager_id TEXT NOT NULL REFERENCES sentinel.managers(id),
            engine_resource_type TEXT NOT NULL,
            engine_resource_id TEXT NOT NULL,
            last_authoritative_run_id TEXT NOT NULL,
            lifecycle_state TEXT NOT NULL CHECK (lifecycle_state IN ('active', 'retired')),
            reconciliation_state TEXT NOT NULL CHECK (
                reconciliation_state IN ('present', 'missing', 'retained', 'pending-prune', 'retired')
            ),
            missing_authoritative_runs INTEGER NOT NULL DEFAULT 0 CHECK (missing_authoritative_runs >= 0),
            prune_after_missing_runs INTEGER NOT NULL DEFAULT 3 CHECK (prune_after_missing_runs > 0),
            UNIQUE (source_manager_id, engine_resource_type, engine_resource_id)
        )
        """,
    ]


def is_olvm_host(host: dict[str, Any]) -> bool:
    return host.get("sourceType", "olvm") == "olvm"


def manager_exists(manager_id: str) -> None:
    if not get_record("managers", manager_id):
        raise HTTPException(status_code=422, detail="OLVM sourceManagerId must refer to a configured manager.")


def olvm_identity_fields(host: dict[str, Any]) -> dict[str, Any]:
    return {
        "sourceManagerId": require_text(host, "sourceManagerId"),
        "engineResourceType": require_text(host, "engineResourceType"),
        "engineResourceId": require_text(host, "engineResourceId"),
        "lastAuthoritativeRunId": require_text(host, "lastAuthoritativeRunId"),
        "lifecycle": str(host.get("lifecycle", "active")),
        "reconciliationState": str(host.get("reconciliationState", "present")),
        "missingAuthoritativeRuns": normalized_positive_integer(
            host.get("missingAuthoritativeRuns", 0), "missingAuthoritativeRuns", minimum=0
        ),
        "pruneAfterMissingRuns": normalized_positive_integer(
            host.get("pruneAfterMissingRuns", DEFAULT_OLVM_PRUNE_AFTER_MISSING_RUNS),
            "pruneAfterMissingRuns",
        ),
    }


def validate_olvm_provenance(host: dict[str, Any], previous_host_id: str | None = None) -> None:
    if not is_olvm_host(host):
        return

    if host.get("provenanceState") == "legacy-unresolved":
        identity_keys = {
            "sourceManagerId",
            "engineResourceType",
            "engineResourceId",
            "lastAuthoritativeRunId",
        }
        if any(host.get(key) for key in identity_keys):
            raise HTTPException(
                status_code=422,
                detail="Legacy-unresolved OLVM hosts cannot contain a partial Engine identity.",
            )
        return

    identity = olvm_identity_fields(host)
    manager_exists(identity["sourceManagerId"])
    if identity["lifecycle"] not in OLVM_LIFECYCLE_STATES:
        raise HTTPException(status_code=422, detail="Invalid OLVM lifecycle state.")
    if identity["reconciliationState"] not in OLVM_RECONCILIATION_STATES:
        raise HTTPException(status_code=422, detail="Invalid OLVM reconciliation state.")

    missing_runs = identity["missingAuthoritativeRuns"]
    threshold = identity["pruneAfterMissingRuns"]
    state = identity["reconciliationState"]
    if state == "present" and missing_runs != 0:
        raise HTTPException(status_code=422, detail="Present OLVM resources cannot have missing authoritative runs.")
    if state == "missing" and not 0 < missing_runs < threshold:
        raise HTTPException(status_code=422, detail="Missing OLVM resources must be below the prune threshold.")
    if state in {"retained", "pending-prune"} and missing_runs < threshold:
        raise HTTPException(status_code=422, detail="OLVM pruning is allowed only after the missing-run threshold.")
    if state == "retired" and identity["lifecycle"] != "retired":
        raise HTTPException(status_code=422, detail="Retired OLVM reconciliation state requires a retired lifecycle.")

    host_id = _host_identifier(host)
    with database_connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT host_id
            FROM sentinel.olvm_host_identities
            WHERE source_manager_id = %s
              AND engine_resource_type = %s
              AND engine_resource_id = %s
            """,
            (
                identity["sourceManagerId"],
                identity["engineResourceType"],
                identity["engineResourceId"],
            ),
        )
        row = cursor.fetchone()
    if row and row[0] not in {host_id, previous_host_id}:
        raise HTTPException(
            status_code=409,
            detail="That OLVM manager and Engine resource identity is already in inventory.",
        )


def store_olvm_identity(cursor: psycopg.Cursor, host_id: str, host: dict[str, Any]) -> None:
    if not is_olvm_host(host) or host.get("provenanceState") == "legacy-unresolved":
        cursor.execute("DELETE FROM sentinel.olvm_host_identities WHERE host_id = %s", (host_id,))
        return
    identity = olvm_identity_fields(host)
    cursor.execute(
        """
        INSERT INTO sentinel.olvm_host_identities (
            host_id, source_manager_id, engine_resource_type, engine_resource_id,
            last_authoritative_run_id, lifecycle_state, reconciliation_state,
            missing_authoritative_runs, prune_after_missing_runs
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (host_id) DO UPDATE SET
            source_manager_id = EXCLUDED.source_manager_id,
            engine_resource_type = EXCLUDED.engine_resource_type,
            engine_resource_id = EXCLUDED.engine_resource_id,
            last_authoritative_run_id = EXCLUDED.last_authoritative_run_id,
            lifecycle_state = EXCLUDED.lifecycle_state,
            reconciliation_state = EXCLUDED.reconciliation_state,
            missing_authoritative_runs = EXCLUDED.missing_authoritative_runs,
            prune_after_missing_runs = EXCLUDED.prune_after_missing_runs
        """,
        (
            host_id,
            identity["sourceManagerId"],
            identity["engineResourceType"],
            identity["engineResourceId"],
            identity["lastAuthoritativeRunId"],
            identity["lifecycle"],
            identity["reconciliationState"],
            identity["missingAuthoritativeRuns"],
            identity["pruneAfterMissingRuns"],
        ),
    )


def store_host(record: dict[str, Any], previous_id: str | None = None) -> dict[str, Any]:
    record = dict(record)
    existing = get_record("hosts", previous_id or _host_identifier(record))
    if existing:
        record["id"] = str(existing["id"])
    else:
        record["id"] = _host_identifier(record) or _new_host_id()
    if existing and existing.get("sourceType", "olvm") == "olvm":
        identity_fields = ("sourceManagerId", "engineResourceType", "engineResourceId")
        has_stable_identity = all(existing.get(field) for field in identity_fields)
        if has_stable_identity and existing.get("provenanceState") != "legacy-unresolved":
            if record.get("sourceType", "olvm") != "olvm" or any(
                record.get(field) != existing[field] for field in identity_fields
            ):
                raise HTTPException(
                    status_code=409,
                    detail="An existing OLVM host's immutable Engine identity cannot be changed.",
                )
    if existing and existing.get("sourceType") == "manual" and is_olvm_host(record):
        raise HTTPException(
            status_code=409,
            detail="An OLVM reconciliation cannot overwrite a manually managed host.",
        )
    validate_olvm_provenance(record, existing["id"] if existing else previous_id)
    host_id = str(record["id"])
    with database_connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO sentinel.hosts (id, payload) VALUES (%s, %s)
            ON CONFLICT (id) DO UPDATE SET payload = EXCLUDED.payload, updated_at = NOW()
            """,
            (host_id, Jsonb(record)),
        )
        store_olvm_identity(cursor, host_id, record)
    return record


def migrate_olvm_provenance() -> None:
    identity_fields = (
        "sourceManagerId",
        "engineResourceType",
        "engineResourceId",
        "lastAuthoritativeRunId",
    )
    for host in records("hosts"):
        if not is_olvm_host(host):
            continue
        has_complete_identity = all(host.get(field) for field in identity_fields)
        if has_complete_identity and get_record("managers", str(host["sourceManagerId"])):
            completed = dict(host)
            completed.setdefault("lifecycle", "active")
            completed.setdefault("reconciliationState", "present")
            completed.setdefault("missingAuthoritativeRuns", 0)
            completed.setdefault("pruneAfterMissingRuns", DEFAULT_OLVM_PRUNE_AFTER_MISSING_RUNS)
            completed.setdefault("pruneEligible", False)
            completed.setdefault("provenanceState", "backfilled")
            store_record("hosts", completed)
            continue

        unresolved = dict(host)
        unresolved.update(
            {
                "sourceType": "olvm",
                "provenanceState": "legacy-unresolved",
                "reconciliationState": "unresolved",
                "provenanceIssue": "Assign a configured manager and immutable Engine resource identity before enabling OLVM synchronization.",
            }
        )
        for field in identity_fields:
            unresolved.pop(field, None)
        store_record("hosts", unresolved)


def ensure_unique_host(name: str, address: str, original_name: str | None = None) -> None:
    for host in records("hosts"):
        if host.get("id") != original_name and host["name"].casefold() == name.casefold():
            raise HTTPException(status_code=409, detail=f"Host name {name} is already in inventory.")
        if host.get("id") != original_name and any(
            item.casefold() == address.casefold() for item in host_addresses(host)
        ):
            raise HTTPException(status_code=409, detail=f"Management address {address} is already in inventory.")


def build_host(payload: dict[str, Any], existing: dict[str, Any] | None = None) -> dict[str, Any]:
    name = require_text(payload, "name")
    address = require_text(payload, "address")
    credential = require_reference(require_text(payload, "credential"))
    credential_exists(credential)
    ensure_unique_host(name, address, existing["id"] if existing else None)
    return {
        "id": existing["id"] if existing else _new_host_id(),
        "name": name,
        "role": require_text(payload, "role"),
        "status": existing.get("status", "healthy") if existing else "healthy",
        "environment": require_text(payload, "environment"),
        "os": existing.get("os", "Linux · SSH") if existing else "Linux · SSH",
        "ip": address,
        "ips": [address],
        "collected": existing.get("collected", "Not collected") if existing else "Not collected",
        "uptime": existing.get("uptime", "Not collected") if existing else "Not collected",
        "disk": existing.get("disk", "--") if existing else "--",
        "memory": existing.get("memory", "--") if existing else "--",
        "tags": existing.get("tags", ["manual"]) if existing else ["manual"],
        "sourceType": "manual",
        "lifecycle": payload.get("lifecycle")
        if payload.get("lifecycle") in {"active", "disabled", "decommissioned"}
        else "active",
        "connectionUser": require_text(payload, "user"),
        "connectionPort": require_text(payload, "port"),
        "credential": credential,
    }


def find_olvm_host_by_identity(
    source_manager_id: str, engine_resource_type: str, engine_resource_id: str
) -> dict[str, Any] | None:
    with database_connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT host_id
            FROM sentinel.olvm_host_identities
            WHERE source_manager_id = %s
              AND engine_resource_type = %s
              AND engine_resource_id = %s
            """,
            (source_manager_id, engine_resource_type, engine_resource_id),
        )
        row = cursor.fetchone()
    return get_record("hosts", row[0]) if row else None


def build_authoritative_olvm_host(
    payload: dict[str, Any], existing: dict[str, Any] | None = None
) -> dict[str, Any]:
    name = require_text(payload, "name")
    address = require_text(payload, "address")
    ensure_unique_host(name, address, existing["id"] if existing else None)

    if existing and existing.get("sourceType", "olvm") != "olvm":
        raise HTTPException(status_code=409, detail="An OLVM reconciliation cannot overwrite a manually managed host.")
    if existing and existing.get("provenanceState") == "legacy-unresolved":
        raise HTTPException(
            status_code=409,
            detail="Resolve the legacy OLVM host provenance before reconciling an Engine resource.",
        )

    candidate = {
        **(existing or {}),
        "id": existing["id"] if existing else _new_host_id(),
        "name": name,
        "role": require_text(payload, "role"),
        "environment": require_text(payload, "environment"),
        "ip": address,
        "ips": [str(item) for item in payload.get("ips", [address]) if str(item)] or [address],
        "sourceType": "olvm",
        "sourceManagerId": require_text(payload, "sourceManagerId"),
        "engineResourceType": require_text(payload, "engineResourceType"),
        "engineResourceId": require_text(payload, "engineResourceId"),
        "lastAuthoritativeRunId": require_text(payload, "lastAuthoritativeRunId"),
        "lifecycle": "active",
        "reconciliationState": "present",
        "missingAuthoritativeRuns": 0,
        "pruneAfterMissingRuns": normalized_positive_integer(
            payload.get(
                "pruneAfterMissingRuns",
                existing.get("pruneAfterMissingRuns", DEFAULT_OLVM_PRUNE_AFTER_MISSING_RUNS)
                if existing
                else DEFAULT_OLVM_PRUNE_AFTER_MISSING_RUNS,
            ),
            "pruneAfterMissingRuns",
        ),
        "pruneEligible": False,
        "provenanceState": "authoritative",
        "status": payload.get("status", existing.get("status", "healthy") if existing else "healthy"),
        "os": payload.get("os", existing.get("os", "Unknown") if existing else "Unknown"),
        "collected": payload.get(
            "collected", existing.get("collected", "Not collected") if existing else "Not collected"
        ),
        "uptime": payload.get("uptime", existing.get("uptime", "Not collected") if existing else "Not collected"),
        "disk": payload.get("disk", existing.get("disk", "--") if existing else "--"),
        "memory": payload.get("memory", existing.get("memory", "--") if existing else "--"),
        "tags": payload.get("tags", existing.get("tags", []) if existing else []),
    }
    if existing:
        for field in ("sourceManagerId", "engineResourceType", "engineResourceId"):
            if candidate[field] != existing[field]:
                raise HTTPException(
                    status_code=409,
                    detail="An existing OLVM host's immutable Engine identity cannot be changed.",
                )
    return candidate


def reconcile_authoritative_olvm_host(payload: dict[str, Any]) -> dict[str, Any]:
    manager_id = require_text(payload, "sourceManagerId")
    resource_type = require_text(payload, "engineResourceType")
    resource_id = require_text(payload, "engineResourceId")
    existing = find_olvm_host_by_identity(manager_id, resource_type, resource_id)

    if existing is None:
        proposed_name = require_text(payload, "name")
        proposed_address = require_text(payload, "address")
        for host in records("hosts"):
            if host.get("sourceType") == "manual" and (
                host["name"].casefold() == proposed_name.casefold()
                or any(address.casefold() == proposed_address.casefold() for address in host_addresses(host))
            ):
                raise HTTPException(
                    status_code=409,
                    detail="OLVM reconciliation conflicts with a manually managed host; review the collision without replacing it.",
                )
        named_host = find_case_insensitive("hosts", "name", proposed_name)
        if named_host:
            raise HTTPException(
                status_code=409,
                detail="An existing host has this name but not this immutable Engine identity.",
            )
        return store_record("hosts", build_authoritative_olvm_host(payload))

    record = build_authoritative_olvm_host(payload, existing)
    return store_record("hosts", record, previous_id=existing["id"])


def find_host_by_name(name: str) -> dict[str, Any] | None:
    """Resolve a display name for name-based HTTP compatibility only."""

    target = str(name).casefold()
    return next((host for host in records("hosts") if str(host.get("name", "")).casefold() == target), None)


def migrate_host_identities(*, force: bool = False) -> None:
    """Give legacy display-name keyed hosts an immutable Sentinel identity.

    Sanitized artifacts remain immutable evidence. Mutable projections and queued
    run metadata are remapped so the portal and future executions use the new
    stable identity.
    """

    with database_connection() as connection, connection.transaction(), connection.cursor() as cursor:
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS sentinel.schema_migrations (version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW())"
        )
        if not force:
            cursor.execute("SELECT 1 FROM sentinel.schema_migrations WHERE version = %s", (HOST_ID_MIGRATION_VERSION,))
            if cursor.fetchone() is not None:
                return
        cursor.execute("SELECT id, payload FROM sentinel.hosts ORDER BY id FOR UPDATE")
        legacy_hosts = list(cursor.fetchall())
        remap: dict[str, str] = {}
        normalized: dict[str, dict[str, Any]] = {}
        for legacy_id, payload in legacy_hosts:
            value = dict(payload)
            existing_id = str(value.get("id") or "")
            host_id = existing_id if existing_id.startswith("host-") else _new_host_id()
            remap[str(legacy_id)] = host_id
            value["id"] = host_id
            normalized[str(legacy_id)] = value

        if remap:
            for legacy_id, host_id in remap.items():
                cursor.execute(
                    "INSERT INTO sentinel.hosts (id, payload) VALUES (%s, %s) ON CONFLICT (id) DO UPDATE SET payload = EXCLUDED.payload, updated_at = NOW()",
                    (host_id, Jsonb(normalized[legacy_id])),
                )

            for table_name, column_name in (
                ("sentinel.olvm_host_identities", "host_id"),
                ("sentinel.collection_host_results", "host_id"),
                ("sentinel.host_capacity_facts", "host_id"),
                ("sentinel.collection_alerts", "host_id"),
                ("sentinel.linux_system_current", "host_id"),
                ("sentinel.linux_filesystem_current", "host_id"),
                ("sentinel.linux_filesystem_snapshots", "host_id"),
            ):
                for legacy_id, host_id in remap.items():
                    cursor.execute(
                        f"UPDATE {table_name} SET {column_name} = %s WHERE {column_name} = %s",
                        (host_id, legacy_id),
                    )

            for table_name, key_column, payload_column in (
                ("sentinel.collection_run_details", "run_id", "metadata"),
                ("sentinel.collection_runs", "id", "payload"),
            ):
                cursor.execute(f"SELECT {key_column}, {payload_column} FROM {table_name} FOR UPDATE")
                for record_id, payload in cursor.fetchall():
                    value = dict(payload)
                    expected = value.get("expectedSourceInstances")
                    if not isinstance(expected, list):
                        continue
                    changed = False
                    rewritten = []
                    for item in expected:
                        if not isinstance(item, dict):
                            rewritten.append(item)
                            continue
                        updated = dict(item)
                        if updated.get("type") == "linux" and str(updated.get("id")) in remap:
                            updated["id"] = remap[str(updated["id"])]
                            changed = True
                        rewritten.append(updated)
                    if changed:
                        value["expectedSourceInstances"] = rewritten
                        statement = f"UPDATE {table_name} SET {payload_column} = %s WHERE {key_column} = %s"
                        if table_name == "sentinel.collection_runs":
                            statement = (
                                f"UPDATE {table_name} SET {payload_column} = %s, updated_at = NOW() "
                                f"WHERE {key_column} = %s"
                            )
                        cursor.execute(statement, (Jsonb(value), record_id))

            for legacy_id, host_id in remap.items():
                if legacy_id != host_id:
                    cursor.execute("DELETE FROM sentinel.hosts WHERE id = %s", (legacy_id,))
        cursor.execute(
            "INSERT INTO sentinel.schema_migrations (version) VALUES (%s) ON CONFLICT (version) DO NOTHING",
            (HOST_ID_MIGRATION_VERSION,),
        )


def record_olvm_resource_missing(
    source_manager_id: str,
    engine_resource_type: str,
    engine_resource_id: str,
    authoritative_run_id: str,
    *,
    request_prune: bool = False,
) -> dict[str, Any]:
    host = find_olvm_host_by_identity(source_manager_id, engine_resource_type, engine_resource_id)
    if not host:
        raise HTTPException(status_code=404, detail="OLVM resource identity is not in inventory.")
    if host.get("sourceType") != "olvm":
        raise HTTPException(status_code=409, detail="Only OLVM-owned hosts participate in OLVM reconciliation.")

    updated = dict(host)
    updated["lastAuthoritativeRunId"] = require_text({"run": authoritative_run_id}, "run")
    updated["missingAuthoritativeRuns"] = int(updated.get("missingAuthoritativeRuns", 0)) + 1
    threshold = normalized_positive_integer(
        updated.get("pruneAfterMissingRuns", DEFAULT_OLVM_PRUNE_AFTER_MISSING_RUNS),
        "pruneAfterMissingRuns",
    )
    updated["pruneAfterMissingRuns"] = threshold
    if updated["missingAuthoritativeRuns"] >= threshold:
        updated["reconciliationState"] = "pending-prune" if request_prune else "retained"
        updated["pruneEligible"] = bool(request_prune)
    else:
        updated["reconciliationState"] = "missing"
        updated["pruneEligible"] = False
    return store_record("hosts", updated)
