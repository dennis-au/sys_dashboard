"""Runtime-only resolution of provider-neutral Sentinel secret references.

Sentinel stores opaque ``secret://sentinel/...`` references.  The execution
boundary resolves those references from a root-owned/read-only runtime
directory mounted by the deployment.  Secret values never enter PostgreSQL,
browser payloads, audit events, or collection artifacts.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from .validation import SECRET_REFERENCE_PREFIX, require_reference


DEFAULT_SECRET_DIRECTORY = "/run/sentinel-secrets"
DEFAULT_MANAGED_SSH_KEY_DIRECTORY = "/var/lib/sentinel/managed-ssh"
MANAGED_SSH_REFERENCE_PREFIX = "secret://sentinel/managed-ssh/"
MAX_SECRET_FILE_BYTES = 64 * 1024


class SecretResolutionError(RuntimeError):
    """A runtime secret reference cannot be resolved safely."""


@dataclass(frozen=True, repr=False)
class SecretMaterial:
    """Supported runtime secret fields without exposing their values in repr."""

    username: str | None = None
    password: str | None = None
    token: str | None = None
    private_key: str | None = None

    def __repr__(self) -> str:  # pragma: no cover - defensive logging guard
        return "SecretMaterial(<redacted>)"


def _secret_path(reference: str, root: Path, *, managed_root: Path | None = None) -> Path:
    try:
        normalized = require_reference(reference)
    except HTTPException as exc:
        raise SecretResolutionError("Secret reference is invalid.") from exc
    relative = normalized.removeprefix(SECRET_REFERENCE_PREFIX)
    if normalized.startswith(MANAGED_SSH_REFERENCE_PREFIX):
        key_id = normalized.removeprefix(MANAGED_SSH_REFERENCE_PREFIX)
        if not key_id or "/" in key_id or "\\" in key_id:
            raise SecretResolutionError("Managed SSH key reference is invalid.")
        root = managed_root or Path(
            os.environ.get("SENTINEL_MANAGED_SSH_KEY_DIRECTORY", DEFAULT_MANAGED_SSH_KEY_DIRECTORY)
        )
        relative = key_id
    path = (root / relative).resolve()
    root_resolved = root.resolve()
    if path != root_resolved and root_resolved not in path.parents:
        raise SecretResolutionError("Secret reference resolves outside the configured secret directory.")
    return path


def _decode_secret(raw: bytes, path: Path) -> SecretMaterial:
    if len(raw) > MAX_SECRET_FILE_BYTES:
        raise SecretResolutionError("The configured secret material exceeds the supported size limit.")
    try:
        value: Any = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SecretResolutionError("Secret material must be a UTF-8 JSON object.") from exc
    if not isinstance(value, dict):
        raise SecretResolutionError("Secret material must be a JSON object.")
    allowed = {"username", "password", "token", "privateKey", "private_key"}
    unknown = [str(key) for key in value if str(key) not in allowed]
    if unknown:
        raise SecretResolutionError("Secret material contains an unsupported field.")

    def text(key: str) -> str | None:
        item = value.get(key)
        if item is None:
            return None
        if not isinstance(item, str) or not item:
            raise SecretResolutionError(f"Secret material field {key} must be a non-empty string.")
        return item

    return SecretMaterial(
        username=text("username"),
        password=text("password"),
        token=text("token"),
        private_key=text("privateKey") or text("private_key"),
    )


def resolve_secret(reference: str, *, root: str | Path | None = None) -> SecretMaterial:
    directory = Path(root or os.environ.get("SENTINEL_SECRET_DIRECTORY", DEFAULT_SECRET_DIRECTORY))
    path = _secret_path(
        reference,
        directory,
        managed_root=directory / "managed-ssh" if root is not None else None,
    )
    try:
        raw = path.read_bytes()
    except FileNotFoundError as exc:
        raise SecretResolutionError("The configured secret reference is unavailable at runtime.") from exc
    except OSError as exc:
        raise SecretResolutionError("The configured secret reference could not be read.") from exc
    return _decode_secret(raw, path)
