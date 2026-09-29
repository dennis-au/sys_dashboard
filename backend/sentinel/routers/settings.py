from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import StreamingResponse

from ..audit import iter_ndjson_events, read_internal_audit_settings, set_internal_audit_enabled
from ..config import INTERNAL_AUDIT_LOG_MAX_EVENTS
from ..credentials import update_credential_references
from ..records import store_record
from ..ssh_keys import build_ssh_key_reference
from ..validation import current_record_or_404


router = APIRouter(tags=["settings"])


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


@router.put("/api/settings/ssh-keys/{credential_id}")
def update_ssh_key_reference(credential_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    existing = current_record_or_404("credentials", credential_id)
    record = build_ssh_key_reference(payload, existing)
    store_record("credentials", record)
    update_credential_references(existing["reference"], record["reference"])
    return record
