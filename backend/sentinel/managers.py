from typing import Any

from fastapi import HTTPException

from .validation import credential_exists, find_case_insensitive, require_reference, require_text, safe_identifier


def build_manager(payload: dict[str, Any], existing: dict[str, Any] | None = None) -> dict[str, Any]:
    name = require_text(payload, "name")
    duplicate = find_case_insensitive("managers", "name", name, existing["id"] if existing else None)
    if duplicate:
        raise HTTPException(status_code=409, detail="A manager with this name already exists.")
    credential = require_reference(require_text(payload, "credential"))
    credential_exists(credential)
    return {
        "id": existing["id"] if existing else safe_identifier(name, "manager"),
        "name": name,
        "address": require_text(payload, "address"),
        "username": require_text(payload, "username"),
        "credential": credential,
        "ssl": bool(payload.get("ssl", False)),
        "state": existing.get("state", "ready") if existing else "ready",
        "lastSync": existing.get("lastSync", "Not synced") if existing else "Not synced",
        "inventory": existing.get("inventory", "No inventory yet") if existing else "No inventory yet",
    }
