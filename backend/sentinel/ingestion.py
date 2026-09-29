"""Idempotent receipt and projection of sanitized collection artifacts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import PurePosixPath
from typing import Any, Iterable
from uuid import uuid4

from psycopg.types.json import Jsonb

from .facts import CanonicalFact, FactContractError, SCHEMA_VERSION, validate_fact_record
from .records import database_connection


ARTIFACT_SCHEMA_VERSION = 1
RUN_STATES = frozenset(
    {"queued", "dispatched", "running", "artifact-uploaded", "ingesting", "completed", "partial", "unreachable", "failed"}
)
TERMINAL_RUN_STATES = frozenset({"completed", "partial", "unreachable", "failed"})


class ArtifactContractError(ValueError):
    """An artifact cannot be accepted by the collection ingestion boundary."""


@dataclass(frozen=True)
class ArtifactManifest:
    run_id: str
    profile_id: str | None
    source_path: str | None
    source_commit_sha: str | None
    location: str
    sha256: str
    record_count: int
    collector_version: str
    collection_started_at: str
    collection_completed_at: str
    execution_status: str
    source_instances: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class IngestionResult:
    receipt_id: str
    state: str
    accepted_records: int
    quarantined_records: int
    replayed: bool


def ingestion_schema_statements() -> list[str]:
    """Return the explicit schema migration for artifacts and projections."""

    return [
        """
        CREATE TABLE IF NOT EXISTS sentinel.schema_migrations (
            version TEXT PRIMARY KEY,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS sentinel.collection_artifact_receipts (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES sentinel.collection_run_details(run_id),
            artifact_location TEXT NOT NULL,
            artifact_sha256 TEXT NOT NULL,
            manifest JSONB NOT NULL,
            state TEXT NOT NULL CHECK (state IN ('accepted', 'rejected')),
            record_count INTEGER NOT NULL CHECK (record_count >= 0),
            accepted_records INTEGER NOT NULL CHECK (accepted_records >= 0),
            quarantined_records INTEGER NOT NULL CHECK (quarantined_records >= 0),
            received_at TIMESTAMPTZ NOT NULL,
            completed_at TIMESTAMPTZ NOT NULL,
            diagnostic_code TEXT,
            UNIQUE (run_id, artifact_sha256)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS sentinel.collection_artifact_ledger (
            id TEXT PRIMARY KEY,
            receipt_id TEXT NOT NULL REFERENCES sentinel.collection_artifact_receipts(id),
            run_id TEXT NOT NULL,
            record_key TEXT NOT NULL,
            sequence INTEGER,
            record_type TEXT,
            record_sha256 TEXT,
            acceptance_state TEXT NOT NULL CHECK (acceptance_state IN ('accepted', 'quarantined')),
            diagnostic_code TEXT,
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL,
            UNIQUE (run_id, record_key)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS sentinel.collection_ingestion_errors (
            id TEXT PRIMARY KEY,
            receipt_id TEXT NOT NULL REFERENCES sentinel.collection_artifact_receipts(id),
            run_id TEXT NOT NULL,
            line_number INTEGER,
            record_key TEXT,
            diagnostic_code TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS sentinel.collection_source_instances (
            id TEXT PRIMARY KEY,
            source_type TEXT NOT NULL CHECK (source_type IN ('kubernetes')),
            display_name TEXT NOT NULL,
            credential_reference TEXT,
            state TEXT NOT NULL CHECK (state IN ('enabled', 'disabled')),
            payload JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS sentinel.linux_filesystem_current (
            host_id TEXT NOT NULL,
            filesystem_id TEXT NOT NULL,
            mount_path TEXT NOT NULL,
            filesystem_type TEXT NOT NULL,
            total_bytes BIGINT NOT NULL CHECK (total_bytes >= 0),
            used_bytes BIGINT NOT NULL CHECK (used_bytes >= 0),
            available_bytes BIGINT NOT NULL CHECK (available_bytes >= 0),
            observed_at TIMESTAMPTZ NOT NULL,
            run_id TEXT NOT NULL,
            PRIMARY KEY (host_id, filesystem_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS sentinel.linux_filesystem_snapshots (
            record_key TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            host_id TEXT NOT NULL,
            filesystem_id TEXT NOT NULL,
            mount_path TEXT NOT NULL,
            total_bytes BIGINT NOT NULL CHECK (total_bytes >= 0),
            used_bytes BIGINT NOT NULL CHECK (used_bytes >= 0),
            available_bytes BIGINT NOT NULL CHECK (available_bytes >= 0),
            observed_at TIMESTAMPTZ NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS sentinel.ovirt_vm_current (
            manager_id TEXT NOT NULL,
            vm_id TEXT NOT NULL,
            display_name TEXT,
            state TEXT NOT NULL,
            payload JSONB NOT NULL,
            observed_at TIMESTAMPTZ NOT NULL,
            run_id TEXT NOT NULL,
            PRIMARY KEY (manager_id, vm_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS sentinel.ovirt_storage_capacity_snapshots (
            record_key TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            manager_id TEXT NOT NULL,
            storage_id TEXT NOT NULL,
            total_bytes BIGINT NOT NULL CHECK (total_bytes >= 0),
            used_bytes BIGINT NOT NULL CHECK (used_bytes >= 0),
            observed_at TIMESTAMPTZ NOT NULL,
            payload JSONB NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS sentinel.k8s_cluster_current (
            cluster_id TEXT PRIMARY KEY,
            display_name TEXT,
            state TEXT NOT NULL,
            payload JSONB NOT NULL,
            observed_at TIMESTAMPTZ NOT NULL,
            run_id TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS sentinel.k8s_node_current (
            cluster_id TEXT NOT NULL,
            node_uid TEXT NOT NULL,
            display_name TEXT,
            state TEXT NOT NULL,
            payload JSONB NOT NULL,
            observed_at TIMESTAMPTZ NOT NULL,
            run_id TEXT NOT NULL,
            PRIMARY KEY (cluster_id, node_uid)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS sentinel.k8s_workload_current (
            cluster_id TEXT NOT NULL,
            workload_uid TEXT NOT NULL,
            display_name TEXT,
            state TEXT NOT NULL,
            payload JSONB NOT NULL,
            observed_at TIMESTAMPTZ NOT NULL,
            run_id TEXT NOT NULL,
            PRIMARY KEY (cluster_id, workload_uid)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS sentinel.k8s_pod_current (
            cluster_id TEXT NOT NULL,
            pod_uid TEXT NOT NULL,
            display_name TEXT,
            state TEXT NOT NULL,
            payload JSONB NOT NULL,
            observed_at TIMESTAMPTZ NOT NULL,
            run_id TEXT NOT NULL,
            PRIMARY KEY (cluster_id, pod_uid)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS sentinel.k8s_pvc_current (
            cluster_id TEXT NOT NULL,
            pvc_uid TEXT NOT NULL,
            display_name TEXT,
            state TEXT NOT NULL,
            payload JSONB NOT NULL,
            observed_at TIMESTAMPTZ NOT NULL,
            run_id TEXT NOT NULL,
            PRIMARY KEY (cluster_id, pvc_uid)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS sentinel.k8s_node_capacity_snapshots (
            record_key TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            cluster_id TEXT NOT NULL,
            node_uid TEXT NOT NULL,
            allocatable_cpu_millicores BIGINT NOT NULL CHECK (allocatable_cpu_millicores >= 0),
            allocatable_memory_bytes BIGINT NOT NULL CHECK (allocatable_memory_bytes >= 0),
            observed_at TIMESTAMPTZ NOT NULL,
            payload JSONB NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS sentinel.k8s_namespace_capacity_snapshots (
            record_key TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            cluster_id TEXT NOT NULL,
            namespace_uid TEXT NOT NULL,
            requested_cpu_millicores BIGINT NOT NULL CHECK (requested_cpu_millicores >= 0),
            requested_memory_bytes BIGINT NOT NULL CHECK (requested_memory_bytes >= 0),
            observed_at TIMESTAMPTZ NOT NULL,
            payload JSONB NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS sentinel.k8s_pod_metric_snapshots (
            record_key TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            cluster_id TEXT NOT NULL,
            pod_uid TEXT NOT NULL,
            cpu_millicores BIGINT NOT NULL CHECK (cpu_millicores >= 0),
            memory_bytes BIGINT NOT NULL CHECK (memory_bytes >= 0),
            observed_at TIMESTAMPTZ NOT NULL,
            payload JSONB NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS collection_artifact_ledger_receipt_idx ON sentinel.collection_artifact_ledger (receipt_id)",
        "CREATE INDEX IF NOT EXISTS linux_filesystem_snapshots_observed_idx ON sentinel.linux_filesystem_snapshots (observed_at DESC)",
        "CREATE INDEX IF NOT EXISTS k8s_node_capacity_snapshots_observed_idx ON sentinel.k8s_node_capacity_snapshots (observed_at DESC)",
        "ALTER TABLE sentinel.collection_run_details DROP CONSTRAINT IF EXISTS collection_run_details_state_check",
        "ALTER TABLE sentinel.collection_run_details ADD CONSTRAINT collection_run_details_state_check CHECK (state IN ('queued', 'dispatched', 'running', 'artifact-uploaded', 'ingesting', 'completed', 'partial', 'unreachable', 'failed'))",
        "DROP VIEW IF EXISTS reporting.linux_filesystem_current",
        "DROP VIEW IF EXISTS reporting.linux_filesystem_snapshots",
        """
        CREATE VIEW reporting.linux_filesystem_current AS
        SELECT host_id, filesystem_id, mount_path, filesystem_type, total_bytes,
               used_bytes, available_bytes, observed_at, run_id
        FROM sentinel.linux_filesystem_current
        """,
        """
        CREATE VIEW reporting.linux_filesystem_snapshots AS
        SELECT record_key, run_id, host_id, filesystem_id, mount_path, total_bytes,
               used_bytes, available_bytes, observed_at
        FROM sentinel.linux_filesystem_snapshots
        """,
    ]


def apply_ingestion_migration(cursor) -> None:
    """Apply new artifact tables once, retaining a durable migration marker."""

    cursor.execute(
        "CREATE TABLE IF NOT EXISTS sentinel.schema_migrations (version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW())"
    )
    version = "2026-09-28-artifact-ingestion-v1"
    cursor.execute("SELECT 1 FROM sentinel.schema_migrations WHERE version = %s", (version,))
    if cursor.fetchone() is not None:
        return
    for statement in ingestion_schema_statements()[1:]:
        cursor.execute(statement)
    cursor.execute("INSERT INTO sentinel.schema_migrations (version) VALUES (%s)", (version,))


def apply_linux_system_fact_migration(cursor) -> None:
    """Add typed Linux OS identity projection without rewriting inventory rows."""

    version = "2026-09-29-linux-system-facts-v1"
    cursor.execute("SELECT 1 FROM sentinel.schema_migrations WHERE version = %s", (version,))
    if cursor.fetchone() is not None:
        return
    statements = [
        """
        CREATE TABLE IF NOT EXISTS sentinel.linux_system_current (
            host_id TEXT PRIMARY KEY,
            hostname TEXT NOT NULL,
            os_family TEXT NOT NULL,
            distribution TEXT NOT NULL,
            distribution_version TEXT NOT NULL,
            kernel TEXT NOT NULL,
            architecture TEXT NOT NULL,
            observed_at TIMESTAMPTZ NOT NULL,
            run_id TEXT NOT NULL
        )
        """,
        "DROP VIEW IF EXISTS reporting.linux_system_current",
        """
        CREATE VIEW reporting.linux_system_current AS
        SELECT host_id, hostname, os_family, distribution, distribution_version,
               kernel, architecture, observed_at, run_id
        FROM sentinel.linux_system_current
        """,
    ]
    for statement in statements:
        cursor.execute(statement)
    cursor.execute("INSERT INTO sentinel.schema_migrations (version) VALUES (%s)", (version,))


def _utc_timestamp(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise ArtifactContractError(f"{label} is required.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ArtifactContractError(f"{label} must be an ISO 8601 timestamp.") from exc
    if parsed.tzinfo is None:
        raise ArtifactContractError(f"{label} must include a timezone.")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _location(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ArtifactContractError("artifact.location is required.")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        raise ArtifactContractError("artifact.location must be a protected relative path.")
    return str(path)


def validate_manifest(payload: Any, artifact: bytes) -> ArtifactManifest:
    """Validate immutable artifact metadata independently from its record lines."""

    if not isinstance(payload, dict):
        raise ArtifactContractError("Manifest must be an object.")
    if payload.get("schemaVersion") != ARTIFACT_SCHEMA_VERSION:
        raise ArtifactContractError("Unsupported manifest schema version.")
    run_id = payload.get("runId")
    if not isinstance(run_id, str) or not run_id:
        raise ArtifactContractError("Manifest runId is required.")
    collector_version = payload.get("collectorVersion")
    if not isinstance(collector_version, str) or not collector_version.strip():
        raise ArtifactContractError("Manifest collectorVersion is required.")
    artifact_metadata = payload.get("artifact")
    if not isinstance(artifact_metadata, dict):
        raise ArtifactContractError("Manifest artifact metadata is required.")
    artifact_sha256 = artifact_metadata.get("sha256")
    if not isinstance(artifact_sha256, str) or not re_full_sha256(artifact_sha256):
        raise ArtifactContractError("artifact.sha256 must be a SHA-256 hex digest.")
    if artifact_sha256 != _sha256(artifact):
        raise ArtifactContractError("Artifact checksum does not match the manifest.")
    record_count = artifact_metadata.get("recordCount")
    if isinstance(record_count, bool) or not isinstance(record_count, int) or record_count < 0:
        raise ArtifactContractError("artifact.recordCount must be a non-negative integer.")
    source_instances = payload.get("sourceInstances")
    if not isinstance(source_instances, list) or not source_instances:
        raise ArtifactContractError("Manifest sourceInstances must be a non-empty list.")
    parsed_sources: list[tuple[str, str]] = []
    for source in source_instances:
        if not isinstance(source, dict) or not isinstance(source.get("type"), str) or not isinstance(source.get("id"), str):
            raise ArtifactContractError("Each source instance needs type and id.")
        source_type = source["type"].strip()
        source_id = source["id"].strip()
        if source_type not in {"linux", "olvm", "kubernetes"} or not source_id:
            raise ArtifactContractError("Manifest source instance is unsupported.")
        parsed_sources.append((source_type, source_id))
    if len(set(parsed_sources)) != len(parsed_sources):
        raise ArtifactContractError("Manifest sourceInstances must be unique.")
    started_at = _utc_timestamp(payload.get("collectionStartedAt"), "collectionStartedAt")
    completed_at = _utc_timestamp(payload.get("collectionCompletedAt"), "collectionCompletedAt")
    if datetime.fromisoformat(completed_at.replace("Z", "+00:00")) < datetime.fromisoformat(started_at.replace("Z", "+00:00")):
        raise ArtifactContractError("collectionCompletedAt cannot precede collectionStartedAt.")
    execution_status = payload.get("executionStatus")
    if execution_status not in {"completed", "partial", "failed"}:
        raise ArtifactContractError("Manifest executionStatus must be completed, partial, or failed.")
    profile_id = payload.get("profileId")
    if not isinstance(profile_id, str) or not profile_id.strip():
        raise ArtifactContractError("Manifest profileId is required.")
    source = payload.get("source")
    if not isinstance(source, dict):
        raise ArtifactContractError("Manifest source is required.")
    source_path = source.get("path")
    source_commit_sha = source.get("commitSha")
    if not isinstance(source_path, str) or not source_path.strip():
        raise ArtifactContractError("Manifest source.path is required.")
    if not isinstance(source_commit_sha, str) or not _git_sha(source_commit_sha):
        raise ArtifactContractError("Manifest source.commitSha must be a 40-character Git SHA.")
    return ArtifactManifest(
        run_id=run_id,
        profile_id=profile_id.strip(),
        source_path=source_path.strip(),
        source_commit_sha=source_commit_sha.lower(),
        location=_location(artifact_metadata.get("location")),
        sha256=artifact_sha256,
        record_count=record_count,
        collector_version=collector_version.strip(),
        collection_started_at=started_at,
        collection_completed_at=completed_at,
        execution_status=execution_status,
        source_instances=tuple(parsed_sources),
    )


def re_full_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value.lower())


def _git_sha(value: str) -> bool:
    return len(value) == 40 and all(character in "0123456789abcdef" for character in value.lower())


def _decode_lines(artifact: bytes) -> list[tuple[int, Any]]:
    try:
        text = artifact.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ArtifactContractError("Artifact must be UTF-8 NDJSON.") from exc
    lines: list[tuple[int, Any]] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            lines.append((line_number, json.loads(line)))
        except json.JSONDecodeError as exc:
            raise ArtifactContractError("Artifact contains invalid NDJSON.") from exc
    return lines


def _source_exists(cursor, source_type: str, source_id: str) -> bool:
    if source_type == "linux":
        cursor.execute("SELECT 1 FROM sentinel.hosts WHERE id = %s", (source_id,))
    elif source_type == "olvm":
        cursor.execute("SELECT 1 FROM sentinel.managers WHERE id = %s", (source_id,))
    else:
        cursor.execute(
            "SELECT 1 FROM sentinel.collection_source_instances WHERE id = %s AND source_type = 'kubernetes' AND state = 'enabled'",
            (source_id,),
        )
    return cursor.fetchone() is not None


def _run_binding(cursor, manifest: ArtifactManifest) -> dict[str, Any]:
    cursor.execute(
        """
        SELECT run_id, profile_id, source_path, source_commit_sha, source_state, state, metadata
        FROM sentinel.collection_run_details
        WHERE run_id = %s
        FOR UPDATE
        """,
        (manifest.run_id,),
    )
    row = cursor.fetchone()
    if row is None:
        raise ArtifactContractError("Manifest runId is not a Sentinel collection run.")
    state = row[5]
    metadata = row[6] if isinstance(row[6], dict) else {}
    if row[1] is not None and manifest.profile_id != row[1]:
        raise ArtifactContractError("Manifest profileId does not match the reserved collection run.")
    if row[2] is not None and manifest.source_path != row[2]:
        raise ArtifactContractError("Manifest source path does not match the reserved collection run.")
    if row[3] is not None and manifest.source_commit_sha != row[3]:
        raise ArtifactContractError("Manifest source commit does not match the reserved collection run.")
    expected_sources = metadata.get("expectedSourceInstances")
    if expected_sources:
        expected = {
            (str(item.get("type")), str(item.get("id")))
            for item in expected_sources
            if isinstance(item, dict) and item.get("type") and item.get("id")
        }
        if expected != set(manifest.source_instances):
            raise ArtifactContractError("Artifact source instances do not match the reserved collection run.")
    return {
        "runId": row[0],
        "profileId": row[1],
        "sourcePath": row[2],
        "sourceCommitSha": row[3],
        "sourceState": row[4],
        "state": state,
    }


def _transition(cursor, run_id: str, state: str, *, failure_reason: str | None = None) -> None:
    if state not in RUN_STATES:
        raise ArtifactContractError("Unsupported collection run state.")
    terminal = state in TERMINAL_RUN_STATES
    cursor.execute(
        """
        UPDATE sentinel.collection_run_details
        SET state = %s,
            started_at = CASE WHEN %s = 'running' AND started_at IS NULL THEN NOW() ELSE started_at END,
            completed_at = CASE WHEN %s THEN NOW() ELSE completed_at END,
            failure_reason = %s
        WHERE run_id = %s
        """,
        (state, state, terminal, failure_reason, run_id),
    )


def _project_fact(cursor, fact: CanonicalFact) -> None:
    payload = fact.payload
    if fact.record_type == "linux.system_fact.v1":
        cursor.execute(
            """
            INSERT INTO sentinel.linux_system_current (
                host_id, hostname, os_family, distribution, distribution_version,
                kernel, architecture, observed_at, run_id
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (host_id) DO UPDATE SET
              hostname = EXCLUDED.hostname,
              os_family = EXCLUDED.os_family,
              distribution = EXCLUDED.distribution,
              distribution_version = EXCLUDED.distribution_version,
              kernel = EXCLUDED.kernel,
              architecture = EXCLUDED.architecture,
              observed_at = EXCLUDED.observed_at,
              run_id = EXCLUDED.run_id
            WHERE sentinel.linux_system_current.observed_at <= EXCLUDED.observed_at
            """,
            (
                fact.source_id,
                str(payload["hostname"]),
                str(payload["osFamily"]),
                str(payload["distribution"]),
                str(payload["distributionVersion"]),
                str(payload["kernel"]),
                str(payload["architecture"]),
                fact.observed_at,
                fact.run_id,
            ),
        )
    elif fact.record_type == "linux.filesystem_snapshot.v1":
        values = (
            fact.source_id,
            fact.resource_id,
            str(payload["mountPath"]),
            str(payload["filesystemType"]),
            payload["totalBytes"],
            payload["usedBytes"],
            payload["availableBytes"],
            fact.observed_at,
            fact.run_id,
        )
        cursor.execute(
            """
            INSERT INTO sentinel.linux_filesystem_current (
                host_id, filesystem_id, mount_path, filesystem_type, total_bytes,
                used_bytes, available_bytes, observed_at, run_id
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (host_id, filesystem_id) DO UPDATE SET
              mount_path = EXCLUDED.mount_path,
              filesystem_type = EXCLUDED.filesystem_type,
              total_bytes = EXCLUDED.total_bytes,
              used_bytes = EXCLUDED.used_bytes,
              available_bytes = EXCLUDED.available_bytes,
              observed_at = EXCLUDED.observed_at,
              run_id = EXCLUDED.run_id
            WHERE sentinel.linux_filesystem_current.observed_at <= EXCLUDED.observed_at
            """,
            values,
        )
        cursor.execute(
            """
            INSERT INTO sentinel.linux_filesystem_snapshots (
                record_key, run_id, host_id, filesystem_id, mount_path, total_bytes,
                used_bytes, available_bytes, observed_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (record_key) DO NOTHING
            """,
            (fact.record_key, fact.run_id, fact.source_id, fact.resource_id, str(payload["mountPath"]), payload["totalBytes"], payload["usedBytes"], payload["availableBytes"], fact.observed_at),
        )
    elif fact.record_type == "linux.capacity_snapshot.v1":
        cursor.execute(
            """
            INSERT INTO sentinel.host_capacity_facts (
                id, run_id, host_id, host_name, observed_at, cpu_cores,
                memory_total_bytes, memory_used_bytes, disk_total_bytes,
                disk_used_bytes, execution_mode
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'live')
            ON CONFLICT (id) DO NOTHING
            """,
            (
                f"capacity-{fact.record_key}",
                fact.run_id,
                fact.source_id,
                fact.display_name or fact.source_id,
                fact.observed_at,
                payload["cpuCores"],
                payload["memoryTotalBytes"],
                payload["memoryUsedBytes"],
                payload["diskTotalBytes"],
                payload["diskUsedBytes"],
            ),
        )
    elif fact.record_type == "ovirt.vm_fact.v1":
        _upsert_current(cursor, "sentinel.ovirt_vm_current", ("manager_id", "vm_id"), fact)
    elif fact.record_type == "ovirt.storage_capacity_snapshot.v1":
        cursor.execute(
            """
            INSERT INTO sentinel.ovirt_storage_capacity_snapshots (
                record_key, run_id, manager_id, storage_id, total_bytes, used_bytes, observed_at, payload
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (record_key) DO NOTHING
            """,
            (fact.record_key, fact.run_id, fact.source_id, fact.resource_id, payload["totalBytes"], payload["usedBytes"], fact.observed_at, Jsonb(payload)),
        )
    elif fact.record_type == "k8s.cluster_fact.v1":
        _upsert_current(cursor, "sentinel.k8s_cluster_current", ("cluster_id",), fact)
    elif fact.record_type == "k8s.node_fact.v1":
        _upsert_current(cursor, "sentinel.k8s_node_current", ("cluster_id", "node_uid"), fact)
    elif fact.record_type == "k8s.workload_fact.v1":
        _upsert_current(cursor, "sentinel.k8s_workload_current", ("cluster_id", "workload_uid"), fact)
    elif fact.record_type == "k8s.pod_fact.v1":
        _upsert_current(cursor, "sentinel.k8s_pod_current", ("cluster_id", "pod_uid"), fact)
    elif fact.record_type == "k8s.pvc_snapshot.v1":
        _upsert_current(cursor, "sentinel.k8s_pvc_current", ("cluster_id", "pvc_uid"), fact)
    elif fact.record_type == "k8s.node_capacity_snapshot.v1":
        cursor.execute(
            """
            INSERT INTO sentinel.k8s_node_capacity_snapshots (
                record_key, run_id, cluster_id, node_uid, allocatable_cpu_millicores,
                allocatable_memory_bytes, observed_at, payload
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (record_key) DO NOTHING
            """,
            (fact.record_key, fact.run_id, fact.source_id, fact.resource_id, payload["allocatableCpuMillicores"], payload["allocatableMemoryBytes"], fact.observed_at, Jsonb(payload)),
        )
    elif fact.record_type == "k8s.namespace_capacity_snapshot.v1":
        cursor.execute(
            """
            INSERT INTO sentinel.k8s_namespace_capacity_snapshots (
                record_key, run_id, cluster_id, namespace_uid, requested_cpu_millicores,
                requested_memory_bytes, observed_at, payload
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (record_key) DO NOTHING
            """,
            (fact.record_key, fact.run_id, fact.source_id, fact.resource_id, payload["requestedCpuMillicores"], payload["requestedMemoryBytes"], fact.observed_at, Jsonb(payload)),
        )
    elif fact.record_type == "k8s.pod_metric_snapshot.v1":
        cursor.execute(
            """
            INSERT INTO sentinel.k8s_pod_metric_snapshots (
                record_key, run_id, cluster_id, pod_uid, cpu_millicores, memory_bytes, observed_at, payload
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (record_key) DO NOTHING
            """,
            (fact.record_key, fact.run_id, fact.source_id, fact.resource_id, payload["cpuMillicores"], payload["memoryBytes"], fact.observed_at, Jsonb(payload)),
        )


def _upsert_current(cursor, table: str, columns: tuple[str, ...], fact: CanonicalFact) -> None:
    primary_values = (fact.source_id,) if len(columns) == 1 else (fact.source_id, fact.resource_id)
    insert_columns = ", ".join((*columns, "display_name", "state", "payload", "observed_at", "run_id"))
    placeholders = ", ".join("%s" for _ in range(len(columns) + 5))
    assignments = ", ".join(f"{column} = EXCLUDED.{column}" for column in ("display_name", "state", "payload", "observed_at", "run_id"))
    conflict_columns = ", ".join(columns)
    cursor.execute(
        f"""
        INSERT INTO {table} ({insert_columns}) VALUES ({placeholders})
        ON CONFLICT ({conflict_columns}) DO UPDATE SET {assignments}
        WHERE {table}.observed_at <= EXCLUDED.observed_at
        """,
        (*primary_values, fact.display_name, str(fact.payload["state"]), Jsonb(fact.payload), fact.observed_at, fact.run_id),
    )


def _quarantine(cursor, receipt_id: str, manifest: ArtifactManifest, lines: Iterable[tuple[int, Any]], diagnostic: str) -> None:
    for line_number, record in lines:
        record_key = record.get("recordKey") if isinstance(record, dict) and isinstance(record.get("recordKey"), str) else None
        record_type = record.get("recordType") if isinstance(record, dict) and isinstance(record.get("recordType"), str) else None
        cursor.execute(
            """
            INSERT INTO sentinel.collection_artifact_ledger (
                id, receipt_id, run_id, record_key, record_type, acceptance_state,
                diagnostic_code, metadata, created_at
            ) VALUES (%s, %s, %s, %s, %s, 'quarantined', %s, %s, NOW())
            ON CONFLICT (run_id, record_key) DO NOTHING
            """,
            (f"ledger-{uuid4().hex}", receipt_id, manifest.run_id, record_key or f"line-{line_number}", record_type, diagnostic, Jsonb({"line": line_number})),
        )
        cursor.execute(
            """
            INSERT INTO sentinel.collection_ingestion_errors (
                id, receipt_id, run_id, line_number, record_key, diagnostic_code, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s, NOW())
            """,
            (f"ingestion-error-{uuid4().hex}", receipt_id, manifest.run_id, line_number, record_key, diagnostic),
        )


def ingest_ndjson_artifact(manifest_payload: Any, artifact: bytes) -> IngestionResult:
    """Atomically accept or quarantine one already-uploaded sanitized artifact.

    The function is intentionally internal. Browser payloads and unrestricted
    Ansible output are not accepted by this boundary.
    """

    manifest = validate_manifest(manifest_payload, artifact)
    lines = _decode_lines(artifact)
    if len(lines) != manifest.record_count:
        raise ArtifactContractError("Artifact line count does not match the manifest.")
    now = datetime.now(timezone.utc)
    with database_connection() as connection, connection.transaction(), connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT id, state, accepted_records, quarantined_records
            FROM sentinel.collection_artifact_receipts
            WHERE run_id = %s AND artifact_sha256 = %s
            """,
            (manifest.run_id, manifest.sha256),
        )
        existing = cursor.fetchone()
        if existing is not None:
            return IngestionResult(existing[0], existing[1], existing[2], existing[3], True)
        binding = _run_binding(cursor, manifest)
        # A concurrent delivery may have completed while this transaction waited
        # to lock the run. Recheck after the lock so at-least-once replay remains
        # harmless instead of being misclassified as a terminal-run error.
        cursor.execute(
            """
            SELECT id, state, accepted_records, quarantined_records
            FROM sentinel.collection_artifact_receipts
            WHERE run_id = %s AND artifact_sha256 = %s
            """,
            (manifest.run_id, manifest.sha256),
        )
        existing = cursor.fetchone()
        if existing is not None:
            return IngestionResult(existing[0], existing[1], existing[2], existing[3], True)
        if binding["state"] in TERMINAL_RUN_STATES:
            raise ArtifactContractError("Terminal collection runs cannot accept a new artifact.")

        receipt_id = f"receipt-{uuid4().hex}"
        try:
            facts = [validate_fact_record(record, expected_run_id=manifest.run_id) for _, record in lines]
            if len({fact.sequence for fact in facts}) != len(facts) or {fact.sequence for fact in facts} != set(range(1, len(facts) + 1)):
                raise FactContractError("Artifact record sequences must be unique and contiguous.")
            if len({fact.record_key for fact in facts}) != len(facts):
                raise FactContractError("Artifact record keys must be unique.")
            for fact in facts:
                if (fact.source_type, fact.source_id) not in manifest.source_instances:
                    raise FactContractError("Record source is not declared by the manifest.")
                if not _source_exists(cursor, fact.source_type, fact.source_id):
                    raise FactContractError("Record source identity is not managed by Sentinel.")
                if fact.provenance["playbookPath"] != manifest.source_path or fact.provenance["playbookCommitSha"] != manifest.source_commit_sha:
                    raise FactContractError("Record playbook provenance does not match the manifest.")
        except (FactContractError, ArtifactContractError) as exc:
            diagnostic = "artifact-contract-invalid"
            cursor.execute(
                """
                INSERT INTO sentinel.collection_artifact_receipts (
                    id, run_id, artifact_location, artifact_sha256, manifest, state, record_count,
                    accepted_records, quarantined_records, received_at, completed_at, diagnostic_code
                ) VALUES (%s, %s, %s, %s, %s, 'rejected', %s, 0, %s, %s, %s, %s)
                """,
                (receipt_id, manifest.run_id, manifest.location, manifest.sha256, Jsonb(manifest_payload), manifest.record_count, len(lines), now, now, diagnostic),
            )
            _quarantine(cursor, receipt_id, manifest, lines, diagnostic)
            _transition(cursor, manifest.run_id, "failed", failure_reason="Artifact ingestion contract validation failed.")
            return IngestionResult(receipt_id, "rejected", 0, len(lines), False)

        _transition(cursor, manifest.run_id, "ingesting")
        cursor.execute(
            """
            INSERT INTO sentinel.collection_artifact_receipts (
                id, run_id, artifact_location, artifact_sha256, manifest, state, record_count,
                accepted_records, quarantined_records, received_at, completed_at
            ) VALUES (%s, %s, %s, %s, %s, 'accepted', %s, %s, 0, %s, %s)
            """,
            (receipt_id, manifest.run_id, manifest.location, manifest.sha256, Jsonb(manifest_payload), manifest.record_count, len(facts), now, now),
        )
        for fact in facts:
            cursor.execute(
                """
                INSERT INTO sentinel.collection_artifact_ledger (
                    id, receipt_id, run_id, record_key, sequence, record_type, record_sha256,
                    acceptance_state, metadata, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, 'accepted', %s, NOW())
                """,
                (f"ledger-{uuid4().hex}", receipt_id, fact.run_id, fact.record_key, fact.sequence, fact.record_type, fact.record_sha256, Jsonb(fact.ledger_metadata())),
            )
            _project_fact(cursor, fact)
        _transition(cursor, manifest.run_id, manifest.execution_status)
        return IngestionResult(receipt_id, "accepted", len(facts), 0, False)
