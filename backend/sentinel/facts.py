"""Canonical, sanitized collection-fact contracts.

The ingestion boundary accepts only these typed records.  It intentionally has
no dependency on Ansible or an external source system, so an execution worker
cannot turn arbitrary stdout into reporting data.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any


SCHEMA_VERSION = 1
SENSITIVE_FIELD_MARKERS = (
    "password",
    "secret",
    "token",
    "privatekey",
    "private_key",
    "authorization",
    "kubeconfig",
    "clientkey",
    "client_key",
)
RECORD_TYPE_SOURCE_TYPES = {
    "linux.filesystem_snapshot.v1": "linux",
    "ovirt.vm_fact.v1": "olvm",
    "ovirt.storage_capacity_snapshot.v1": "olvm",
    "k8s.cluster_fact.v1": "kubernetes",
    "k8s.node_fact.v1": "kubernetes",
    "k8s.node_capacity_snapshot.v1": "kubernetes",
    "k8s.namespace_capacity_snapshot.v1": "kubernetes",
    "k8s.workload_fact.v1": "kubernetes",
    "k8s.pod_fact.v1": "kubernetes",
    "k8s.pod_metric_snapshot.v1": "kubernetes",
    "k8s.pvc_snapshot.v1": "kubernetes",
}
CURRENT_RECORD_TYPES = {
    "ovirt.vm_fact.v1",
    "k8s.cluster_fact.v1",
    "k8s.node_fact.v1",
    "k8s.workload_fact.v1",
    "k8s.pod_fact.v1",
    "k8s.pvc_snapshot.v1",
}
SNAPSHOT_RECORD_TYPES = set(RECORD_TYPE_SOURCE_TYPES) - CURRENT_RECORD_TYPES
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,255}$")


class FactContractError(ValueError):
    """A record cannot be retained or projected by Sentinel."""


@dataclass(frozen=True)
class CanonicalFact:
    schema_version: int
    record_type: str
    run_id: str
    record_key: str
    sequence: int
    collected_at: str
    observed_at: str
    source_type: str
    source_id: str
    resource_kind: str
    resource_id: str
    display_name: str | None
    payload: dict[str, Any]
    provenance: dict[str, Any]
    record_sha256: str

    def ledger_metadata(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "recordType": self.record_type,
            "sourceType": self.source_type,
            "sourceId": self.source_id,
            "resourceKind": self.resource_kind,
            "resourceId": self.resource_id,
            "observedAt": self.observed_at,
        }


def _require_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FactContractError(f"{label} is required.")
    return value.strip()


def _require_identifier(value: Any, label: str) -> str:
    normalized = _require_text(value, label)
    if not _IDENTIFIER.fullmatch(normalized):
        raise FactContractError(f"{label} has an invalid format.")
    return normalized


def _timestamp(value: Any, label: str) -> str:
    text = _require_text(value, label)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise FactContractError(f"{label} must be an ISO 8601 timestamp.") from exc
    if parsed.tzinfo is None:
        raise FactContractError(f"{label} must include a timezone.")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _safe_value(value: Any, *, depth: int = 0) -> None:
    if depth > 16:
        raise FactContractError("Record nesting exceeds the supported limit.")
    if isinstance(value, dict):
        for key, nested in value.items():
            if not isinstance(key, str):
                raise FactContractError("Record object keys must be strings.")
            normalized = key.lower().replace("-", "").replace("_", "")
            if any(marker.replace("_", "") in normalized for marker in SENSITIVE_FIELD_MARKERS):
                raise FactContractError("Record contains a prohibited secret-bearing field.")
            _safe_value(nested, depth=depth + 1)
    elif isinstance(value, list):
        for nested in value:
            _safe_value(nested, depth=depth + 1)
    elif not isinstance(value, (str, int, float, bool, type(None))):
        raise FactContractError("Record contains an unsupported value type.")


def _non_negative_integer(payload: dict[str, Any], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise FactContractError(f"payload.{key} must be a non-negative integer.")
    return value


def _require_payload_fields(record_type: str, payload: dict[str, Any]) -> None:
    if record_type == "linux.filesystem_snapshot.v1":
        _require_text(payload.get("mountPath"), "payload.mountPath")
        _require_text(payload.get("filesystemType"), "payload.filesystemType")
        total = _non_negative_integer(payload, "totalBytes")
        used = _non_negative_integer(payload, "usedBytes")
        _non_negative_integer(payload, "availableBytes")
        if used > total:
            raise FactContractError("payload.usedBytes cannot exceed payload.totalBytes.")
    elif record_type == "ovirt.storage_capacity_snapshot.v1":
        total = _non_negative_integer(payload, "totalBytes")
        used = _non_negative_integer(payload, "usedBytes")
        if used > total:
            raise FactContractError("payload.usedBytes cannot exceed payload.totalBytes.")
    elif record_type == "k8s.node_capacity_snapshot.v1":
        _non_negative_integer(payload, "allocatableCpuMillicores")
        _non_negative_integer(payload, "allocatableMemoryBytes")
    elif record_type == "k8s.namespace_capacity_snapshot.v1":
        _non_negative_integer(payload, "requestedCpuMillicores")
        _non_negative_integer(payload, "requestedMemoryBytes")
    elif record_type == "k8s.pod_metric_snapshot.v1":
        _non_negative_integer(payload, "cpuMillicores")
        _non_negative_integer(payload, "memoryBytes")
    elif record_type in CURRENT_RECORD_TYPES:
        _require_text(payload.get("state"), "payload.state")


def _provenance(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        raise FactContractError("provenance must be an object.")
    collector = _require_identifier(value.get("collector"), "provenance.collector")
    playbook_path = _require_text(value.get("playbookPath"), "provenance.playbookPath")
    playbook_commit_sha = _require_text(value.get("playbookCommitSha"), "provenance.playbookCommitSha").lower()
    if len(playbook_commit_sha) != 40 or any(character not in "0123456789abcdef" for character in playbook_commit_sha):
        raise FactContractError("provenance.playbookCommitSha must be a 40-character Git SHA.")
    return {
        "collector": collector,
        "playbookPath": playbook_path,
        "playbookCommitSha": playbook_commit_sha,
    }


def deterministic_record_key(
    *, record_type: str, source_type: str, source_id: str, resource_kind: str, resource_id: str, observed_at: str
) -> str:
    """Create a stable, source-qualified key for exactly one collected record."""

    identity = "\x1f".join((record_type, source_type, source_id, resource_kind, resource_id, observed_at))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _canonical_bytes(record: dict[str, Any]) -> bytes:
    return json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def validate_fact_record(record: Any, *, expected_run_id: str) -> CanonicalFact:
    """Validate a decoded NDJSON line before any database projection occurs."""

    if not isinstance(record, dict):
        raise FactContractError("Each artifact line must contain an object.")
    _safe_value(record)
    schema_version = record.get("schemaVersion")
    if schema_version != SCHEMA_VERSION:
        raise FactContractError("Unsupported record schema version.")
    record_type = _require_text(record.get("recordType"), "recordType")
    source_type = RECORD_TYPE_SOURCE_TYPES.get(record_type)
    if source_type is None:
        raise FactContractError("Unsupported record type.")
    run_id = _require_identifier(record.get("runId"), "runId")
    if run_id != expected_run_id:
        raise FactContractError("Record runId does not match the manifest.")
    sequence = record.get("sequence")
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
        raise FactContractError("sequence must be a positive integer.")
    collected_at = _timestamp(record.get("collectedAt"), "collectedAt")
    observed_at = _timestamp(record.get("observedAt", collected_at), "observedAt")
    source = record.get("source")
    resource = record.get("resource")
    if not isinstance(source, dict) or not isinstance(resource, dict):
        raise FactContractError("source and resource objects are required.")
    supplied_source_type = _require_identifier(source.get("type"), "source.type")
    if supplied_source_type != source_type:
        raise FactContractError("recordType and source.type do not match.")
    source_id = _require_identifier(source.get("id"), "source.id")
    resource_kind = _require_identifier(resource.get("kind"), "resource.kind")
    resource_id = _require_identifier(resource.get("id"), "resource.id")
    display_name = resource.get("displayName")
    if display_name is not None and (not isinstance(display_name, str) or len(display_name) > 512):
        raise FactContractError("resource.displayName is invalid.")
    payload = record.get("payload")
    if not isinstance(payload, dict):
        raise FactContractError("payload must be an object.")
    _require_payload_fields(record_type, payload)
    provenance = _provenance(record.get("provenance"))
    expected_key = deterministic_record_key(
        record_type=record_type,
        source_type=source_type,
        source_id=source_id,
        resource_kind=resource_kind,
        resource_id=resource_id,
        observed_at=observed_at,
    )
    if _require_text(record.get("recordKey"), "recordKey") != expected_key:
        raise FactContractError("recordKey is not deterministic for the supplied identity.")
    return CanonicalFact(
        schema_version=schema_version,
        record_type=record_type,
        run_id=run_id,
        record_key=expected_key,
        sequence=sequence,
        collected_at=collected_at,
        observed_at=observed_at,
        source_type=source_type,
        source_id=source_id,
        resource_kind=resource_kind,
        resource_id=resource_id,
        display_name=display_name,
        payload=payload,
        provenance=provenance,
        record_sha256=hashlib.sha256(_canonical_bytes(record)).hexdigest(),
    )
