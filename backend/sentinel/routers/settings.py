from typing import Any

from fastapi import APIRouter, status

from ..credentials import update_credential_references
from ..records import store_record
from ..ssh_keys import build_ssh_key_reference
from ..validation import current_record_or_404


router = APIRouter(tags=["settings"])


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
