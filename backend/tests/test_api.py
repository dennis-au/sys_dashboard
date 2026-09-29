import os
import sys
import uuid
from pathlib import Path

import httpx
import psycopg
import pytest
from psycopg.types.json import Jsonb


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from sentinel.credentials import migrate_legacy_secret_references


API_BASE_URL = os.environ.get("API_BASE_URL", "http://127.0.0.1:8000")
DATABASE_URL = os.environ["DATABASE_URL"]


def cleanup_regression_records() -> None:
    with psycopg.connect(DATABASE_URL, autocommit=True) as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            DELETE FROM sentinel.collection_host_results
            WHERE run_id IN (
                SELECT run_id FROM sentinel.collection_run_details
                WHERE name LIKE '%regression-%'
            )
            """
        )
        cursor.execute(
            "DELETE FROM sentinel.collection_run_details WHERE name LIKE '%regression-%'"
        )
        for table in (
            "sentinel.hosts",
            "sentinel.credentials",
            "sentinel.managers",
            "sentinel.collection_profiles",
        ):
            cursor.execute(f"DELETE FROM {table} WHERE id LIKE '%regression%'")
        cursor.execute(
            "DELETE FROM sentinel.collection_runs WHERE payload->>'name' LIKE '%regression-%'"
        )


@pytest.fixture(autouse=True)
def remove_regression_records():
    cleanup_regression_records()
    yield
    cleanup_regression_records()


@pytest.fixture
def client():
    with httpx.Client(base_url=API_BASE_URL, timeout=10.0) as session:
        yield session


def create_ssh_key(client: httpx.Client, suffix: str) -> tuple[str, str]:
    reference = f"secret://sentinel/inventory/regression-{suffix}"
    response = client.post(
        "/api/settings/ssh-keys",
        json={
            "name": f"Regression SSH key {suffix}",
            "reference": reference,
            "type": "SSH key",
            "principal": "ansible",
            "scope": "Regression targets",
            "state": "active",
        },
    )
    assert response.status_code == 201
    return response.json()["id"], reference


def delete_run(run_id: str) -> None:
    with psycopg.connect(DATABASE_URL, autocommit=True) as connection, connection.cursor() as cursor:
        cursor.execute("DELETE FROM sentinel.collection_host_results WHERE run_id = %s", (run_id,))
        cursor.execute("DELETE FROM sentinel.host_capacity_facts WHERE run_id = %s", (run_id,))
        cursor.execute("DELETE FROM sentinel.collection_alerts WHERE run_id = %s", (run_id,))
        cursor.execute("DELETE FROM sentinel.collection_run_details WHERE run_id = %s", (run_id,))
        cursor.execute("DELETE FROM sentinel.collection_runs WHERE id = %s", (run_id,))


def test_health_and_bootstrap_starts_empty(client: httpx.Client):
    health = client.get("/api/health")
    assert health.status_code == 200
    assert health.json() == {"status": "ok", "database": "connected"}

    bootstrap = client.get("/api/bootstrap")
    assert bootstrap.status_code == 200
    data = bootstrap.json()
    assert data == {
        "hosts": [],
        "runs": [],
        "credentials": [],
        "managers": [],
        "collectionProfiles": [],
        "grafanaDashboards": [],
    }
    assert client.post("/api/collections/run").status_code == 409
    assert client.post("/api/managers/sync-all").status_code == 409


def test_internal_audit_setting_is_persisted_and_validated(client: httpx.Client):
    initial = client.get("/api/settings/internal-audit")
    assert initial.status_code == 200
    assert initial.json() == {"enabled": False}

    invalid = client.put("/api/settings/internal-audit", json={"enabled": "true"})
    assert invalid.status_code == 422
    assert invalid.json()["detail"] == "Internal audit enabled must be a boolean."

    enabled = client.put("/api/settings/internal-audit", json={"enabled": True})
    assert enabled.status_code == 200
    assert enabled.json() == {"enabled": True}
    assert client.get("/api/settings/internal-audit").json() == {"enabled": True}

    disabled = client.put("/api/settings/internal-audit", json={"enabled": False})
    assert disabled.status_code == 200
    assert disabled.json() == {"enabled": False}


def test_summary_reports_an_empty_operational_state(client: httpx.Client):
    response = client.get("/api/summary")

    assert response.status_code == 200
    summary = response.json()
    assert summary["mode"]["kind"] == "simulation"
    assert "no target or external service was contacted" in summary["mode"]["message"]
    assert set(summary) == {
        "generatedAt",
        "mode",
        "freshness",
        "inventory",
        "collections",
        "capacity",
        "alerts",
        "recentActivity",
    }
    assert summary["freshness"]["state"] == "unavailable"
    assert summary["inventory"]["total"] == 0
    assert summary["collections"]["latest"] is None
    assert summary["collections"]["outcomes"] == {
        "total": 0,
        "successful": 0,
        "unreachable": 0,
        "failed": 0,
        "queued": 0,
        "successRate": None,
        "allUnreachable": False,
    }
    assert summary["capacity"]["memory"]["totalBytes"] == 0
    assert summary["alerts"]["openCount"] == 0


def test_manual_host_crud_and_collision_rejection(client: httpx.Client):
    suffix = uuid.uuid4().hex[:8]
    _, reference = create_ssh_key(client, suffix)
    host_name = f"regression-host-{suffix}"
    host = {
        "name": host_name,
        "address": f"198.51.100.{int(suffix[:2], 16) % 200 + 20}",
        "user": "ops",
        "port": "22",
        "credential": reference,
        "role": "Regression target",
        "environment": "Operations",
        "lifecycle": "active",
    }
    created = client.post("/api/hosts", json=host)
    assert created.status_code == 201
    assert created.json()["sourceType"] == "manual"

    duplicate = client.post("/api/hosts", json={**host, "name": f"regression-copy-{suffix}"})
    assert duplicate.status_code == 409
    assert "Management address" in duplicate.json()["detail"]

    updated_name = f"regression-renamed-{suffix}"
    updated = client.put(f"/api/hosts/{host_name}", json={**host, "name": updated_name, "address": "198.51.100.250"})
    assert updated.status_code == 200
    assert updated.json()["name"] == updated_name
    assert updated.json()["ip"] == "198.51.100.250"


def test_reference_cascade_profiles_and_simulated_actions(client: httpx.Client):
    suffix = uuid.uuid4().hex[:8]
    credential_id, reference = create_ssh_key(client, suffix)
    credential = {
        "name": f"Regression SSH key {suffix}",
        "reference": reference,
        "type": "SSH key",
        "principal": "ansible",
        "scope": "Regression targets",
        "state": "active",
    }

    manager = {
        "name": f"regression-manager-{suffix}",
        "address": f"regression-{suffix}.example.test",
        "username": "admin@internal",
        "credential": reference,
        "ssl": True,
    }
    created_manager = client.post("/api/managers", json=manager)
    assert created_manager.status_code == 201
    manager_id = created_manager.json()["id"]

    blocked_profile_id = f"regression-review-pending-{suffix}"
    blocked_profile = {
        "id": blocked_profile_id,
        "name": f"Regression review-pending {suffix}",
        "description": "Profile fixture for source gating.",
        "playbook": "collections/regression.yml",
        "revision": "a" * 40,
        "scope": "Regression targets",
        "schedule": "",
        "scheduleDescription": "Manual only.",
        "credential": reference,
        "lastRun": "Not run",
        "coverage": "No targets",
        "state": "enabled",
        "source": {
            "repository": "sentinel/sentinel-playbooks",
            "path": "collections/regression.yml",
            "commitSha": "a" * 40,
            "state": "migration-pending-review",
        },
    }
    with psycopg.connect(DATABASE_URL, autocommit=True) as connection, connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO sentinel.collection_profiles (id, payload) VALUES (%s, %s)",
            (blocked_profile_id, Jsonb(blocked_profile)),
        )

    new_reference = f"secret://sentinel/inventory/regression-updated-{suffix}"
    updated_credential = client.put(
        f"/api/settings/ssh-keys/{credential_id}", json={**credential, "reference": new_reference}
    )
    assert updated_credential.status_code == 200

    data = client.get("/api/bootstrap").json()
    assert next(item for item in data["managers"] if item["id"] == manager_id)["credential"] == new_reference
    created_host = client.post(
        "/api/hosts",
        json={
            "name": f"regression-host-{suffix}",
            "address": f"198.51.100.{int(suffix[:2], 16) % 180 + 30}",
            "user": "ops",
            "port": "22",
            "credential": new_reference,
            "role": "Regression target",
            "environment": "Operations",
            "lifecycle": "active",
        },
    )
    assert created_host.status_code == 201

    assert client.post(f"/api/managers/{manager_id}/test").json()["mode"] == "simulation"
    assert client.post(f"/api/managers/{manager_id}/sync").json()["mode"] == "simulation"
    blocked_profile_run = client.post(f"/api/profiles/{blocked_profile_id}/run")
    assert blocked_profile_run.status_code == 409
    assert "awaiting Git review" in blocked_profile_run.json()["detail"]
    collection_run = client.post("/api/collections/run")
    assert collection_run.status_code == 200
    assert collection_run.json()["mode"] == "simulation"
    summary = client.get("/api/summary").json()
    queued = next(item for item in summary["recentActivity"] if item["name"] == "Simulated inventory collection")
    assert queued["state"] == "queued"
    assert queued["mode"] == "simulation"
    delete_run(queued["runId"])


def test_ssh_key_references_are_managed_only_from_settings(client: httpx.Client):
    suffix = uuid.uuid4().hex[:8]
    payload = {
        "name": f"regression-ssh-{suffix}",
        "reference": f"secret://sentinel/inventory/ssh-{suffix}",
        "type": "SSH key",
        "principal": "ansible",
        "scope": "Regression targets",
        "state": "active",
    }

    wrong_module = client.post("/api/credentials", json=payload)
    assert wrong_module.status_code == 422
    assert wrong_module.json()["detail"] == "Manage SSH key references from Settings."

    created = client.post("/api/settings/ssh-keys", json=payload)
    assert created.status_code == 201
    assert created.json()["type"] == "SSH key"


def test_legacy_secret_references_are_migrated_to_provider_neutral_uris():
    suffix = uuid.uuid4().hex[:8]
    credential_id = f"regression-legacy-reference-{suffix}"
    legacy_reference = f"kv/sentinel/inventory/legacy-{suffix}"
    with psycopg.connect(DATABASE_URL, autocommit=True) as connection, connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO sentinel.credentials (id, payload) VALUES (%s, %s)",
            (
                credential_id,
                Jsonb(
                    {
                        "id": credential_id,
                        "name": f"Regression legacy reference {suffix}",
                        "reference": legacy_reference,
                        "type": "SSH key",
                        "principal": "ansible",
                        "scope": "Regression targets",
                        "lastUsed": "Not used",
                        "state": "active",
                    }
                ),
            ),
        )

    migrate_legacy_secret_references()

    with psycopg.connect(DATABASE_URL) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT payload->>'reference' FROM sentinel.credentials WHERE id = %s", (credential_id,))
        assert cursor.fetchone()[0] == f"secret://sentinel/inventory/legacy-{suffix}"
