"""Settings-owned SSH key rules and managed key-pair generation."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from .credentials import build_credential
from .secrets import DEFAULT_MANAGED_SSH_KEY_DIRECTORY, DEFAULT_SECRET_DIRECTORY


SSH_KEY_TYPE = "SSH key"
MANAGED_SSH_REFERENCE_PREFIX = "secret://sentinel/managed-ssh/"


class SshKeyGenerationError(RuntimeError):
    """A managed SSH key pair could not be generated or stored safely."""


@dataclass(frozen=True)
class GeneratedSshKey:
    reference: str
    public_key: str
    fingerprint: str


def _secret_root() -> Path:
    return Path(os.environ.get("SENTINEL_SECRET_DIRECTORY", DEFAULT_SECRET_DIRECTORY))


def _managed_directory(root: Path, *, use_configured_directory: bool) -> Path:
    if use_configured_directory:
        directory = Path(
            os.environ.get("SENTINEL_MANAGED_SSH_KEY_DIRECTORY", DEFAULT_MANAGED_SSH_KEY_DIRECTORY)
        )
    else:
        directory = root / "managed-ssh"
    root_resolved = directory.resolve().parent
    directory_resolved = directory.resolve()
    if root_resolved not in directory_resolved.parents:
        raise SshKeyGenerationError("Managed SSH key storage is misconfigured.")
    return directory


def _public_key_fingerprint(public_key: str) -> str:
    parts = public_key.split()
    if len(parts) < 2 or parts[0] != "ssh-ed25519":
        raise SshKeyGenerationError("Generated SSH public key is invalid.")
    try:
        key_blob = base64.b64decode(parts[1], validate=True)
    except ValueError as exc:
        raise SshKeyGenerationError("Generated SSH public key is invalid.") from exc
    digest = hashlib.sha256(key_blob).digest()
    return f"SHA256:{base64.b64encode(digest).decode('ascii').rstrip('=')}"


def generate_managed_ssh_key(*, root: str | Path | None = None) -> GeneratedSshKey:
    """Generate an Ed25519 pair and persist only its private half at runtime."""

    secret_root = Path(root) if root is not None else _secret_root()
    directory = _managed_directory(secret_root, use_configured_directory=root is None)
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
    except OSError as exc:
        raise SshKeyGenerationError("Managed SSH key storage is unavailable.") from exc

    key_id = uuid.uuid4().hex
    destination = directory / key_id
    if destination.exists():  # pragma: no cover - UUID collision protection
        raise SshKeyGenerationError("Managed SSH key generation could not reserve storage.")
    ssh_keygen = shutil.which("ssh-keygen")
    if not ssh_keygen:
        raise SshKeyGenerationError("SSH key generation is unavailable in this deployment.")

    try:
        with tempfile.TemporaryDirectory(prefix="sentinel-ssh-", dir=directory) as raw_workspace:
            workspace = Path(raw_workspace)
            private_path = workspace / "key"
            subprocess.run(
                [ssh_keygen, "-q", "-t", "ed25519", "-N", "", "-C", f"sentinel:{key_id}", "-f", str(private_path)],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            private_key = private_path.read_text(encoding="utf-8")
            public_key = (workspace / "key.pub").read_text(encoding="utf-8").strip()
            fingerprint = _public_key_fingerprint(public_key)

            descriptor, temporary_name = tempfile.mkstemp(prefix=f".{key_id}-", dir=directory)
            temporary_path = Path(temporary_name)
            try:
                os.fchmod(descriptor, 0o600)
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    json.dump({"privateKey": private_key}, handle, separators=(",", ":"))
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary_path, destination)
            except Exception:
                temporary_path.unlink(missing_ok=True)
                raise
    except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
        destination.unlink(missing_ok=True)
        raise SshKeyGenerationError("Managed SSH key generation failed.") from exc

    try:
        if stat.S_IMODE(destination.stat().st_mode) != 0o600:
            os.chmod(destination, 0o600)
    except OSError as exc:
        destination.unlink(missing_ok=True)
        raise SshKeyGenerationError("Managed SSH key storage could not be secured.") from exc

    return GeneratedSshKey(
        reference=f"{MANAGED_SSH_REFERENCE_PREFIX}{key_id}",
        public_key=public_key,
        fingerprint=fingerprint,
    )


def remove_managed_ssh_key(reference: str, *, root: str | Path | None = None) -> None:
    """Remove a newly generated key after a metadata persistence failure."""

    if not reference.startswith(MANAGED_SSH_REFERENCE_PREFIX):
        return
    key_id = reference.removeprefix(MANAGED_SSH_REFERENCE_PREFIX)
    if not key_id or "/" in key_id or "\\" in key_id:
        return
    secret_root = Path(root) if root is not None else _secret_root()
    (_managed_directory(secret_root, use_configured_directory=root is None) / key_id).unlink(missing_ok=True)


def build_generated_ssh_key(payload: dict[str, Any], generated: GeneratedSshKey) -> dict[str, Any]:
    if payload.get("type") not in {None, SSH_KEY_TYPE}:
        raise HTTPException(status_code=422, detail="Generated keys must use credential type SSH key.")
    if payload.get("reference"):
        raise HTTPException(status_code=422, detail="Sentinel assigns the secret reference for generated SSH keys.")
    record = build_credential({**payload, "reference": generated.reference, "type": SSH_KEY_TYPE})
    return {
        **record,
        "managedBy": "sentinel",
        "keyAlgorithm": "ed25519",
        "publicKey": generated.public_key,
        "fingerprint": generated.fingerprint,
    }


def build_ssh_key_reference(
    payload: dict[str, Any], existing: dict[str, Any] | None = None
) -> dict[str, Any]:
    if payload.get("type") != SSH_KEY_TYPE:
        raise HTTPException(status_code=422, detail="Settings SSH key references must use credential type SSH key.")
    if existing is not None and existing.get("type") != SSH_KEY_TYPE:
        raise HTTPException(status_code=409, detail="Only SSH key references can be managed from Settings.")
    if existing and existing.get("managedBy") == "sentinel":
        requested_reference = payload.get("reference")
        if requested_reference not in {None, "", existing["reference"]}:
            raise HTTPException(status_code=422, detail="Sentinel-managed SSH key references cannot be changed.")
        record = build_credential(
            {**payload, "reference": existing["reference"], "type": SSH_KEY_TYPE}, existing
        )
        return {
            **record,
            "managedBy": "sentinel",
            "keyAlgorithm": existing.get("keyAlgorithm", "ed25519"),
            "publicKey": existing.get("publicKey", ""),
            "fingerprint": existing.get("fingerprint", ""),
        }
    return build_credential(payload, existing)


def reject_ssh_key_service_reference(payload: dict[str, Any], existing: dict[str, Any] | None = None) -> None:
    if payload.get("type") == SSH_KEY_TYPE or (existing is not None and existing.get("type") == SSH_KEY_TYPE):
        raise HTTPException(status_code=422, detail="Manage SSH key references from Settings.")
