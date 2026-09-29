import json
import sys
from pathlib import Path

import httpx
import pytest


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from sentinel.olvm import OlvmClient, OlvmRequestError


def manager(secret_reference: str) -> dict:
    return {
        "id": "manager-one",
        "name": "Manager one",
        "address": "engine.example.test",
        "username": "admin@internal",
        "credential": secret_reference,
        "ssl": True,
    }


def test_olvm_client_normalizes_vm_inventory_and_uses_runtime_secret(tmp_path: Path, monkeypatch):
    secret = tmp_path / "olvm" / "manager-one"
    secret.parent.mkdir()
    secret.write_text(json.dumps({"password": "not-returned"}), encoding="utf-8")
    monkeypatch.setenv("SENTINEL_SECRET_DIRECTORY", str(tmp_path))
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.path == "/ovirt-engine/api/vms"
        assert request.url.params["follow"] == "nics.reporteddevices"
        return httpx.Response(
            200,
            json={
                "vm": [
                    {
                        "id": "vm-001",
                        "name": "web-01",
                        "status": "up",
                        "os": {"type": "rhel_9x64"},
                        "reported_devices": [{"ips": [{"address": "192.0.2.10"}, {"address": "2001:db8::10"}]}],
                    }
                ]
            },
        )

    client = OlvmClient(manager("secret://sentinel/olvm/manager-one"), httpx.Client(transport=httpx.MockTransport(handler)))
    try:
        virtual_machines = client.list_vms()
    finally:
        client.close()

    assert len(virtual_machines) == 1
    assert virtual_machines[0].resource_id == "vm-001"
    assert virtual_machines[0].addresses == ("192.0.2.10", "2001:db8::10")
    assert requests[0].headers["authorization"].startswith("Basic ")


def test_olvm_client_reports_authentication_failures_without_response_body(tmp_path: Path, monkeypatch):
    secret = tmp_path / "olvm" / "manager-one"
    secret.parent.mkdir()
    secret.write_text(json.dumps({"password": "not-returned"}), encoding="utf-8")
    monkeypatch.setenv("SENTINEL_SECRET_DIRECTORY", str(tmp_path))
    client = OlvmClient(
        manager("secret://sentinel/olvm/manager-one"),
        httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(401, text="sensitive remote detail"))),
    )

    with pytest.raises(OlvmRequestError, match="configured credential") as exc_info:
        client.test_connection()

    assert "sensitive remote detail" not in str(exc_info.value)
