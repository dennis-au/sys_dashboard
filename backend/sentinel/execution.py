"""Target-free artifact construction used by a future supervised worker.

This module does not invoke Ansible or resolve an external secret reference. It owns
only the deterministic, sanitized handoff from a future executor to ingestion.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Iterable

from .facts import CanonicalFact, validate_fact_record
from .ingestion import ARTIFACT_SCHEMA_VERSION


@dataclass(frozen=True)
class PreparedArtifact:
    manifest: dict[str, Any]
    ndjson: bytes
    facts: tuple[CanonicalFact, ...]


def _timestamp(value: datetime | None) -> str:
    timestamp = (value or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return timestamp.isoformat(timespec="seconds").replace("+00:00", "Z")


def prepare_sanitized_artifact(
    *,
    run_id: str,
    records: Iterable[dict[str, Any]],
    source_instances: Iterable[dict[str, str]],
    collector_version: str,
    location: str,
    profile_id: str | None = None,
    source_path: str | None = None,
    source_commit_sha: str | None = None,
    execution_status: str = "completed",
    collection_started_at: datetime | None = None,
    collection_completed_at: datetime | None = None,
) -> PreparedArtifact:
    """Create the only artifact shape accepted by Sentinel ingestion."""

    raw_records = list(records)
    fact_list = tuple(validate_fact_record(record, expected_run_id=run_id) for record in raw_records)
    lines = [json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True) for record in raw_records]
    ndjson = ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8")
    artifact_sha256 = hashlib.sha256(ndjson).hexdigest()
    started_at = _timestamp(collection_started_at)
    completed_at = _timestamp(collection_completed_at or collection_started_at)
    manifest = {
        "schemaVersion": ARTIFACT_SCHEMA_VERSION,
        "runId": run_id,
        "profileId": profile_id,
        "source": {"path": source_path, "commitSha": source_commit_sha},
        "collectorVersion": collector_version,
        "collectionStartedAt": started_at,
        "collectionCompletedAt": completed_at,
        "sourceInstances": list(source_instances),
        "executionStatus": execution_status,
        "artifact": {
            "location": location,
            "schemaVersion": 1,
            "sha256": artifact_sha256,
            "recordCount": len(fact_list),
        },
    }
    return PreparedArtifact(manifest=manifest, ndjson=ndjson, facts=fact_list)
