"""Read-only Grafana catalog integration for Sentinel portal handoff."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import threading
from typing import Any, Mapping
from urllib.parse import quote, urlsplit

import httpx


DEFAULT_GRAFANA_URL = "http://grafana:3000"
DEFAULT_GRAFANA_PUBLIC_URL = "/grafana"
DEFAULT_GRAFANA_TOKEN_FILE = "/run/grafana-service-credentials/grafana_api_token"
DEFAULT_GRAFANA_ADMIN_PASSWORD_FILE = "/run/grafana-admin-credentials/admin_password"
MAX_DASHBOARDS = 100
_UID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_credential_reset_lock = threading.Lock()


class GrafanaConfigurationError(RuntimeError):
    """Raised when Grafana runtime configuration cannot be used safely."""


class GrafanaRequestError(RuntimeError):
    """Raised when Grafana cannot serve a safe catalog response."""


class GrafanaCredentialResetError(RuntimeError):
    """Raised when the bounded Grafana administrator reset cannot complete."""


@dataclass(frozen=True)
class GrafanaSettings:
    internal_url: str
    public_url: str
    token: str

    @classmethod
    def from_environment(cls, environment: Mapping[str, str] | None = None) -> "GrafanaSettings":
        values = os.environ if environment is None else environment
        internal_url = _absolute_url(
            values.get("GRAFANA_URL", DEFAULT_GRAFANA_URL), "Grafana URL configuration is unavailable."
        )
        public_url = _public_url(
            values.get("GRAFANA_PUBLIC_URL", DEFAULT_GRAFANA_PUBLIC_URL),
            "Grafana public URL configuration is unavailable.",
        )
        token_file = values.get("GRAFANA_API_TOKEN_FILE", DEFAULT_GRAFANA_TOKEN_FILE).strip()
        if not token_file:
            raise GrafanaConfigurationError("Grafana service credential is unavailable.")
        try:
            token = Path(token_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise GrafanaConfigurationError("Grafana service credential is unavailable.") from exc
        if not token:
            raise GrafanaConfigurationError("Grafana service credential is unavailable.")
        return cls(internal_url=internal_url, public_url=public_url, token=token)


@dataclass(frozen=True)
class GrafanaAdminSettings:
    """Runtime-only credentials for the Grafana administrator reset control."""

    internal_url: str
    username: str
    password: str
    password_file: Path

    @classmethod
    def from_environment(cls, environment: Mapping[str, str] | None = None) -> "GrafanaAdminSettings":
        values = os.environ if environment is None else environment
        internal_url = _absolute_url(
            values.get("GRAFANA_URL", DEFAULT_GRAFANA_URL), "Grafana URL configuration is unavailable."
        )
        username = values.get("GRAFANA_ADMIN_USER", "admin").strip()
        if not _USERNAME_PATTERN.fullmatch(username):
            raise GrafanaConfigurationError("Grafana administrator configuration is unavailable.")
        password_file_value = values.get("GRAFANA_ADMIN_PASSWORD_FILE", DEFAULT_GRAFANA_ADMIN_PASSWORD_FILE).strip()
        if not password_file_value:
            raise GrafanaConfigurationError("Grafana administrator configuration is unavailable.")
        password_file = Path(password_file_value)
        try:
            password = password_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise GrafanaConfigurationError("Grafana administrator configuration is unavailable.") from exc
        if not password:
            raise GrafanaConfigurationError("Grafana administrator configuration is unavailable.")
        return cls(internal_url=internal_url, username=username, password=password, password_file=password_file)


@dataclass(frozen=True)
class GrafanaDashboard:
    uid: str
    title: str
    folder: str
    tags: list[str]
    url: str

    def public_dict(self) -> dict[str, Any]:
        return {
            "uid": self.uid,
            "title": self.title,
            "folder": self.folder,
            "tags": self.tags,
            "url": self.url,
        }


class GrafanaClient:
    """Minimal Grafana API client; it never mutates dashboards or data sources."""

    def __init__(self, settings: GrafanaSettings, client: httpx.Client | None = None):
        self.settings = settings
        self._client = client or httpx.Client(base_url=settings.internal_url, timeout=5.0)
        self._owns_client = client is None

    @classmethod
    def from_environment(cls) -> "GrafanaClient":
        return cls(GrafanaSettings.from_environment())

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def readiness(self) -> dict[str, str]:
        payload = self._get_json("/api/health")
        if payload.get("database") != "ok" or not isinstance(payload.get("version"), str):
            raise GrafanaRequestError("Grafana returned an invalid health response.")
        return {"database": "ok", "version": payload["version"]}

    def dashboards(self) -> list[GrafanaDashboard]:
        payload = self._get_json("/api/search", params={"type": "dash-db", "limit": str(MAX_DASHBOARDS)})
        if not isinstance(payload, list):
            raise GrafanaRequestError("Grafana returned an invalid dashboard catalog.")
        dashboards: list[GrafanaDashboard] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            uid = item.get("uid")
            title = item.get("title")
            if not isinstance(uid, str) or not _UID_PATTERN.fullmatch(uid):
                continue
            if not isinstance(title, str) or not title.strip():
                continue
            folder = item.get("folderTitle")
            raw_tags = item.get("tags")
            tags = [tag for tag in raw_tags if isinstance(tag, str) and tag.strip()][:12] if isinstance(raw_tags, list) else []
            dashboards.append(
                GrafanaDashboard(
                    uid=uid,
                    title=title.strip(),
                    folder=folder.strip() if isinstance(folder, str) and folder.strip() else "General",
                    tags=tags,
                    url=_dashboard_url(self.settings.public_url, uid),
                )
            )
        return dashboards

    def _get_json(self, path: str, *, params: dict[str, str] | None = None) -> Any:
        try:
            response = self._client.get(path, params=params, headers={"Authorization": f"Bearer {self.settings.token}"})
        except httpx.HTTPError as exc:
            raise GrafanaRequestError("Grafana is unavailable.") from exc
        if response.status_code != 200:
            raise GrafanaRequestError("Grafana is unavailable.")
        try:
            return response.json()
        except ValueError as exc:
            raise GrafanaRequestError("Grafana returned an invalid response.") from exc


class GrafanaAdminClient:
    """Minimal write client used only to rotate Grafana's administrator password."""

    def __init__(self, settings: GrafanaAdminSettings, password: str | None = None, client: httpx.Client | None = None):
        self.settings = settings
        self._client = client or httpx.Client(
            base_url=settings.internal_url,
            timeout=5.0,
            auth=(settings.username, password if password is not None else settings.password),
        )
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def administrator_id(self) -> int:
        try:
            response = self._client.get("/api/users/lookup", params={"loginOrEmail": self.settings.username})
        except httpx.HTTPError as exc:
            raise GrafanaCredentialResetError("Grafana administrator password could not be reset.") from exc
        if response.status_code != 200:
            raise GrafanaCredentialResetError("Grafana administrator password could not be reset.")
        try:
            user_id = response.json().get("id")
        except ValueError as exc:
            raise GrafanaCredentialResetError("Grafana administrator password could not be reset.") from exc
        if not isinstance(user_id, int) or user_id < 1:
            raise GrafanaCredentialResetError("Grafana administrator password could not be reset.")
        return user_id

    def set_password(self, user_id: int, password: str) -> None:
        try:
            response = self._client.put(f"/api/admin/users/{user_id}/password", json={"password": password})
        except httpx.HTTPError as exc:
            raise GrafanaCredentialResetError("Grafana administrator password could not be reset.") from exc
        if response.status_code != 200:
            raise GrafanaCredentialResetError("Grafana administrator password could not be reset.")


def reset_administrator_password(new_password: str) -> dict[str, str]:
    """Change Grafana first, then atomically retain the runtime-only secret."""

    settings = GrafanaAdminSettings.from_environment()
    with _credential_reset_lock:
        client = GrafanaAdminClient(settings)
        try:
            user_id = client.administrator_id()
            client.set_password(user_id, new_password)
        finally:
            client.close()
        try:
            _write_runtime_password(settings.password_file, new_password)
        except OSError as exc:
            rollback = GrafanaAdminClient(settings, password=new_password)
            try:
                rollback.set_password(user_id, settings.password)
            except GrafanaCredentialResetError:
                raise GrafanaCredentialResetError("Grafana changed the password but Sentinel could not retain its runtime credential.") from exc
            finally:
                rollback.close()
            raise GrafanaCredentialResetError("Grafana administrator password could not be reset.") from exc
    return {"username": settings.username, "status": "updated"}


def catalog() -> dict[str, Any]:
    """Return redacted portal-ready status and handoff metadata."""

    try:
        client = GrafanaClient.from_environment()
    except GrafanaConfigurationError:
        return _unavailable("Grafana is starting or is not configured.")
    try:
        health = client.readiness()
        dashboards = [dashboard.public_dict() for dashboard in client.dashboards()]
    except GrafanaRequestError:
        return _unavailable("Grafana is unavailable.")
    finally:
        client.close()
    return {
        "state": "ready",
        "message": "Grafana is connected.",
        "url": _grafana_root_url(client.settings.public_url),
        "version": health["version"],
        "dashboards": dashboards,
    }


def _unavailable(message: str) -> dict[str, Any]:
    return {"state": "unavailable", "message": message, "url": None, "version": None, "dashboards": []}


def _absolute_url(raw_value: str, message: str) -> str:
    value = raw_value.strip().rstrip("/")
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise GrafanaConfigurationError(message)
    return value


def _public_url(raw_value: str, message: str) -> str:
    value = raw_value.strip().rstrip("/")
    if value.startswith("/"):
        if value.startswith("//") or "?" in value or "#" in value:
            raise GrafanaConfigurationError(message)
        return value or "/"
    return _absolute_url(value, message)


def _grafana_root_url(public_url: str) -> str:
    return public_url if public_url.endswith("/") else f"{public_url}/"


def _dashboard_url(public_url: str, uid: str) -> str:
    return f"{_grafana_root_url(public_url)}d/{quote(uid, safe='')}"


def _write_runtime_password(path: Path, password: str) -> None:
    """Replace a private volume file atomically without sending it to PostgreSQL."""

    temporary_path = path.with_name(f".{path.name}.tmp")
    descriptor = os.open(temporary_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(password)
            handle.flush()
            os.fsync(handle.fileno())
        # The volume is mounted only by API, Grafana, and bootstrap. Grafana's
        # unprivileged container user must be able to read the runtime secret.
        os.chmod(temporary_path, 0o644)
        os.replace(temporary_path, path)
    except Exception:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
