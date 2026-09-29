from typing import Any

from fastapi import HTTPException

from .records import records, store_record
from .validation import find_case_insensitive, migrate_legacy_reference, require_reference, require_text, safe_identifier


def build_credential(payload: dict[str, Any], existing: dict[str, Any] | None = None) -> dict[str, Any]:
    name = require_text(payload, "name")
    reference = require_reference(require_text(payload, "reference"))
    excluded_id = existing["id"] if existing else None
    if find_case_insensitive("credentials", "name", name, excluded_id) or find_case_insensitive(
        "credentials", "reference", reference, excluded_id
    ):
        raise HTTPException(status_code=409, detail="Credential names and secret references must be unique.")
    return {
        "id": existing["id"] if existing else safe_identifier(name, "credential"),
        "name": name,
        "reference": reference,
        "type": require_text(payload, "type"),
        "principal": require_text(payload, "principal"),
        "scope": require_text(payload, "scope"),
        "lastUsed": existing.get("lastUsed", "Not used") if existing else "Not used",
        "state": payload.get("state") if payload.get("state") in {"active", "disabled"} else "active",
    }


def update_credential_references(old_reference: str, new_reference: str) -> None:
    if old_reference == new_reference:
        return
    for kind in ("hosts", "managers", "profiles"):
        for record in records(kind):
            if record.get("credential") == old_reference:
                record["credential"] = new_reference
                store_record(kind, record)


def migrate_legacy_secret_references() -> None:
    """Convert retained provider-specific paths to Sentinel's opaque URI form."""

    for credential in records("credentials"):
        previous_reference = str(credential.get("reference", ""))
        migrated_reference = migrate_legacy_reference(previous_reference)
        if migrated_reference == previous_reference:
            continue
        replacement = {**credential, "reference": migrated_reference}
        store_record("credentials", replacement)
        update_credential_references(previous_reference, migrated_reference)
