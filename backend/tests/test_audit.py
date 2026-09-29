import os
import sys
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from sentinel import audit
from sentinel.app import app


DATABASE_URL = os.environ["DATABASE_URL"]
MISSING_ROUTE = "/api/audit-regression-missing"
UNHANDLED_ROUTE = "/api/audit-regression-unhandled"


@app.get(UNHANDLED_ROUTE)
def audit_regression_unhandled_route():
    raise RuntimeError("audit regression password=must-not-leak")


def clear_audit_events() -> None:
    with psycopg.connect(DATABASE_URL, autocommit=True) as connection, connection.cursor() as cursor:
        cursor.execute(
            "DELETE FROM sentinel.internal_audit_events WHERE service = 'audit-test' OR route IN (%s, %s)",
            (MISSING_ROUTE, UNHANDLED_ROUTE),
        )


@pytest.fixture(autouse=True)
def audit_mode():
    clear_audit_events()
    audit.set_internal_audit_enabled(True)
    yield
    audit.set_internal_audit_enabled(False)
    clear_audit_events()


def test_internal_audit_redacts_secret_like_values_and_deduplicates_occurrences():
    audit.record_event(
        service="audit-test",
        event_kind="redaction_regression",
        message="password=unacceptable bearer token-value postgresql://sentinel:database-secret@postgres/sentinel_db",
        context={"token": "token-value", "nested": {"apiKey": "api-key-value"}},
    )
    audit.record_event(
        service="audit-test",
        event_kind="redaction_regression",
        message="password=unacceptable bearer token-value postgresql://sentinel:database-secret@postgres/sentinel_db",
        context={"token": "token-value", "nested": {"apiKey": "api-key-value"}},
    )

    event = next(item for item in audit.export_events() if item["service"] == "audit-test")
    serialized = audit.ndjson_events()

    assert event["occurrence_count"] == 2
    assert event["context"] == {"token": "[redacted]", "nested": {"apiKey": "[redacted]"}}
    for secret in ("unacceptable", "token-value", "database-secret", "api-key-value"):
        assert secret not in serialized
    assert "[redacted]" in serialized


def test_internal_audit_captures_http_errors_and_returns_a_correlation_id():
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get(MISSING_ROUTE)

    assert response.status_code == 404
    request_id = response.headers["X-Sentinel-Audit-Request-Id"]
    event = next(item for item in audit.export_events() if item["route"] == MISSING_ROUTE)

    assert event["event_kind"] == "http_exception"
    assert event["status_code"] == 404
    assert event["request_id"] == request_id


def test_internal_audit_captures_unhandled_errors_once_without_leaking_detail():
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get(UNHANDLED_ROUTE)

    assert response.status_code == 500
    assert response.headers["X-Sentinel-Audit-Request-Id"]
    event = next(item for item in audit.export_events() if item["route"] == UNHANDLED_ROUTE)

    assert event["event_kind"] == "unhandled_exception"
    assert event["occurrence_count"] == 1
    assert "must-not-leak" not in audit.ndjson_events()
    assert "[redacted]" in event["message"]


def test_internal_audit_export_download_is_redacted_ndjson_attachment():
    audit.record_event(
        service="audit-test",
        event_kind="export_regression",
        message="token=export-secret",
    )

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/api/settings/internal-audit/export")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-ndjson")
    assert "attachment; filename=\"sentinel-diagnostics-" in response.headers["content-disposition"]
    assert response.text.endswith("\n")
    assert "export-secret" not in response.text
    assert '"event_kind":"export_regression"' in response.text
