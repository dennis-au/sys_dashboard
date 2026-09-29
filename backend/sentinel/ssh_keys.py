"""Settings-owned SSH key reference rules."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from .credentials import build_credential


SSH_KEY_TYPE = "SSH key"


def build_ssh_key_reference(
    payload: dict[str, Any], existing: dict[str, Any] | None = None
) -> dict[str, Any]:
    if payload.get("type") != SSH_KEY_TYPE:
        raise HTTPException(status_code=422, detail="Settings SSH key references must use credential type SSH key.")
    if existing is not None and existing.get("type") != SSH_KEY_TYPE:
        raise HTTPException(status_code=409, detail="Only SSH key references can be managed from Settings.")
    return build_credential(payload, existing)


def reject_ssh_key_service_reference(payload: dict[str, Any], existing: dict[str, Any] | None = None) -> None:
    if payload.get("type") == SSH_KEY_TYPE or (existing is not None and existing.get("type") == SSH_KEY_TYPE):
        raise HTTPException(status_code=422, detail="Manage SSH key references from Settings.")
