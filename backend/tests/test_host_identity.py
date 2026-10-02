import os
import sys
import uuid
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from sentinel.inventory import migrate_host_identities
from sentinel.reporting import inventory_read_model


DATABASE_URL = os.environ["DATABASE_URL"]


def _host_payload(name: str) -> dict[str, object]:
    return {
        "name": name,
        "role": "Regression target",
        "status": "healthy",
        "environment": "Operations",
        "os": "Linux · SSH",
        "ip": "198.51.100.90",
        "ips": ["198.51.100.90"],
        "collected": "Not collected",
        "uptime": "Not collected",
        "disk": "--",
        "memory": "--",
        "tags": ["manual"],
        "sourceType": "manual",
        "lifecycle": "active",
        "connectionUser": "root",
        "connectionPort": "22",
        "credential": "secret://sentinel/managed-ssh/regression",
    }


def _delete_host_data(*host_ids: str, run_id: str | None = None, receipt_id: str | None = None) -> None:
    with psycopg.connect(DATABASE_URL, autocommit=True) as connection, connection.cursor() as cursor:
        if receipt_id:
            cursor.execute("DELETE FROM sentinel.collection_artifact_ledger WHERE receipt_id = %s", (receipt_id,))
            cursor.execute("DELETE FROM sentinel.collection_ingestion_errors WHERE receipt_id = %s", (receipt_id,))
            cursor.execute("DELETE FROM sentinel.collection_artifact_receipts WHERE id = %s", (receipt_id,))
        if run_id:
            cursor.execute("DELETE FROM sentinel.collection_host_results WHERE run_id = %s", (run_id,))
            cursor.execute("DELETE FROM sentinel.host_capacity_facts WHERE run_id = %s", (run_id,))
            cursor.execute("DELETE FROM sentinel.collection_alerts WHERE run_id = %s", (run_id,))
            cursor.execute("DELETE FROM sentinel.collection_run_details WHERE run_id = %s", (run_id,))
            cursor.execute("DELETE FROM sentinel.collection_runs WHERE id = %s", (run_id,))
        if host_ids:
            cursor.execute("DELETE FROM sentinel.linux_filesystem_snapshots WHERE host_id = ANY(%s)", (list(host_ids),))
            cursor.execute("DELETE FROM sentinel.linux_filesystem_current WHERE host_id = ANY(%s)", (list(host_ids),))
            cursor.execute("DELETE FROM sentinel.linux_system_current WHERE host_id = ANY(%s)", (list(host_ids),))
            cursor.execute("DELETE FROM sentinel.collection_alerts WHERE host_id = ANY(%s)", (list(host_ids),))
            cursor.execute("DELETE FROM sentinel.host_capacity_facts WHERE host_id = ANY(%s)", (list(host_ids),))
            cursor.execute("DELETE FROM sentinel.collection_host_results WHERE host_id = ANY(%s)", (list(host_ids),))
            cursor.execute("DELETE FROM sentinel.hosts WHERE id = ANY(%s)", (list(host_ids),))


def test_host_identity_migration_remaps_mutable_projections_and_preserves_artifact_evidence():
    suffix = uuid.uuid4().hex[:8]
    legacy_id = f"legacy-host-{suffix}"
    run_id = f"host-identity-regression-{suffix}"
    receipt_id = f"receipt-host-identity-{suffix}"
    artifact_manifest = {"sourceInstances": [{"type": "linux", "id": legacy_id}]}
    migrated_id: str | None = None
    try:
        with psycopg.connect(DATABASE_URL, autocommit=True) as connection, connection.cursor() as cursor:
            cursor.execute("INSERT INTO sentinel.hosts (id, payload) VALUES (%s, %s)", (legacy_id, Jsonb(_host_payload(legacy_id))))
            cursor.execute("INSERT INTO sentinel.collection_runs (id, payload) VALUES (%s, %s)", (run_id, Jsonb({"id": run_id, "expectedSourceInstances": artifact_manifest["sourceInstances"]})))
            cursor.execute(
                """
                INSERT INTO sentinel.collection_run_details (
                    run_id, name, source_type, execution_mode, state, requested_at, metadata
                ) VALUES (%s, %s, 'profile', 'live', 'completed', NOW(), %s)
                """,
                (run_id, f"host identity regression {suffix}", Jsonb({"expectedSourceInstances": artifact_manifest["sourceInstances"]})),
            )
            cursor.execute(
                """
                INSERT INTO sentinel.collection_host_results (
                    id, run_id, host_id, host_name, source_type, execution_mode, state, completed_at
                ) VALUES (%s, %s, %s, %s, 'linux', 'live', 'success', NOW())
                """,
                (f"result-{suffix}", run_id, legacy_id, legacy_id),
            )
            cursor.execute(
                """
                INSERT INTO sentinel.host_capacity_facts (
                    id, run_id, host_id, host_name, observed_at, cpu_cores, memory_total_bytes,
                    memory_used_bytes, disk_total_bytes, disk_used_bytes, execution_mode
                ) VALUES (%s, %s, %s, %s, NOW(), 2, 100, 50, 200, 75, 'live')
                """,
                (f"capacity-{suffix}", run_id, legacy_id, legacy_id),
            )
            cursor.execute(
                """
                INSERT INTO sentinel.collection_alerts (
                    id, run_id, host_id, host_name, severity, state, category, summary, observed_at, execution_mode
                ) VALUES (%s, %s, %s, %s, 'warning', 'open', 'identity.regression', 'migration', NOW(), 'live')
                """,
                (f"alert-{suffix}", run_id, legacy_id, legacy_id),
            )
            cursor.execute(
                """
                INSERT INTO sentinel.linux_system_current (
                    host_id, hostname, os_family, distribution, distribution_version, kernel, architecture, observed_at, run_id
                ) VALUES (%s, 'legacy-host', 'RedHat', 'Rocky', '9.4', '5.14', 'x86_64', NOW(), %s)
                """,
                (legacy_id, run_id),
            )
            cursor.execute(
                """
                INSERT INTO sentinel.linux_filesystem_current (
                    host_id, filesystem_id, mount_path, filesystem_type, total_bytes, used_bytes, available_bytes, observed_at, run_id
                ) VALUES (%s, 'fs-root', '/', 'xfs', 200, 75, 125, NOW(), %s)
                """,
                (legacy_id, run_id),
            )
            cursor.execute(
                """
                INSERT INTO sentinel.linux_filesystem_snapshots (
                    record_key, run_id, host_id, filesystem_id, mount_path, total_bytes, used_bytes, available_bytes, observed_at
                ) VALUES (%s, %s, %s, 'fs-root', '/', 200, 75, 125, NOW())
                """,
                (f"filesystem-{suffix}", run_id, legacy_id),
            )
            cursor.execute(
                """
                INSERT INTO sentinel.collection_artifact_receipts (
                    id, run_id, artifact_location, artifact_sha256, manifest, state, record_count,
                    accepted_records, quarantined_records, received_at, completed_at
                ) VALUES (%s, %s, %s, %s, %s, 'accepted', 0, 0, 0, NOW(), NOW())
                """,
                (receipt_id, run_id, f"{run_id}/facts.ndjson.gz", "a" * 64, Jsonb(artifact_manifest)),
            )

        migrate_host_identities(force=True)

        with psycopg.connect(DATABASE_URL, autocommit=True) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT id, payload FROM sentinel.hosts WHERE payload->>'name' = %s", (legacy_id,))
            migrated_id, payload = cursor.fetchone()
            assert migrated_id.startswith("host-")
            assert payload["id"] == migrated_id
            for table_name in (
                "sentinel.collection_host_results",
                "sentinel.host_capacity_facts",
                "sentinel.collection_alerts",
                "sentinel.linux_system_current",
                "sentinel.linux_filesystem_current",
                "sentinel.linux_filesystem_snapshots",
            ):
                cursor.execute(f"SELECT host_id FROM {table_name} WHERE host_id = %s", (migrated_id,))
                assert cursor.fetchone() is not None
            cursor.execute("SELECT metadata FROM sentinel.collection_run_details WHERE run_id = %s", (run_id,))
            assert cursor.fetchone()[0]["expectedSourceInstances"] == [{"type": "linux", "id": migrated_id}]
            cursor.execute("SELECT payload FROM sentinel.collection_runs WHERE id = %s", (run_id,))
            assert cursor.fetchone()[0]["expectedSourceInstances"] == [{"type": "linux", "id": migrated_id}]
            cursor.execute("SELECT manifest FROM sentinel.collection_artifact_receipts WHERE id = %s", (receipt_id,))
            assert cursor.fetchone()[0] == artifact_manifest
    finally:
        _delete_host_data(legacy_id, *( [migrated_id] if migrated_id else [] ), run_id=run_id, receipt_id=receipt_id)


def test_inventory_read_model_uses_stable_identity_and_does_not_inherit_reused_name_history():
    suffix = uuid.uuid4().hex[:8]
    name = f"identity-reused-name-{suffix}"
    retired_id = f"host-retired-{suffix}"
    replacement_id = f"host-replacement-{suffix}"
    run_id = f"identity-reuse-run-{suffix}"
    try:
        with psycopg.connect(DATABASE_URL, autocommit=True) as connection, connection.cursor() as cursor:
            cursor.execute("INSERT INTO sentinel.hosts (id, payload) VALUES (%s, %s)", (retired_id, Jsonb({**_host_payload(name), "id": retired_id})))
            cursor.execute(
                """
                INSERT INTO sentinel.collection_host_results (
                    id, run_id, host_id, host_name, source_type, execution_mode, state, completed_at
                ) VALUES (%s, %s, %s, %s, 'linux', 'live', 'success', NOW())
                """,
                (f"result-reuse-{suffix}", run_id, retired_id, name),
            )
            cursor.execute(
                """
                INSERT INTO sentinel.linux_system_current (
                    host_id, hostname, os_family, distribution, distribution_version, kernel, architecture, observed_at, run_id
                ) VALUES (%s, %s, 'RedHat', 'Rocky', '9.4', '5.14', 'x86_64', NOW(), %s)
                """,
                (retired_id, name, run_id),
            )
            cursor.execute("DELETE FROM sentinel.hosts WHERE id = %s", (retired_id,))
            cursor.execute("INSERT INTO sentinel.hosts (id, payload) VALUES (%s, %s)", (replacement_id, Jsonb({**_host_payload(name), "id": replacement_id})))

        host = next(item for item in inventory_read_model() if item["id"] == replacement_id)
        assert host["name"] == name
        assert host["collection"] == {"state": "not_collected", "collectedAt": None, "runId": None}
        assert host["collected"] == "Not collected"
        assert host["os"] == "Linux · SSH"
    finally:
        _delete_host_data(retired_id, replacement_id, run_id=run_id)


def test_inventory_read_model_projects_accepted_linux_facts_without_mutating_host_configuration():
    suffix = uuid.uuid4().hex[:8]
    host_id = f"host-projection-{suffix}"
    name = f"identity-projection-{suffix}"
    run_id = f"identity-projection-run-{suffix}"
    try:
        with psycopg.connect(DATABASE_URL, autocommit=True) as connection, connection.cursor() as cursor:
            cursor.execute("INSERT INTO sentinel.hosts (id, payload) VALUES (%s, %s)", (host_id, Jsonb({**_host_payload(name), "id": host_id})))
            cursor.execute(
                """
                INSERT INTO sentinel.collection_host_results (
                    id, run_id, host_id, host_name, source_type, execution_mode, state, completed_at
                ) VALUES (%s, %s, %s, %s, 'linux', 'live', 'success', NOW())
                """,
                (f"result-projection-{suffix}", run_id, host_id, name),
            )
            cursor.execute(
                """
                INSERT INTO sentinel.linux_system_current (
                    host_id, hostname, os_family, distribution, distribution_version, kernel, architecture, observed_at, run_id
                ) VALUES (%s, %s, 'RedHat', 'Rocky', '9.4', '5.14', 'x86_64', NOW(), %s)
                """,
                (host_id, name, run_id),
            )
            cursor.execute(
                """
                INSERT INTO sentinel.host_capacity_facts (
                    id, run_id, host_id, host_name, observed_at, cpu_cores, memory_total_bytes,
                    memory_used_bytes, disk_total_bytes, disk_used_bytes, execution_mode
                ) VALUES (%s, %s, %s, %s, NOW(), 2, 100, 50, 200, 75, 'live')
                """,
                (f"capacity-projection-{suffix}", run_id, host_id, name),
            )

        host = next(item for item in inventory_read_model() if item["id"] == host_id)
        assert host["collection"]["state"] == "success"
        assert host["collection"]["runId"] == run_id
        assert host["collected"] != "Not collected"
        assert host["os"] == "Rocky 9.4 · x86_64"
        assert host["memory"] == "50%"
        assert host["disk"] == "37.5%"
        with psycopg.connect(DATABASE_URL, autocommit=True) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT payload FROM sentinel.hosts WHERE id = %s", (host_id,))
            assert cursor.fetchone()[0]["collected"] == "Not collected"
    finally:
        _delete_host_data(host_id, run_id=run_id)
