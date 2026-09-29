import sys
from pathlib import Path

import httpx
import pytest


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from sentinel.grafana import GrafanaClient, GrafanaConfigurationError, GrafanaSettings


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
            "url": "/grafana/d/sentinel-linux-facts",
        }
    ]


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

    with pytest.raises(GrafanaConfigurationError, match="service credential is unavailable"):
        GrafanaSettings.from_environment(
            {
                "GRAFANA_URL": "http://grafana:3000",
                "GRAFANA_PUBLIC_URL": "/grafana",
                "GRAFANA_API_TOKEN_FILE": str(tmp_path / "missing"),
            }
        )
