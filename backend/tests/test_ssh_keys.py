import json
import stat
import sys
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from sentinel.secrets import resolve_secret
from sentinel.ssh_keys import MANAGED_SSH_REFERENCE_PREFIX, generate_managed_ssh_key


def test_generated_managed_key_keeps_private_material_in_runtime_secret_storage(tmp_path: Path):
    generated = generate_managed_ssh_key(root=tmp_path)

    assert generated.reference.startswith(MANAGED_SSH_REFERENCE_PREFIX)
    assert generated.public_key.startswith("ssh-ed25519 ")
    assert generated.fingerprint.startswith("SHA256:")
    assert "PRIVATE KEY" not in repr(generated)

    path = tmp_path / "managed-ssh" / generated.reference.rsplit("/", 1)[-1]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert set(raw) == {"privateKey"}
    assert "PRIVATE KEY" in raw["privateKey"]
    assert raw["privateKey"] not in repr(generated)

    material = resolve_secret(generated.reference, root=tmp_path)
    assert material.private_key == raw["privateKey"]
    assert raw["privateKey"] not in repr(material)
