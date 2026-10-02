"""Read-only Ansible collection execution and sanitized artifact construction."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
from typing import Any, Callable, Iterable

from forgejo_client import ForgejoClient
from yaml import YAMLError, safe_load

from .artifacts import write_sanitized_artifact
from .facts import CanonicalFact, deterministic_record_key, validate_fact_record
from .ingestion import ARTIFACT_SCHEMA_VERSION, ingest_ndjson_artifact
from .playbooks import read_profile_playbook, require_runnable_source
from .records import get_record, store_record
from .reporting import complete_live_collection, mark_collection_running, record_host_outcomes
from .secrets import SecretResolutionError, resolve_secret


DEFAULT_ARTIFACT_DIRECTORY = "/var/lib/sentinel/artifacts"
DEFAULT_TIMEOUT_SECONDS = 300
_FACT_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,255}$")


class CollectionExecutionError(RuntimeError):
    """A safe-to-display collection execution failure."""


@dataclass(frozen=True)
class PreparedArtifact:
    manifest: dict[str, Any]
    ndjson: bytes
    facts: tuple[CanonicalFact, ...]


def _timestamp(value: datetime | None) -> str:
    timestamp = (value or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return timestamp.isoformat(timespec="seconds").replace("+00:00", "Z")


def prepare_sanitized_artifact(
    *,
    run_id: str,
    records: Iterable[dict[str, Any]],
    source_instances: Iterable[dict[str, str]],
    collector_version: str,
    location: str,
    profile_id: str | None = None,
    source_path: str | None = None,
    source_commit_sha: str | None = None,
    execution_status: str = "completed",
    collection_started_at: datetime | None = None,
    collection_completed_at: datetime | None = None,
) -> PreparedArtifact:
    """Create the only artifact shape accepted by Sentinel ingestion."""

    raw_records = list(records)
    fact_list = tuple(validate_fact_record(record, expected_run_id=run_id) for record in raw_records)
    lines = [json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True) for record in raw_records]
    ndjson = ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8")
    artifact_sha256 = hashlib.sha256(ndjson).hexdigest()
    started_at = _timestamp(collection_started_at)
    completed_at = _timestamp(collection_completed_at or collection_started_at)
    manifest = {
        "schemaVersion": ARTIFACT_SCHEMA_VERSION,
        "runId": run_id,
        "profileId": profile_id,
        "source": {"path": source_path, "commitSha": source_commit_sha},
        "collectorVersion": collector_version,
        "collectionStartedAt": started_at,
        "collectionCompletedAt": completed_at,
        "sourceInstances": list(source_instances),
        "executionStatus": execution_status,
        "artifact": {
            "location": location,
            "schemaVersion": 1,
            "sha256": artifact_sha256,
            "recordCount": len(fact_list),
        },
    }
    return PreparedArtifact(manifest=manifest, ndjson=ndjson, facts=fact_list)


def _timeout_seconds() -> int:
    raw = os.environ.get("SENTINEL_ANSIBLE_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise CollectionExecutionError("Sentinel Ansible timeout configuration is invalid.") from exc
    if not 30 <= value <= 3600:
        raise CollectionExecutionError("Sentinel Ansible timeout configuration is invalid.")
    return value


def _artifact_root() -> Path:
    configured = os.environ.get("SENTINEL_ARTIFACT_DIRECTORY", DEFAULT_ARTIFACT_DIRECTORY).strip()
    if not configured:
        raise CollectionExecutionError("Sentinel artifact directory configuration is unavailable.")
    return Path(configured)


def validate_read_only_playbook(source: str) -> None:
    """Reject source constructs outside Sentinel's controlled fact-gathering subset."""

    try:
        document = safe_load(source)
    except YAMLError as exc:
        raise CollectionExecutionError("The selected playbook is not valid YAML.") from exc
    plays = document if isinstance(document, list) else [document]
    if not plays or not all(isinstance(play, dict) for play in plays):
        raise CollectionExecutionError("A live collection playbook must contain one or more plays.")
    allowed_keys = {"name", "hosts", "gather_facts", "gather_subset", "tasks"}
    for play in plays:
        if set(play) - allowed_keys:
            raise CollectionExecutionError("The selected playbook contains unsupported execution controls.")
        if play.get("gather_facts") is not True:
            raise CollectionExecutionError("Each live collection play must set gather_facts: true.")
        if not isinstance(play.get("hosts"), str) or not play["hosts"].strip():
            raise CollectionExecutionError("Each live collection play must declare a target host pattern.")
        tasks = play.get("tasks", [])
        if tasks not in (None, []):
            raise CollectionExecutionError("Live collection playbooks cannot contain tasks; Sentinel gathers Ansible facts only.")


_CALLBACK_PLUGIN = r'''
from __future__ import annotations

import json
import os

from ansible.plugins.callback import CallbackBase


class CallbackModule(CallbackBase):
    CALLBACK_VERSION = 2.0
    CALLBACK_TYPE = "aggregate"
    CALLBACK_NAME = "sentinel_facts"
    CALLBACK_NEEDS_WHITELIST = False

    def _write(self, value):
        path = os.environ.get("SENTINEL_FACT_EVENTS")
        if not path:
            return
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")

    def v2_runner_on_ok(self, result, **kwargs):
        facts = result._result.get("ansible_facts")
        if not isinstance(facts, dict):
            return
        self._write({
            "host": result._host.get_name(),
            "state": "success",
            "facts": {
                "hostname": facts.get("ansible_hostname", facts.get("hostname")),
                "osFamily": facts.get("ansible_os_family", facts.get("os_family")),
                "distribution": facts.get("ansible_distribution", facts.get("distribution")),
                "distributionVersion": facts.get("ansible_distribution_version", facts.get("distribution_version")),
                "kernel": facts.get("ansible_kernel", facts.get("kernel")),
                "architecture": facts.get("ansible_architecture", facts.get("architecture")),
                "mounts": facts.get("ansible_mounts", facts.get("mounts", [])),
                "cpuCores": facts.get("ansible_processor_vcpus", facts.get("processor_vcpus")),
                "memoryTotalMb": facts.get("ansible_memtotal_mb", facts.get("memtotal_mb")),
                "memoryAvailableMb": facts.get("ansible_memavailable_mb", facts.get("memavailable_mb", facts.get("ansible_memfree_mb", facts.get("memfree_mb")))),
            },
        })

    def v2_runner_on_unreachable(self, result, **kwargs):
        self._write({"host": result._host.get_name(), "state": "unreachable"})

    def v2_runner_on_failed(self, result, **kwargs):
        self._write({"host": result._host.get_name(), "state": "failed"})
'''


def _write_private(path: Path, content: str | bytes) -> None:
    if isinstance(content, str):
        path.write_text(content, encoding="utf-8")
    else:
        path.write_bytes(content)
    path.chmod(0o600)


def _fact_identifier(value: str) -> bool:
    return bool(_FACT_IDENTIFIER.fullmatch(value))


def _host_credentials(host: dict[str, Any], profile: dict[str, Any]) -> tuple[str, Any]:
    reference = host.get("credential") or profile.get("credential")
    if not isinstance(reference, str) or not reference:
        raise CollectionExecutionError("The target does not have an approved SSH secret reference.")
    try:
        material = resolve_secret(reference)
    except SecretResolutionError as exc:
        raise CollectionExecutionError("The target SSH secret is unavailable at runtime.") from exc
    username = material.username or host.get("connectionUser")
    if not isinstance(username, str) or not username.strip():
        raise CollectionExecutionError("The target SSH username is unavailable.")
    if not material.private_key and not material.password:
        raise CollectionExecutionError("The target SSH secret needs a private key or password.")
    return username.strip(), material


def _ssh_host_key_arguments() -> str | None:
    """Return strict host-key arguments for an operator-managed trust store."""

    configured = os.environ.get("SENTINEL_SSH_KNOWN_HOSTS_FILE", "").strip()
    if not configured:
        return None
    path = Path(configured)
    try:
        available = path.is_file()
    except OSError as exc:
        raise CollectionExecutionError("The SSH host key trust store is unavailable.") from exc
    if not available:
        raise CollectionExecutionError("The SSH host key trust store is unavailable.")
    escaped_path = shlex.quote(str(path))
    return (
        f"-o UserKnownHostsFile={escaped_path} "
        "-o GlobalKnownHostsFile=/dev/null -o StrictHostKeyChecking=yes"
    )


def _build_inventory(
    workspace: Path, hosts: list[dict[str, Any]], profile: dict[str, Any]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    hostvars: dict[str, dict[str, Any]] = {}
    failures: list[dict[str, Any]] = []
    host_key_arguments = _ssh_host_key_arguments()
    keys_directory = workspace / "keys"
    keys_directory.mkdir(mode=0o700)
    for host in hosts:
        host_id = str(host.get("id") or host.get("name") or "").strip()
        host_name = str(host.get("name") or host_id).strip()
        if not _fact_identifier(host_id):
            failures.append({"host_id": host_id or "unknown", "host_name": host_name or "unknown", "state": "failed", "reason": "Host identity is unsupported by the canonical fact contract."})
            continue
        address = host.get("ip") or host.get("address")
        if not isinstance(address, str) or not address.strip():
            failures.append({"host_id": host_id, "host_name": host_name, "state": "failed", "reason": "Target management address is unavailable."})
            continue
        try:
            username, material = _host_credentials(host, profile)
        except CollectionExecutionError as exc:
            failures.append({"host_id": host_id, "host_name": host_name, "state": "failed", "reason": str(exc)})
            continue
        try:
            port = int(str(host.get("connectionPort") or "22"))
        except ValueError:
            failures.append({"host_id": host_id, "host_name": host_name, "state": "failed", "reason": "Target SSH port is invalid."})
            continue
        if not 1 <= port <= 65535:
            failures.append({"host_id": host_id, "host_name": host_name, "state": "failed", "reason": "Target SSH port is invalid."})
            continue
        variables: dict[str, Any] = {"ansible_host": address.strip(), "ansible_user": username, "ansible_port": port}
        if host_key_arguments is not None:
            variables["ansible_ssh_common_args"] = host_key_arguments
        if material.private_key:
            key_path = keys_directory / f"{hashlib.sha256(host_id.encode('utf-8')).hexdigest()}.key"
            _write_private(key_path, material.private_key)
            variables["ansible_ssh_private_key_file"] = str(key_path)
        else:
            variables["ansible_password"] = material.password
        hostvars[host_id] = variables
    # This file is consumed as a static YAML/JSON inventory, not as the output
    # of an executable dynamic-inventory script. Ansible therefore requires
    # the group hosts to be a mapping, with per-host variables nested there.
    return {"all": {"hosts": hostvars}}, failures


def _read_callback_events(path: Path, allowed_hosts: set[str]) -> dict[str, dict[str, Any]]:
    events: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return events
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(value, dict):
            continue
        host = value.get("host")
        state = value.get("state")
        if not isinstance(host, str) or host not in allowed_hosts or state not in {"success", "unreachable", "failed"}:
            continue
        if state == "success" and not isinstance(value.get("facts"), dict):
            continue
        events[host] = value
    return events


def _non_negative(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _linux_fact_records(
    run_id: str,
    host_id: str,
    facts: dict[str, Any],
    *,
    collected_at: str,
    source_path: str,
    source_commit_sha: str,
    sequence_start: int,
    display_name: str | None = None,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    system_payload = {
        "hostname": facts.get("hostname"),
        "osFamily": facts.get("osFamily"),
        "distribution": facts.get("distribution"),
        "distributionVersion": facts.get("distributionVersion"),
        "kernel": facts.get("kernel"),
        "architecture": facts.get("architecture"),
    }
    if all(isinstance(value, str) and value.strip() for value in system_payload.values()):
        sequence = sequence_start + len(records)
        records.append(
            {
                "schemaVersion": 1,
                "recordType": "linux.system_fact.v1",
                "runId": run_id,
                "sequence": sequence,
                "collectedAt": collected_at,
                "observedAt": collected_at,
                "source": {"type": "linux", "id": host_id},
                "resource": {"kind": "host_system", "id": "system", "displayName": system_payload["hostname"]},
                "payload": system_payload,
                "provenance": {"collector": "sentinel-ansible", "playbookPath": source_path, "playbookCommitSha": source_commit_sha},
                "recordKey": deterministic_record_key(record_type="linux.system_fact.v1", source_type="linux", source_id=host_id, resource_kind="host_system", resource_id="system", observed_at=collected_at),
            }
        )
    mounts = facts.get("mounts")
    if isinstance(mounts, list):
        for mount in mounts:
            if not isinstance(mount, dict):
                continue
            mount_path = mount.get("mount") or mount.get("mountPath")
            filesystem_type = mount.get("fstype") or mount.get("filesystemType")
            total = _non_negative(mount.get("size_total") if "size_total" in mount else mount.get("totalBytes"))
            available = _non_negative(mount.get("size_available") if "size_available" in mount else mount.get("availableBytes"))
            used = _non_negative(mount.get("size_used") if "size_used" in mount else mount.get("usedBytes"))
            if not isinstance(mount_path, str) or not mount_path or not isinstance(filesystem_type, str) or not filesystem_type:
                continue
            if total is None or available is None or used is None or used > total:
                continue
            resource_id = "fs-" + hashlib.sha256(f"{mount.get('device', '')}\x1f{mount_path}".encode("utf-8")).hexdigest()[:24]
            sequence = sequence_start + len(records)
            records.append(
                {
                    "schemaVersion": 1,
                    "recordType": "linux.filesystem_snapshot.v1",
                    "runId": run_id,
                    "sequence": sequence,
                    "collectedAt": collected_at,
                    "observedAt": collected_at,
                    "source": {"type": "linux", "id": host_id},
                    "resource": {"kind": "filesystem", "id": resource_id, "displayName": mount_path},
                    "payload": {"mountPath": mount_path, "filesystemType": filesystem_type, "totalBytes": total, "usedBytes": used, "availableBytes": available},
                    "provenance": {"collector": "sentinel-ansible", "playbookPath": source_path, "playbookCommitSha": source_commit_sha},
                    "recordKey": deterministic_record_key(record_type="linux.filesystem_snapshot.v1", source_type="linux", source_id=host_id, resource_kind="filesystem", resource_id=resource_id, observed_at=collected_at),
                }
            )
    filesystem_records = [
        record for record in records if record["recordType"] == "linux.filesystem_snapshot.v1"
    ]
    total_disk = sum(int(record["payload"]["totalBytes"]) for record in filesystem_records)
    used_disk = sum(int(record["payload"]["usedBytes"]) for record in filesystem_records)
    cpu_cores = _non_negative(facts.get("cpuCores"))
    memory_total_mb = _non_negative(facts.get("memoryTotalMb"))
    memory_available_mb = _non_negative(facts.get("memoryAvailableMb"))
    if cpu_cores is not None and memory_total_mb is not None:
        memory_total = memory_total_mb * 1024 * 1024
        memory_available = min(memory_total, (memory_available_mb or 0) * 1024 * 1024)
        sequence = sequence_start + len(records)
        records.append(
            {
                "schemaVersion": 1,
                "recordType": "linux.capacity_snapshot.v1",
                "runId": run_id,
                "sequence": sequence,
                "collectedAt": collected_at,
                "observedAt": collected_at,
                "source": {"type": "linux", "id": host_id},
                "resource": {"kind": "host_capacity", "id": "system", "displayName": display_name or host_id},
                "payload": {
                    "cpuCores": cpu_cores,
                    "memoryTotalBytes": memory_total,
                    "memoryUsedBytes": memory_total - memory_available,
                    "diskTotalBytes": total_disk,
                    "diskUsedBytes": used_disk,
                },
                "provenance": {"collector": "sentinel-ansible", "playbookPath": source_path, "playbookCommitSha": source_commit_sha},
                "recordKey": deterministic_record_key(record_type="linux.capacity_snapshot.v1", source_type="linux", source_id=host_id, resource_kind="host_capacity", resource_id="system", observed_at=collected_at),
            }
        )
    return records


def _outcome_state(outcomes: list[dict[str, Any]]) -> str:
    states = {str(item.get("state")) for item in outcomes}
    if not states or states <= {"skipped"}:
        return "failed"
    if states == {"unreachable"}:
        return "unreachable"
    if "failed" in states and "success" not in states:
        return "failed"
    if "unreachable" in states or "failed" in states or "skipped" in states:
        return "partial"
    return "completed"


def _update_profile_run_status(profile: dict[str, Any], state: str, outcomes: list[dict[str, Any]]) -> None:
    total = len(outcomes)
    successful = sum(1 for item in outcomes if item.get("state") == "success")
    profile["lastRun"] = f"{state.capitalize()} {_timestamp(None)}"
    profile["coverage"] = f"{successful}/{total} targets collected" if total else "No targets collected"
    store_record("profiles", profile)


def test_linux_host_connection(
    host: dict[str, Any], *, runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run
) -> dict[str, str]:
    """Perform an Ansible ping with temporary runtime-only SSH material."""

    with tempfile.TemporaryDirectory(prefix="sentinel-ssh-test-") as raw_workspace:
        workspace = Path(raw_workspace)
        inventory, failures = _build_inventory(workspace, [host], {})
        if failures:
            raise CollectionExecutionError(str(failures[0]["reason"]))
        inventory_path = workspace / "inventory.json"
        _write_private(inventory_path, json.dumps(inventory, ensure_ascii=True, separators=(",", ":")))
        try:
            result = runner(
                ["ansible", "all", "--inventory", str(inventory_path), "--module-name", "ansible.builtin.ping"],
                cwd=str(workspace),
                env={**os.environ, "ANSIBLE_NOCOLOR": "1"},
                capture_output=True,
                text=True,
                timeout=min(60, _timeout_seconds()),
                check=False,
            )
        except FileNotFoundError as exc:
            raise CollectionExecutionError("ansible is not installed in the Sentinel execution image.") from exc
        except subprocess.TimeoutExpired as exc:
            raise CollectionExecutionError("The SSH connection test exceeded Sentinel's time limit.") from exc
        if result.returncode != 0:
            raise CollectionExecutionError("The SSH connection test did not complete successfully.")
    return {"mode": "live", "message": f"SSH connection to {host['name']} succeeded."}


def execute_profile_collection(
    profile: dict[str, Any],
    run: dict[str, Any],
    client: ForgejoClient,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    """Execute one reserved Linux profile and ingest only controlled facts."""

    run_id = str(run["id"])
    mark_collection_running(run_id)
    started_at = datetime.now(timezone.utc)
    outcomes: list[dict[str, Any]] = []
    try:
        source = require_runnable_source(profile)
        playbook = read_profile_playbook(profile, client)
        content = playbook["content"]
        validate_read_only_playbook(content)
        expected = run.get("expectedSourceInstances")
        expected_ids = [str(item.get("id")) for item in expected if isinstance(item, dict) and item.get("type") == "linux"] if isinstance(expected, list) else []
        if not expected_ids:
            raise CollectionExecutionError("The selected profile has no Linux targets reserved for collection.")
        resolved_hosts = {
            host_id: host
            for host_id in expected_ids
            if (host := get_record("hosts", host_id)) is not None
        }
        hosts = [host for host in resolved_hosts.values() if host.get("lifecycle", "active") == "active"]
        active_ids = {str(host.get("id") or host.get("name")) for host in hosts}
        inactive_ids = set(expected_ids) - active_ids
        outcomes.extend(
            {
                "host_id": host_id,
                "host_name": str(resolved_hosts.get(host_id, {}).get("name") or host_id),
                "state": "skipped",
                "reason": "Target is no longer active in Sentinel inventory.",
            }
            for host_id in sorted(inactive_ids)
        )
        with tempfile.TemporaryDirectory(prefix="sentinel-collection-") as raw_workspace:
            workspace = Path(raw_workspace)
            playbook_path = workspace / "playbook.yml"
            inventory_path = workspace / "inventory.json"
            callbacks = workspace / "callbacks"
            callbacks.mkdir(mode=0o700)
            events_path = workspace / "events.ndjson"
            _write_private(playbook_path, content)
            _write_private(callbacks / "sentinel_facts.py", _CALLBACK_PLUGIN)
            inventory, setup_failures = _build_inventory(workspace, hosts, profile)
            outcomes.extend(setup_failures)
            runnable_ids = set(inventory["all"]["hosts"])
            host_names = {
                str(host.get("id") or host.get("name")): str(host.get("name") or host.get("id"))
                for host in hosts
            }
            if not runnable_ids:
                final_state = _outcome_state(outcomes)
                record_host_outcomes(run_id, outcomes)
                complete_live_collection(run_id, final_state, failure_reason="No target could be prepared for live collection.")
                _update_profile_run_status(profile, final_state, outcomes)
                return {"run": run, "state": final_state, "outcomes": outcomes, "message": "No target could be prepared for live collection."}
            _write_private(inventory_path, json.dumps(inventory, ensure_ascii=True, separators=(",", ":")))
            environment = {**os.environ, "ANSIBLE_CALLBACK_PLUGINS": str(callbacks), "ANSIBLE_CALLBACKS_ENABLED": "sentinel_facts", "ANSIBLE_NOCOLOR": "1", "SENTINEL_FACT_EVENTS": str(events_path)}
            try:
                completed = runner(["ansible-playbook", "--inventory", str(inventory_path), str(playbook_path)], cwd=str(workspace), env=environment, capture_output=True, text=True, timeout=_timeout_seconds(), check=False)
            except FileNotFoundError as exc:
                raise CollectionExecutionError("ansible-playbook is not installed in the Sentinel execution image.") from exc
            except subprocess.TimeoutExpired as exc:
                raise CollectionExecutionError("The collection exceeded Sentinel's execution time limit.") from exc
            events = _read_callback_events(events_path, runnable_ids)
            for host_id in sorted(runnable_ids):
                event = events.get(host_id)
                if event is None:
                    outcomes.append({"host_id": host_id, "host_name": host_names.get(host_id, host_id), "state": "failed", "reason": "Collector returned no fact result for this target."})
                elif event["state"] == "success":
                    outcomes.append({"host_id": host_id, "host_name": host_names.get(host_id, host_id), "state": "success"})
                else:
                    outcomes.append({"host_id": host_id, "host_name": host_names.get(host_id, host_id), "state": event["state"], "reason": "Target did not complete fact gathering."})
            if completed.returncode != 0 and not any(item.get("state") in {"failed", "unreachable"} for item in outcomes):
                outcomes.extend({"host_id": host_id, "host_name": host_names.get(host_id, host_id), "state": "failed", "reason": "Ansible collection did not complete successfully."} for host_id in sorted(runnable_ids))
            collected_at = _timestamp(datetime.now(timezone.utc))
            fact_records: list[dict[str, Any]] = []
            for outcome in outcomes:
                if outcome.get("state") != "success":
                    continue
                event = events.get(str(outcome["host_id"]))
                if event is None:
                    continue
                generated = _linux_fact_records(
                    run_id,
                    str(outcome["host_id"]),
                    event["facts"],
                    collected_at=collected_at,
                    source_path=str(source["path"]),
                    source_commit_sha=str(source["commitSha"]),
                    sequence_start=len(fact_records) + 1,
                    display_name=str(outcome.get("host_name") or outcome["host_id"]),
                )
                if not generated:
                    outcome["state"] = "failed"
                    outcome["reason"] = "Collector returned no supported Linux facts."
                fact_records.extend(generated)
            final_state = _outcome_state(outcomes)
            if not fact_records:
                record_host_outcomes(run_id, outcomes)
                complete_live_collection(run_id, final_state, failure_reason="No supported facts were returned by the collection.")
                _update_profile_run_status(profile, final_state, outcomes)
                return {"run": run, "state": final_state, "outcomes": outcomes, "message": "No supported facts were returned by the collection."}
            artifact = prepare_sanitized_artifact(run_id=run_id, records=fact_records, source_instances=[{"type": "linux", "id": host_id} for host_id in expected_ids], collector_version="sentinel-ansible-v1", location=f"{run_id}/facts.ndjson.gz", profile_id=str(profile["id"]), source_path=str(source["path"]), source_commit_sha=str(source["commitSha"]), execution_status=final_state if final_state in {"completed", "partial", "failed"} else "partial", collection_started_at=started_at, collection_completed_at=datetime.now(timezone.utc))
            write_sanitized_artifact(_artifact_root(), artifact.manifest["artifact"]["location"], artifact.ndjson)
            receipt = ingest_ndjson_artifact(artifact.manifest, artifact.ndjson)
            if receipt.state != "accepted":
                for outcome in outcomes:
                    if outcome.get("state") == "success":
                        outcome["state"] = "failed"
                        outcome["reason"] = "Sentinel rejected the collected artifact."
                record_host_outcomes(run_id, outcomes)
                complete_live_collection(
                    run_id, "failed", failure_reason="Sentinel rejected the collected artifact."
                )
                _update_profile_run_status(profile, "failed", outcomes)
                return {
                    "run": run,
                    "state": "failed",
                    "outcomes": outcomes,
                    "message": "Sentinel rejected the collected artifact.",
                }
            record_host_outcomes(run_id, outcomes)
            complete_live_collection(run_id, final_state)
            _update_profile_run_status(profile, final_state, outcomes)
            return {"run": run, "state": final_state, "outcomes": outcomes, "message": "Live collection completed."}
    except CollectionExecutionError as exc:
        record_host_outcomes(run_id, outcomes)
        complete_live_collection(run_id, "failed", failure_reason=str(exc))
        _update_profile_run_status(profile, "failed", outcomes)
        return {"run": run, "state": "failed", "outcomes": outcomes, "message": str(exc)}
    except Exception:
        record_host_outcomes(run_id, outcomes)
        complete_live_collection(run_id, "failed", failure_reason="Live collection execution failed.")
        _update_profile_run_status(profile, "failed", outcomes)
        return {"run": run, "state": "failed", "outcomes": outcomes, "message": "Live collection execution failed."}
