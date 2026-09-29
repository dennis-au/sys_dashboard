from datetime import datetime, timezone
import hashlib
import json
import os
import sys
from pathlib import Path

import psycopg
import pytest
from psycopg.types.json import Jsonb


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from sentinel.artifacts import ArtifactStorageError, read_sanitized_artifact, write_sanitized_artifact
from sentinel.execution import prepare_sanitized_artifact
from sentinel.facts import FactContractError, deterministic_record_key, validate_fact_record
from sentinel import ingestion
from sentinel.ingestion import ArtifactContractError, ingest_ndjson_artifact


DATABASE_URL = os.environ["DATABASE_URL"]
RUN_ID = "ingestion-regression-run"
NOW = "2026-09-28T04:05:00Z"
SHA = "a" * 40
SOURCE_ID = "ingestion-regression-host"


def filesystem_record(*, run_id: str = RUN_ID, payload: dict | None = None) -> dict:
    values = {
        "mountPath": "/",
        "filesystemType": "ext4",
        "totalBytes": 1000,
        "usedBytes": 400,
        "availableBytes": 600,
    }
    values.update(payload or {})
    return {
        "schemaVersion": 1,
        "recordType": "linux.filesystem_snapshot.v1",
        "runId": run_id,
        "sequence": 1,
        "collectedAt": NOW,
        "observedAt": NOW,
        "source": {"type": "linux", "id": SOURCE_ID},
        "resource": {"kind": "filesystem", "id": "rootfs", "displayName": "/"},
        "payload": values,
        "provenance": {
            "collector": "sentinel-test",
            "playbookPath": "inventory/linux-facts.yml",
            "playbookCommitSha": SHA,
        },
        "recordKey": deterministic_record_key(
            record_type="linux.filesystem_snapshot.v1",
            source_type="linux",
            source_id=SOURCE_ID,
            resource_kind="filesystem",
            resource_id="rootfs",
            observed_at=NOW,
        ),
    }


def insert_artifact_run(*, expected_sources=None) -> None:
    with psycopg.connect(DATABASE_URL, autocommit=True) as connection, connection.cursor() as cursor:
        cursor.execute("DELETE FROM sentinel.hosts WHERE id = %s", (SOURCE_ID,))
        cursor.execute(
            "INSERT INTO sentinel.hosts (id, payload) VALUES (%s, %s)",
            (
                SOURCE_ID,
                Jsonb(
                    {
                        "name": SOURCE_ID,
                        "ip": "198.51.100.50",
                        "ips": ["198.51.100.50"],
                        "sourceType": "manual",
                        "lifecycle": "active",
                    }
                ),
            ),
        )
        cursor.execute("DELETE FROM sentinel.collection_artifact_ledger WHERE run_id = %s", (RUN_ID,))
        cursor.execute("DELETE FROM sentinel.collection_ingestion_errors WHERE run_id = %s", (RUN_ID,))
        cursor.execute("DELETE FROM sentinel.collection_artifact_receipts WHERE run_id = %s", (RUN_ID,))
        cursor.execute("DELETE FROM sentinel.linux_filesystem_current WHERE run_id = %s", (RUN_ID,))
        cursor.execute("DELETE FROM sentinel.linux_filesystem_snapshots WHERE run_id = %s", (RUN_ID,))
        cursor.execute("DELETE FROM sentinel.collection_run_details WHERE run_id = %s", (RUN_ID,))
        cursor.execute(
            """
            INSERT INTO sentinel.collection_run_details (
                run_id, name, profile_id, source_type, source_path, source_commit_sha,
                source_state, execution_mode, state, requested_at, metadata
            ) VALUES (%s, 'ingestion regression', 'linux-inventory-facts', 'profile', 'inventory/linux-facts.yml', %s,
                      'pinned', 'simulation', 'artifact-uploaded', NOW(), %s)
            """,
            (RUN_ID, SHA, Jsonb({"expectedSourceInstances": expected_sources or [{"type": "linux", "id": SOURCE_ID}]})),
        )


@pytest.fixture(autouse=True)
def artifact_run():
    insert_artifact_run()
    yield
    insert_artifact_run()
    with psycopg.connect(DATABASE_URL, autocommit=True) as connection, connection.cursor() as cursor:
        cursor.execute("DELETE FROM sentinel.collection_artifact_ledger WHERE run_id = %s", (RUN_ID,))
        cursor.execute("DELETE FROM sentinel.collection_ingestion_errors WHERE run_id = %s", (RUN_ID,))
        cursor.execute("DELETE FROM sentinel.collection_artifact_receipts WHERE run_id = %s", (RUN_ID,))
        cursor.execute("DELETE FROM sentinel.linux_filesystem_current WHERE run_id = %s", (RUN_ID,))
        cursor.execute("DELETE FROM sentinel.linux_filesystem_snapshots WHERE run_id = %s", (RUN_ID,))
        cursor.execute("DELETE FROM sentinel.collection_run_details WHERE run_id = %s", (RUN_ID,))
        cursor.execute("DELETE FROM sentinel.hosts WHERE id = %s", (SOURCE_ID,))


def test_linux_artifact_is_projected_once_and_replay_is_harmless():
    artifact = prepare_sanitized_artifact(
        run_id=RUN_ID,
        records=[filesystem_record()],
        source_instances=[{"type": "linux", "id": SOURCE_ID}],
        collector_version="sentinel-test",
        location=f"{RUN_ID}/facts.ndjson.gz",
        profile_id="linux-inventory-facts",
        source_path="inventory/linux-facts.yml",
        source_commit_sha=SHA,
        collection_started_at=datetime(2026, 9, 28, 4, 4, tzinfo=timezone.utc),
        collection_completed_at=datetime(2026, 9, 28, 4, 5, tzinfo=timezone.utc),
    )

    accepted = ingest_ndjson_artifact(artifact.manifest, artifact.ndjson)
    replay = ingest_ndjson_artifact(artifact.manifest, artifact.ndjson)

    assert accepted.state == "accepted"
    assert accepted.accepted_records == 1
    assert accepted.replayed is False
    assert replay.receipt_id == accepted.receipt_id
    assert replay.replayed is True
    with psycopg.connect(DATABASE_URL) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT state FROM sentinel.collection_run_details WHERE run_id = %s", (RUN_ID,))
        assert cursor.fetchone()[0] == "completed"
        cursor.execute(
            "SELECT mount_path, total_bytes, used_bytes, available_bytes FROM reporting.linux_filesystem_current WHERE run_id = %s",
            (RUN_ID,),
        )
        assert cursor.fetchone() == ("/", 1000, 400, 600)
        cursor.execute("SELECT COUNT(*) FROM sentinel.collection_artifact_ledger WHERE run_id = %s", (RUN_ID,))
        assert cursor.fetchone()[0] == 1


def test_invalid_secret_bearing_record_is_quarantined_without_a_projection():
    record = filesystem_record(payload={"apiToken": "must-not-persist"})
    ndjson = (json.dumps(record, sort_keys=True) + "\n").encode("utf-8")
    manifest = {
        "schemaVersion": 1,
        "runId": RUN_ID,
        "profileId": "linux-inventory-facts",
        "source": {"path": "inventory/linux-facts.yml", "commitSha": SHA},
        "collectorVersion": "sentinel-test",
        "collectionStartedAt": NOW,
        "collectionCompletedAt": NOW,
        "sourceInstances": [{"type": "linux", "id": SOURCE_ID}],
        "executionStatus": "completed",
        "artifact": {"location": f"{RUN_ID}/invalid.ndjson.gz", "schemaVersion": 1, "sha256": hashlib.sha256(ndjson).hexdigest(), "recordCount": 1},
    }

    result = ingest_ndjson_artifact(manifest, ndjson)

    assert result == result.__class__(result.receipt_id, "rejected", 0, 1, False)
    with psycopg.connect(DATABASE_URL) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT COUNT(*) FROM sentinel.linux_filesystem_current WHERE run_id = %s", (RUN_ID,))
        assert cursor.fetchone()[0] == 0
        cursor.execute("SELECT diagnostic_code FROM sentinel.collection_ingestion_errors WHERE run_id = %s", (RUN_ID,))
        assert cursor.fetchone()[0] == "artifact-contract-invalid"
        cursor.execute("SELECT failure_reason FROM sentinel.collection_run_details WHERE run_id = %s", (RUN_ID,))
        assert cursor.fetchone()[0] == "Artifact ingestion contract validation failed."


def test_manifest_source_binding_rejects_an_unreserved_source():
    insert_artifact_run(expected_sources=[{"type": "linux", "id": "web-prod-02"}])
    artifact = prepare_sanitized_artifact(
        run_id=RUN_ID,
        records=[filesystem_record()],
        source_instances=[{"type": "linux", "id": SOURCE_ID}],
        collector_version="sentinel-test",
        location=f"{RUN_ID}/facts.ndjson.gz",
        profile_id="linux-inventory-facts",
        source_path="inventory/linux-facts.yml",
        source_commit_sha=SHA,
    )

    with pytest.raises(ArtifactContractError, match="source instances"):
        ingest_ndjson_artifact(artifact.manifest, artifact.ndjson)


def test_fact_contract_requires_deterministic_keys_and_normalized_units():
    record = filesystem_record()
    record["recordKey"] = "not-deterministic"
    with pytest.raises(FactContractError, match="recordKey"):
        validate_fact_record(record, expected_run_id=RUN_ID)

    record = filesystem_record(payload={"totalBytes": 10, "usedBytes": 11})
    with pytest.raises(FactContractError, match="cannot exceed"):
        validate_fact_record(record, expected_run_id=RUN_ID)

    record = filesystem_record()
    record["provenance"].pop("playbookCommitSha")
    with pytest.raises(FactContractError, match="provenance.playbookCommitSha"):
        validate_fact_record(record, expected_run_id=RUN_ID)


def test_record_playbook_provenance_must_match_the_manifest():
    record = filesystem_record()
    record["provenance"]["playbookCommitSha"] = "b" * 40
    artifact = prepare_sanitized_artifact(
        run_id=RUN_ID,
        records=[record],
        source_instances=[{"type": "linux", "id": SOURCE_ID}],
        collector_version="sentinel-test",
        location=f"{RUN_ID}/provenance.ndjson.gz",
        profile_id="linux-inventory-facts",
        source_path="inventory/linux-facts.yml",
        source_commit_sha=SHA,
    )

    result = ingest_ndjson_artifact(artifact.manifest, artifact.ndjson)

    assert result.state == "rejected"
    with psycopg.connect(DATABASE_URL) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT COUNT(*) FROM sentinel.linux_filesystem_current WHERE run_id = %s", (RUN_ID,))
        assert cursor.fetchone()[0] == 0


def test_interrupted_ingestion_rolls_back_the_receipt_ledger_and_projection(monkeypatch):
    artifact = prepare_sanitized_artifact(
        run_id=RUN_ID,
        records=[filesystem_record()],
        source_instances=[{"type": "linux", "id": SOURCE_ID}],
        collector_version="sentinel-test",
        location=f"{RUN_ID}/interrupted.ndjson.gz",
        profile_id="linux-inventory-facts",
        source_path="inventory/linux-facts.yml",
        source_commit_sha=SHA,
    )

    def interrupt_projection(*_args) -> None:
        raise RuntimeError("simulated worker interruption")

    monkeypatch.setattr(ingestion, "_project_fact", interrupt_projection)

    with pytest.raises(RuntimeError, match="simulated worker interruption"):
        ingest_ndjson_artifact(artifact.manifest, artifact.ndjson)

    with psycopg.connect(DATABASE_URL) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT state FROM sentinel.collection_run_details WHERE run_id = %s", (RUN_ID,))
        assert cursor.fetchone()[0] == "artifact-uploaded"
        cursor.execute("SELECT COUNT(*) FROM sentinel.collection_artifact_receipts WHERE run_id = %s", (RUN_ID,))
        assert cursor.fetchone()[0] == 0
        cursor.execute("SELECT COUNT(*) FROM sentinel.collection_artifact_ledger WHERE run_id = %s", (RUN_ID,))
        assert cursor.fetchone()[0] == 0
        cursor.execute("SELECT COUNT(*) FROM sentinel.linux_filesystem_current WHERE run_id = %s", (RUN_ID,))
        assert cursor.fetchone()[0] == 0


def test_sanitized_artifacts_are_written_atomically_and_checksum_verified(tmp_path: Path):
    ndjson = b'{"recordType":"linux.filesystem_snapshot.v1"}\n'
    location = "run-1/facts.ndjson.gz"

    destination = write_sanitized_artifact(tmp_path, location, ndjson)

    assert destination == tmp_path / location
    assert read_sanitized_artifact(tmp_path, location, hashlib.sha256(ndjson).hexdigest()) == ndjson
    with pytest.raises(ArtifactStorageError, match="protected relative"):
        write_sanitized_artifact(tmp_path, "../escape.ndjson.gz", ndjson)
