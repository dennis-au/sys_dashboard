import sys
from pathlib import Path

import pytest


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from sentinel.execution import (
    CollectionExecutionError,
    _build_inventory,
    _linux_fact_records,
    _ssh_host_key_arguments,
    validate_read_only_playbook,
)
from sentinel.facts import validate_fact_record


SHA = "a" * 40


def test_read_only_playbook_accepts_fact_only_plays_and_rejects_tasks():
    validate_read_only_playbook(
        "---\n- name: Linux facts\n  hosts: all\n  gather_facts: true\n  tasks: []\n"
    )

    with pytest.raises(CollectionExecutionError, match="cannot contain tasks"):
        validate_read_only_playbook(
            "---\n- name: Unsafe\n  hosts: all\n  gather_facts: true\n  tasks:\n    - ansible.builtin.shell: id\n"
        )


def test_static_ansible_inventory_nests_host_variables_under_the_host_mapping(tmp_path: Path, monkeypatch):
    class SecretMaterial:
        username = "root"
        private_key = "private-key"
        password = None

    monkeypatch.delenv("SENTINEL_SSH_KNOWN_HOSTS_FILE", raising=False)
    monkeypatch.setattr("sentinel.execution.resolve_secret", lambda reference: SecretMaterial())
    inventory, failures = _build_inventory(
        tmp_path,
        [
            {
                "name": "linux-test-01",
                "ip": "192.0.2.10",
                "connectionPort": "22",
                "credential": "secret://sentinel/managed-ssh/test",
            }
        ],
        {},
    )

    assert failures == []
    assert inventory == {
        "all": {
            "hosts": {
                "linux-test-01": {
                    "ansible_host": "192.0.2.10",
                    "ansible_user": "root",
                    "ansible_port": 22,
                    "ansible_ssh_private_key_file": str(
                        tmp_path / "keys" / "d066b67bf7799e740c0f686565547336ab78e66d6a18c8e03161a6a8ab818fa1.key"
                    ),
                }
            }
        }
    }


def test_static_ansible_inventory_uses_an_operator_managed_known_hosts_file(tmp_path: Path, monkeypatch):
    class SecretMaterial:
        username = "root"
        private_key = None
        password = "password"

    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text("192.0.2.10 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAItest\n", encoding="utf-8")
    monkeypatch.setenv("SENTINEL_SSH_KNOWN_HOSTS_FILE", str(known_hosts))
    monkeypatch.setattr("sentinel.execution.resolve_secret", lambda reference: SecretMaterial())
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    inventory, failures = _build_inventory(
        workspace,
        [{"name": "linux-test-02", "ip": "192.0.2.10", "credential": "secret://sentinel/test"}],
        {},
    )

    assert failures == []
    assert inventory["all"]["hosts"]["linux-test-02"]["ansible_ssh_common_args"] == (
        f"-o UserKnownHostsFile={known_hosts} -o GlobalKnownHostsFile=/dev/null -o StrictHostKeyChecking=yes"
    )


def test_known_hosts_permission_problem_is_a_controlled_execution_error(tmp_path: Path, monkeypatch):
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text("192.0.2.10 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAItest\n", encoding="utf-8")
    monkeypatch.setenv("SENTINEL_SSH_KNOWN_HOSTS_FILE", str(known_hosts))
    monkeypatch.setattr(Path, "is_file", lambda _path: (_ for _ in ()).throw(PermissionError("blocked")))

    with pytest.raises(CollectionExecutionError, match="trust store is unavailable"):
        _ssh_host_key_arguments()


def test_linux_fact_normalization_emits_canonical_filesystem_and_capacity_records():
    records = _linux_fact_records(
        "run-one",
        "linux-one",
        {
            "hostname": "linux-one",
            "osFamily": "RedHat",
            "distribution": "CentOS",
            "distributionVersion": "9",
            "kernel": "5.14.0",
            "architecture": "x86_64",
            "mounts": [
                {
                    "device": "/dev/vda1",
                    "mount": "/",
                    "fstype": "ext4",
                    "size_total": 1000,
                    "size_used": 400,
                    "size_available": 600,
                }
            ],
            "cpuCores": 2,
            "memoryTotalMb": 100,
            "memoryAvailableMb": 40,
        },
        collected_at="2026-09-29T04:05:00Z",
        source_path="inventory/linux-facts.yml",
        source_commit_sha=SHA,
        sequence_start=1,
    )

    assert [record["recordType"] for record in records] == [
        "linux.system_fact.v1",
        "linux.filesystem_snapshot.v1",
        "linux.capacity_snapshot.v1",
    ]
    assert records[0]["payload"]["distribution"] == "CentOS"
    assert records[2]["payload"]["memoryUsedBytes"] == 60 * 1024 * 1024
    assert records[2]["payload"]["diskUsedBytes"] == 400
    assert validate_fact_record(records[2], expected_run_id="run-one").record_type == "linux.capacity_snapshot.v1"
