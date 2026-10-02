import sys
from pathlib import Path
import json

import httpx
import pytest


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from sentinel.grafana import (
    GrafanaAdminClient,
    GrafanaAdminSettings,
    GrafanaClient,
    GrafanaConfigurationError,
    GrafanaSettings,
    reset_administrator_password,
)


def settings_for(tmp_path: Path) -> GrafanaSettings:
    token_file = tmp_path / "grafana_api_token"
    token_file.write_text("viewer-token", encoding="utf-8")
    return GrafanaSettings.from_environment(
        {
            "GRAFANA_URL": "http://grafana:3000",
            "GRAFANA_PUBLIC_URL": "/grafana",
            "GRAFANA_API_TOKEN_FILE": str(token_file),
        }
    )


def test_grafana_client_returns_a_safe_read_only_catalog(tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer viewer-token"
        if request.url.path == "/api/health":
            return httpx.Response(200, json={"database": "ok", "version": "11.5.2"})
        if request.url.path == "/api/search":
            assert request.url.params == httpx.QueryParams({"type": "dash-db", "limit": "100"})
            return httpx.Response(
                200,
                json=[
                    {
                        "uid": "sentinel-linux-facts",
                        "title": "Sentinel Linux facts",
                        "folderTitle": "Sentinel",
                        "tags": ["sentinel", "linux"],
                        "type": "dash-db",
                        "url": "/grafana/d/sentinel-linux-facts/sentinel-linux-facts",
                    },
                    {"uid": "not a valid uid", "title": "Ignored"},
                ],
            )
        raise AssertionError(f"Unexpected Grafana request: {request.method} {request.url}")

    client = GrafanaClient(
        settings_for(tmp_path),
        httpx.Client(base_url="http://grafana:3000", transport=httpx.MockTransport(handler)),
    )
    assert client.readiness() == {"database": "ok", "version": "11.5.2"}

    dashboards = client.dashboards()

    assert [dashboard.public_dict() for dashboard in dashboards] == [
        {
            "uid": "sentinel-linux-facts",
            "title": "Sentinel Linux facts",
            "folder": "Sentinel",
            "tags": ["sentinel", "linux"],
            "url": "/grafana/d/sentinel-linux-facts/sentinel-linux-facts",
        }
    ]


def test_grafana_client_rejects_an_unsafe_catalog_dashboard_url(tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/search":
            return httpx.Response(
                200,
                json=[
                    {
                        "uid": "sentinel-linux-facts",
                        "title": "Sentinel Linux facts",
                        "url": "https://untrusted.example/d/sentinel-linux-facts/sentinel-linux-facts",
                    }
                ],
            )
        raise AssertionError(f"Unexpected Grafana request: {request.method} {request.url}")

    client = GrafanaClient(
        settings_for(tmp_path),
        httpx.Client(base_url="http://grafana:3000", transport=httpx.MockTransport(handler)),
    )

    assert client.dashboards()[0].url == "/grafana/d/sentinel-linux-facts"


def test_grafana_settings_reject_unsafe_or_unavailable_configuration(tmp_path: Path):
    token_file = tmp_path / "grafana_api_token"
    token_file.write_text("viewer-token", encoding="utf-8")

    with pytest.raises(GrafanaConfigurationError, match="public URL configuration"):
        GrafanaSettings.from_environment(
            {
                "GRAFANA_URL": "http://grafana:3000",
                "GRAFANA_PUBLIC_URL": "//untrusted.example",
                "GRAFANA_API_TOKEN_FILE": str(token_file),
            }
        )


def test_grafana_administrator_client_uses_basic_auth_only_for_password_rotation(tmp_path: Path):
    password_file = tmp_path / "grafana_admin_password"
    password_file.write_text("current-admin-password", encoding="utf-8")
    settings = GrafanaAdminSettings.from_environment(
        {
            "GRAFANA_URL": "http://grafana:3000",
            "GRAFANA_ADMIN_USER": "admin",
            "GRAFANA_ADMIN_PASSWORD_FILE": str(password_file),
        }
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"].startswith("Basic ")
        if request.method == "GET" and request.url.path == "/api/users/lookup":
            assert request.url.params == httpx.QueryParams({"loginOrEmail": "admin"})
            return httpx.Response(200, json={"id": 1, "login": "admin"})
        if request.method == "PUT" and request.url.path == "/api/admin/users/1/password":
            assert json.loads(request.content) == {"password": "replacement-admin-password"}
            return httpx.Response(200, json={"message": "User password updated"})
        raise AssertionError(f"Unexpected Grafana request: {request.method} {request.url}")

    client = GrafanaAdminClient(
        settings,
        client=httpx.Client(base_url="http://grafana:3000", auth=("admin", "current-admin-password"), transport=httpx.MockTransport(handler)),
    )
    assert client.administrator_id() == 1
    client.set_password(1, "replacement-admin-password")


def test_grafana_administrator_reset_retains_only_the_replacement_runtime_secret(tmp_path: Path, monkeypatch):
    import sentinel.grafana as grafana

    password_file = tmp_path / "grafana_admin_password"
    password_file.write_text("current-admin-password", encoding="utf-8")
    calls: list[tuple[str, str | None]] = []

    class FakeAdminClient:
        def __init__(self, settings, password=None):
            calls.append(("create", password))

        def administrator_id(self):
            return 1

        def set_password(self, user_id, password):
            assert user_id == 1
            calls.append(("set", password))

        def close(self):
            return None

    monkeypatch.setattr(grafana, "GrafanaAdminClient", FakeAdminClient)
    monkeypatch.setenv("GRAFANA_URL", "http://grafana:3000")
    monkeypatch.setenv("GRAFANA_ADMIN_USER", "admin")
    monkeypatch.setenv("GRAFANA_ADMIN_PASSWORD_FILE", str(password_file))

    assert reset_administrator_password("replacement-admin-password") == {"username": "admin", "status": "updated"}
    assert password_file.read_text(encoding="utf-8") == "replacement-admin-password"
    assert calls == [("create", None), ("set", "replacement-admin-password")]

    with pytest.raises(GrafanaConfigurationError, match="service credential is unavailable"):
        GrafanaSettings.from_environment(
            {
                "GRAFANA_URL": "http://grafana:3000",
                "GRAFANA_PUBLIC_URL": "/grafana",
                "GRAFANA_API_TOKEN_FILE": str(tmp_path / "missing"),
            }
        )
