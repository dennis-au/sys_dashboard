from typing import Any

from fastapi import APIRouter, status

from ..credentials import build_credential, update_credential_references
from ..records import store_record
from ..ssh_keys import reject_ssh_key_service_reference
from ..validation import current_record_or_404


router = APIRouter(tags=["credentials"])


@router.post("/api/credentials", status_code=status.HTTP_201_CREATED)
def create_credential(payload: dict[str, Any]) -> dict[str, Any]:
    reject_ssh_key_service_reference(payload)
    return store_record("credentials", build_credential(payload))


@router.put("/api/credentials/{credential_id}")
def update_credential(credential_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    existing = current_record_or_404("credentials", credential_id)
    reject_ssh_key_service_reference(payload, existing)
    record = build_credential(payload, existing)
    store_record("credentials", record)
    update_credential_references(existing["reference"], record["reference"])
    return record
