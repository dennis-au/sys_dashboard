import re
from typing import Any
from uuid import uuid4

from .records import records, store_record
from .reporting import reserve_live_collection


def basic_playbook_issues(source: str) -> list[str]:
    issues = []
    if not source.strip():
        issues.append("Playbook source cannot be empty.")
    if "\t" in source:
        issues.append("Replace tab indentation with spaces.")
    if not re.search(r"^---\s*$", source, re.MULTILINE):
        issues.append("Start the YAML document with ---.")
    if not re.search(r"^\s*-\s+name\s*:", source, re.MULTILINE):
        issues.append("Add an Ansible play name.")
    if not re.search(r"^\s+hosts\s*:", source, re.MULTILINE):
        issues.append("Specify hosts for the Ansible play.")
    if not re.search(r"^\s+tasks\s*:", source, re.MULTILINE):
        issues.append("Add a tasks section.")
    return issues


def create_run(
    name: str,
    summary: str,
    run_type: str = "success",
    *,
    collection_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Record a queued live run before a worker contacts a configured source."""

    context = collection_context or {}
    record = {
        "id": f"run-{uuid4().hex}",
        "name": name,
        "date": "Just now",
        "summary": summary,
        "status": "Queued",
        "type": run_type,
    }
    expected_sources = context.get("expected_source_instances")
    if isinstance(expected_sources, list):
        # This is source identity metadata, not credentials or source content.
        # The authoritative copy is in collection_run_details.metadata.
        record["expectedSourceInstances"] = expected_sources
    store_record("runs", record)
    reserve_live_collection(record, **context)
    return record


def queue_profile_run(
    profile: dict[str, Any], *, trigger: str = "manual", hosts: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """Reserve a runnable profile for the worker-owned live execution boundary."""

    scheduled = trigger == "schedule"
    profile["lastRun"] = "Scheduled collection queued" if scheduled else "Manual collection queued"
    store_record("profiles", profile)
    target_hosts = list(hosts) if hosts is not None else _target_hosts(profile)
    expected_source_instances = _expected_source_instances(profile, target_hosts)
    name = f"{'Scheduled' if scheduled else 'Manual'} collection: {profile['name']}"
    summary = f"{'Scheduled' if scheduled else 'Manual'} profile run queued for worker execution"
    run = create_run(
        name,
        summary,
        collection_context={
            "profile": profile,
            "hosts": target_hosts,
            "source_type": "profile",
            "trigger": trigger,
            "expected_source_instances": expected_source_instances,
        },
    )
    return {
        "profile": profile,
        "run": run,
        "mode": "live",
        "message": f"Queued collection run for {profile['name']}. The Sentinel worker will execute it.",
    }


def _target_hosts(profile: dict[str, Any]) -> list[dict[str, Any]]:
    """Resolve target ownership from Sentinel records, never a static inventory group."""

    playbook = str(profile.get("playbook", "")).lower()
    if "olvm" in playbook:
        return []
    manual_scope = "manual" in str(profile.get("scope", "")).lower()
    return [
        host
        for host in records("hosts")
        if host.get("lifecycle", "active") == "active"
        and (not manual_scope or host.get("sourceType") == "manual")
    ]


def _expected_source_instances(profile: dict[str, Any], hosts: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Bind Linux sources to Sentinel host IDs and OLVM profiles to manager IDs."""

    playbook = str(profile.get("playbook", "")).lower()
    if "olvm" in playbook:
        return [{"type": "olvm", "id": str(manager["id"])} for manager in records("managers")]
    return [{"type": "linux", "id": str(host.get("id") or host["name"])} for host in hosts]
