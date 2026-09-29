"""Bounded read-only OLVM Engine REST adapter.

The adapter owns URL construction, runtime-only credential resolution, and
normalization of the small VM shape used by inventory reconciliation. It never
returns response bodies or credential material to callers.
"""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
from typing import Any, Iterable
from urllib.parse import urlsplit

import httpx

from .secrets import SecretResolutionError, resolve_secret


DEFAULT_TIMEOUT_SECONDS = 15.0
OLVM_API_PATH = "/ovirt-engine/api"


class OlvmError(RuntimeError):
    """An operator-safe OLVM integration failure."""


class OlvmConfigurationError(OlvmError):
    """A manager record or secret reference cannot form a safe request."""


class OlvmUnavailableError(OlvmError):
    """The configured Engine could not be reached within the request bound."""


class OlvmRequestError(OlvmError):
    """OLVM rejected or could not satisfy a read-only request."""


@dataclass(frozen=True)
class OlvmVm:
    resource_id: str
    name: str
    state: str
    addresses: tuple[str, ...]
    operating_system: str

    @property
    def address(self) -> str | None:
        return self.addresses[0] if self.addresses else None


def _base_url(address: Any, use_ssl: bool) -> str:
    if not isinstance(address, str) or not address.strip():
        raise OlvmConfigurationError("OLVM manager address is unavailable.")
    configured = address.strip()
    parsed = urlsplit(configured if "://" in configured else f"//{configured}")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise OlvmConfigurationError("OLVM manager address is invalid.")
    host = parsed.netloc if parsed.netloc else parsed.path
    if not host or "/" in host or "@" in host:
        raise OlvmConfigurationError("OLVM manager address is invalid.")
    scheme = "https" if use_ssl else "http"
    return f"{scheme}://{host}{OLVM_API_PATH}"


def _response_error(response: httpx.Response, operation: str) -> OlvmError:
    if response.status_code in {401, 403}:
        return OlvmRequestError("OLVM rejected the configured credential.")
    if response.status_code == 404:
        return OlvmRequestError(f"OLVM {operation} endpoint is unavailable.")
    return OlvmRequestError(f"OLVM {operation} failed with HTTP status {response.status_code}.")


def _text(value: Any, fallback: str = "") -> str:
    return value.strip() if isinstance(value, str) and value.strip() else fallback


def _addresses(value: Any) -> tuple[str, ...]:
    """Extract de-duplicated IP addresses from documented Engine VM fields."""

    found: list[str] = []

    def add(candidate: Any) -> None:
        if not isinstance(candidate, str):
            return
        try:
            normalized = str(ipaddress.ip_address(candidate.strip()))
        except ValueError:
            return
        if normalized not in found:
            found.append(normalized)

    def walk(node: Any, depth: int = 0) -> None:
        if depth > 8:
            return
        if isinstance(node, list):
            for item in node:
                walk(item, depth + 1)
            return
        if not isinstance(node, dict):
            return
        add(node.get("address"))
        add(node.get("ip"))
        for key in ("ips", "ip", "reported_devices", "reported_device", "nics", "nic", "guest_info"):
            if key in node:
                walk(node[key], depth + 1)

    walk(value)
    return tuple(found)


def _vm_items(payload: Any) -> Iterable[dict[str, Any]]:
    if isinstance(payload, list):
        values = payload
    elif isinstance(payload, dict):
        values = payload.get("vm") or payload.get("vms") or []
        if isinstance(values, dict):
            values = values.get("vm") or []
    else:
        raise OlvmRequestError("OLVM returned an invalid VM inventory response.")
    if not isinstance(values, list):
        raise OlvmRequestError("OLVM returned an invalid VM inventory response.")
    for value in values:
        if isinstance(value, dict):
            yield value


def _normalize_vm(value: dict[str, Any]) -> OlvmVm | None:
    resource_id = _text(value.get("id"))
    if not resource_id:
        return None
    name = _text(value.get("name"), resource_id)
    state = _text(value.get("status"), "unknown").lower()
    operating_system = "Unknown"
    os_value = value.get("os")
    if isinstance(os_value, dict):
        operating_system = _text(os_value.get("type"), operating_system)
    return OlvmVm(
        resource_id=resource_id,
        name=name,
        state=state,
        addresses=_addresses(value),
        operating_system=operating_system,
    )


class OlvmClient:
    """Synchronous OLVM client used only by API/worker source adapters."""

    def __init__(self, manager: dict[str, Any], http_client: httpx.Client | None = None) -> None:
        self.manager = manager
        try:
            material = resolve_secret(str(manager.get("credential", "")))
        except SecretResolutionError as exc:
            raise OlvmConfigurationError("OLVM manager secret is unavailable at runtime.") from exc
        username = material.username or _text(manager.get("username"))
        if not username:
            raise OlvmConfigurationError("OLVM manager username is unavailable.")
        if not material.password and not material.token:
            raise OlvmConfigurationError("OLVM manager secret needs a password or token.")
        self.base_url = _base_url(manager.get("address"), bool(manager.get("ssl", True)))
        self._owns_client = http_client is None
        self._http = http_client or httpx.Client(timeout=DEFAULT_TIMEOUT_SECONDS, verify=bool(manager.get("ssl", True)))
        self._auth = httpx.BasicAuth(username, material.password) if material.password else None
        self._headers = {"Accept": "application/json"}
        if material.token:
            self._headers["Authorization"] = f"Bearer {material.token}"

    def close(self) -> None:
        if self._owns_client:
            self._http.close()

    def _get(self, path: str, operation: str, *, params: dict[str, str] | None = None) -> httpx.Response:
        try:
            response = self._http.get(
                f"{self.base_url}{path}", headers=self._headers, auth=self._auth, params=params
            )
        except httpx.TimeoutException as exc:
            raise OlvmUnavailableError("OLVM did not respond before the connection timeout.") from exc
        except httpx.RequestError as exc:
            raise OlvmUnavailableError("OLVM could not be reached from the Sentinel runtime.") from exc
        if response.status_code < 200 or response.status_code >= 300:
            raise _response_error(response, operation)
        return response

    def test_connection(self) -> None:
        self._get("", "connection test")

    def list_vms(self) -> list[OlvmVm]:
        response = self._get(
            "/vms",
            "VM inventory request",
            params={"follow": "nics.reporteddevices", "max": "10000"},
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise OlvmRequestError("OLVM returned a non-JSON VM inventory response.") from exc
        return [vm for item in _vm_items(payload) if (vm := _normalize_vm(item)) is not None]
