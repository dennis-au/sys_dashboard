from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import StreamingResponse

from ..audit import iter_ndjson_events, read_internal_audit_settings, set_internal_audit_enabled
from ..config import INTERNAL_AUDIT_LOG_MAX_EVENTS
from ..credentials import update_credential_references
from ..grafana import GrafanaConfigurationError, GrafanaCredentialResetError, reset_administrator_password
from ..records import store_record
from ..ssh_keys import (
    SshKeyGenerationError,
    build_generated_ssh_key,
    build_ssh_key_reference,
    generate_managed_ssh_key,
    remove_managed_ssh_key,
)
from ..validation import current_record_or_404


router = APIRouter(tags=["settings"])


@router.post("/api/settings/grafana/admin-password/reset")
def reset_grafana_administrator_password(payload: dict[str, Any]) -> dict[str, str]:
    """Rotate the Grafana administrator password without persisting it in Sentinel."""

    password = payload.get("newPassword")
    confirmation = payload.get("confirmation")
    if not isinstance(password, str) or not isinstance(confirmation, str):
        raise HTTPException(status_code=422, detail="Provide and confirm a Grafana administrator password.")
    if password != confirmation:
        raise HTTPException(status_code=422, detail="Grafana administrator passwords do not match.")
    if not 16 <= len(password) <= 256:
        raise HTTPException(status_code=422, detail="Grafana administrator passwords must be between 16 and 256 characters.")
    try:
        return reset_administrator_password(password)
    except (GrafanaConfigurationError, GrafanaCredentialResetError) as exc:
        raise HTTPException(status_code=503, detail="Grafana administrator password could not be reset.") from exc


@router.get("/api/settings/internal-audit")
def get_internal_audit_settings() -> dict[str, bool]:
    return read_internal_audit_settings()


@router.put("/api/settings/internal-audit")
def update_internal_audit_settings(payload: dict[str, Any]) -> dict[str, bool]:
    enabled = payload.get("enabled")
    if not isinstance(enabled, bool):
        raise HTTPException(status_code=422, detail="Internal audit enabled must be a boolean.")
    return set_internal_audit_enabled(enabled)


@router.get("/api/settings/internal-audit/export")
def export_internal_audit_events() -> StreamingResponse:
    """Download retained diagnostic events as a redacted NDJSON attachment."""

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    filename = f"sentinel-diagnostics-{timestamp}.ndjson"
    return StreamingResponse(
        iter_ndjson_events(INTERNAL_AUDIT_LOG_MAX_EVENTS),
        media_type="application/x-ndjson",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/api/settings/ssh-keys", status_code=status.HTTP_201_CREATED)
def create_ssh_key_reference(payload: dict[str, Any]) -> dict[str, Any]:
    return store_record("credentials", build_ssh_key_reference(payload))


@router.post("/api/settings/ssh-keys/generate", status_code=status.HTTP_201_CREATED)
def generate_ssh_key(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        generated = generate_managed_ssh_key()
    except SshKeyGenerationError as exc:
        raise HTTPException(status_code=503, detail="Managed SSH key generation is unavailable.") from exc
    try:
        record = build_generated_ssh_key(payload, generated)
        return store_record("credentials", record)
    except Exception:
        remove_managed_ssh_key(generated.reference)
        raise


@router.put("/api/settings/ssh-keys/{credential_id}")
def update_ssh_key_reference(credential_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    existing = current_record_or_404("credentials", credential_id)
    record = build_ssh_key_reference(payload, existing)
    store_record("credentials", record)
    update_credential_references(existing["reference"], record["reference"])
    return record
