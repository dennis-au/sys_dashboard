import re
from typing import Any
from uuid import uuid4

from fastapi import HTTPException

from .config import TABLES
from .records import get_record, records


def require_text(payload: dict[str, Any], field: str) -> str:
    value = str(payload.get(field, "")).strip()
    if not value:
        raise HTTPException(status_code=422, detail=f"{field.replace('_', ' ').capitalize()} is required.")
    return value


def require_reference(reference: str) -> str:
    if not reference.startswith("kv/sentinel/"):
        raise HTTPException(status_code=422, detail="OpenBao references must be under kv/sentinel/.")
    return reference


def safe_identifier(name: str, prefix: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return f"{prefix}-{normalized or uuid4().hex[:8]}"


def normalized_positive_integer(value: Any, field: str, minimum: int = 1) -> int:
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=f"{field} must be a whole number.") from exc
    if normalized < minimum:
        raise HTTPException(status_code=422, detail=f"{field} must be at least {minimum}.")
    return normalized


def current_record_or_404(kind: str, record_id: str) -> dict[str, Any]:
    record = get_record(kind, record_id)
    if not record:
        raise HTTPException(status_code=404, detail="Record not found.")
    return record


def find_case_insensitive(
    kind: str, field: str, value: str, excluded_id: str | None = None
) -> dict[str, Any] | None:
    target = value.casefold()
    for record in records(kind):
        record_id = str(record[TABLES[kind][1]])
        if record_id != excluded_id and str(record.get(field, "")).casefold() == target:
            return record
    return None


def credential_exists(reference: str) -> None:
    if not any(item["reference"] == reference for item in records("credentials")):
        raise HTTPException(status_code=422, detail="The selected OpenBao reference does not exist.")


def host_addresses(host: dict[str, Any]) -> list[str]:
    addresses = host.get("ips") or [host.get("ip", "")]
    return [str(address) for address in addresses if address]
