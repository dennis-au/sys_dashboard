import os
import uuid

import psycopg
import pytest
from fastapi import HTTPException
from psycopg.types.json import Jsonb

from main import (
    build_authoritative_olvm_host,
    build_credential,
    build_host,
    build_manager,
    delete_record,
    get_record,
    migrate_olvm_provenance,
    record_olvm_resource_missing,
    reconcile_authoritative_olvm_host,
    store_record,
)


def olvm_resource(suffix: str, manager_id: str, **overrides: object) -> dict[str, object]:
    return {
        "name": f"olvm-resource-{suffix}",
        "address": f"198.51.100.{int(suffix[:2], 16) % 180 + 30}",
        "role": "Regression VM",
        "environment": "Operations",
        "sourceManagerId": manager_id,
        "engineResourceType": "vm",
        "engineResourceId": f"regression-engine-{suffix}",
        "lastAuthoritativeRunId": f"regression-run-{suffix}-1",
        "pruneAfterMissingRuns": 3,
        **overrides,
    }


@pytest.fixture
def owned_hosts():
    names: list[str] = []

    def remember(host: dict[str, object]) -> dict[str, object]:
        names.append(str(host["name"]))
        return host

    yield remember
    for name in names:
        delete_record("hosts", name)


@pytest.fixture
def configured_olvm_manager():
    suffix = uuid.uuid4().hex[:8]
    credential = store_record(
        "credentials",
        build_credential(
            {
                "name": f"Regression OLVM credential {suffix}",
                "reference": f"secret://sentinel/inventory/regression-olvm-{suffix}",
                "type": "OLVM API credential",
                "principal": "admin@internal",
                "scope": "Regression OLVM manager",
                "state": "active",
            }
        ),
    )
    manager = store_record(
        "managers",
        build_manager(
            {
                "name": f"regression-olvm-{suffix}",
                "address": f"regression-olvm-{suffix}.example.test",
                "username": "admin@internal",
                "credential": credential["reference"],
                "ssl": True,
            }
        ),
    )
    yield {"id": manager["id"], "credential": credential["reference"]}
    delete_record("managers", manager["id"])
    delete_record("credentials", credential["id"])


def test_authoritative_olvm_host_has_manager_and_engine_identity(configured_olvm_manager, owned_hosts):
    suffix = uuid.uuid4().hex[:8]
    host = owned_hosts(
        reconcile_authoritative_olvm_host(olvm_resource(suffix, configured_olvm_manager["id"]))
    )

    assert host["sourceType"] == "olvm"
    assert host["sourceManagerId"] == configured_olvm_manager["id"]
    assert host["engineResourceType"] == "vm"
    assert host["engineResourceId"]
    assert host["lastAuthoritativeRunId"]
    assert host["reconciliationState"] == "present"


def test_partial_legacy_provenance_is_actionably_unresolved_not_guessed():
    name = f"partial-provenance-{uuid.uuid4().hex[:8]}"
    payload = {
        "name": name,
        "ip": "198.51.100.29",
        "ips": ["198.51.100.29"],
        "role": "Legacy VM",
        "environment": "Operations",
        "sourceType": "olvm",
        "sourceManagerId": "manager-no-longer-configured",
        "engineResourceId": "partial-identity-only",
    }
    try:
        with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as connection, connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO sentinel.hosts (id, payload) VALUES (%s, %s)",
                (name, Jsonb(payload)),
            )
        migrate_olvm_provenance()

        unresolved = get_record("hosts", name)
        assert unresolved["provenanceState"] == "legacy-unresolved"
        assert unresolved["reconciliationState"] == "unresolved"
        assert "sourceManagerId" not in unresolved
        assert "engineResourceId" not in unresolved
    finally:
        delete_record("hosts", name)


def test_olvm_resource_rename_and_address_change_keep_immutable_identity(configured_olvm_manager, owned_hosts):
    suffix = uuid.uuid4().hex[:8]
    created = owned_hosts(reconcile_authoritative_olvm_host(olvm_resource(suffix, configured_olvm_manager["id"])))

    renamed = reconcile_authoritative_olvm_host(
        olvm_resource(
            suffix,
            configured_olvm_manager["id"],
            name=f"olvm-renamed-{suffix}",
            address="203.0.113.44",
            lastAuthoritativeRunId=f"regression-run-{suffix}-2",
        )
    )
    owned_hosts(renamed)

    assert get_record("hosts", str(created["name"])) is None
    assert renamed["name"] == f"olvm-renamed-{suffix}"
    assert renamed["ip"] == "203.0.113.44"
    assert renamed["engineResourceId"] == f"regression-engine-{suffix}"
    assert renamed["lastAuthoritativeRunId"] == f"regression-run-{suffix}-2"


def test_olvm_duplicate_display_name_and_identity_are_rejected(configured_olvm_manager, owned_hosts):
    suffix = uuid.uuid4().hex[:8]
    created = owned_hosts(reconcile_authoritative_olvm_host(olvm_resource(suffix, configured_olvm_manager["id"])))

    with pytest.raises(HTTPException) as duplicate_name:
        reconcile_authoritative_olvm_host(
            olvm_resource(suffix, configured_olvm_manager["id"], engineResourceId=f"regression-other-{suffix}", address="203.0.113.45")
        )
    assert duplicate_name.value.status_code == 409

    with pytest.raises(HTTPException) as duplicate_identity:
        store_record(
            "hosts",
            build_authoritative_olvm_host(
                olvm_resource(suffix, configured_olvm_manager["id"], name=f"olvm-copy-{suffix}", address="203.0.113.46")
            ),
        )
    assert duplicate_identity.value.status_code == 409

    with pytest.raises(HTTPException) as immutable_identity:
        store_record(
            "hosts",
            {**created, "engineResourceId": f"regression-mutated-{suffix}"},
        )
    assert immutable_identity.value.status_code == 409
    assert get_record("hosts", str(created["name"])) is not None


def test_olvm_reconciliation_never_overwrites_manual_host(configured_olvm_manager, owned_hosts):
    suffix = uuid.uuid4().hex[:8]
    manual = owned_hosts(
        store_record(
            "hosts",
            build_host(
                {
                    "name": f"manual-collision-{suffix}",
                    "address": "203.0.113.47",
                    "user": "ops",
                    "port": "22",
                    "credential": configured_olvm_manager["credential"],
                    "role": "Manual regression target",
                    "environment": "Operations",
                    "lifecycle": "active",
                }
            ),
        )
    )

    with pytest.raises(HTTPException) as collision:
        reconcile_authoritative_olvm_host(
            olvm_resource(suffix, configured_olvm_manager["id"], name=str(manual["name"]), address="203.0.113.48")
        )
    assert collision.value.status_code == 409
    assert get_record("hosts", str(manual["name"]))["sourceType"] == "manual"


def test_olvm_pruning_requires_missing_run_threshold_and_explicit_request(configured_olvm_manager, owned_hosts):
    suffix = uuid.uuid4().hex[:8]
    host = owned_hosts(reconcile_authoritative_olvm_host(olvm_resource(suffix, configured_olvm_manager["id"])))

    first = record_olvm_resource_missing(
        str(host["sourceManagerId"]), "vm", str(host["engineResourceId"]), f"regression-run-{suffix}-2"
    )
    second = record_olvm_resource_missing(
        str(host["sourceManagerId"]), "vm", str(host["engineResourceId"]), f"regression-run-{suffix}-3"
    )
    threshold = record_olvm_resource_missing(
        str(host["sourceManagerId"]), "vm", str(host["engineResourceId"]), f"regression-run-{suffix}-4"
    )
    requested = record_olvm_resource_missing(
        str(host["sourceManagerId"]),
        "vm",
        str(host["engineResourceId"]),
        f"regression-run-{suffix}-5",
        request_prune=True,
    )

    assert (first["reconciliationState"], first["pruneEligible"]) == ("missing", False)
    assert (second["reconciliationState"], second["pruneEligible"]) == ("missing", False)
    assert (threshold["reconciliationState"], threshold["pruneEligible"]) == ("retained", False)
    assert (requested["reconciliationState"], requested["pruneEligible"]) == ("pending-prune", True)
    assert requested["lifecycle"] == "active"
