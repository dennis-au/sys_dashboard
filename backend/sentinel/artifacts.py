"""Atomic filesystem storage for sanitized immutable collection artifacts."""

from __future__ import annotations

import gzip
import hashlib
import os
from pathlib import Path, PurePosixPath
import tempfile


class ArtifactStorageError(ValueError):
    """The requested artifact path escapes the controlled artifact root."""


def _relative_location(location: str) -> PurePosixPath:
    path = PurePosixPath(location)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise ArtifactStorageError("Artifact location must be a protected relative path.")
    return path


def write_sanitized_artifact(root: Path, location: str, ndjson: bytes) -> Path:
    """Write compressed NDJSON atomically without exposing a partial artifact."""

    relative = _relative_location(location)
    destination = root.joinpath(*relative.parts)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=".artifact-", delete=False) as temporary:
        temporary_path = Path(temporary.name)
        with gzip.GzipFile(fileobj=temporary, mode="wb", mtime=0) as compressed:
            compressed.write(ndjson)
    try:
        os.replace(temporary_path, destination)
    finally:
        temporary_path.unlink(missing_ok=True)
    return destination


def read_sanitized_artifact(root: Path, location: str, expected_sha256: str) -> bytes:
    """Read and checksum an immutable artifact before passing it to ingestion."""

    path = root.joinpath(*_relative_location(location).parts)
    with gzip.open(path, "rb") as artifact:
        ndjson = artifact.read()
    if hashlib.sha256(ndjson).hexdigest() != expected_sha256:
        raise ArtifactStorageError("Artifact content does not match its manifest checksum.")
    return ndjson
