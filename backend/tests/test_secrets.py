import json
import sys
from pathlib import Path

import pytest


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from sentinel.secrets import SecretResolutionError, resolve_secret


def test_runtime_secret_resolution_keeps_supported_material_out_of_repr(tmp_path: Path):
    path = tmp_path / "inventory" / "linux-one"
    path.parent.mkdir()
    path.write_text(
        json.dumps({"username": "ops", "privateKey": "private-key-material"}), encoding="utf-8"
    )

    material = resolve_secret("secret://sentinel/inventory/linux-one", root=tmp_path)

    assert material.username == "ops"
    assert material.private_key == "private-key-material"
    assert "private-key-material" not in repr(material)


@pytest.mark.parametrize(
    "reference",
    ["secret://sentinel/../escape", "secret://other/inventory/linux-one"],
)
def test_runtime_secret_reference_rejects_invalid_paths(tmp_path: Path, reference: str):
    with pytest.raises(SecretResolutionError, match="invalid"):
        resolve_secret(reference, root=tmp_path)


def test_runtime_secret_rejects_unknown_or_empty_fields(tmp_path: Path):
    path = tmp_path / "service"
    path.write_text(json.dumps({"password": "", "unexpected": "value"}), encoding="utf-8")

    with pytest.raises(SecretResolutionError, match="unsupported field"):
        resolve_secret("secret://sentinel/service", root=tmp_path)
