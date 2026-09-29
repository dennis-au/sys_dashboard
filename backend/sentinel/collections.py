import re
from typing import Any
from uuid import uuid4

from .records import records, store_record
from .reporting import record_simulated_collection


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
    """Record the legacy activity item plus an honest queued simulation record."""

    record = {
        "id": f"run-{uuid4().hex}",
        "name": name,
        "date": "Just now",
        "summary": summary,
        "status": "Completed",
        "type": run_type,
    }
    store_record("runs", record)
    record_simulated_collection(record, **(collection_context or {}))
    return record


def queue_profile_run(profile: dict[str, Any], *, trigger: str = "manual") -> dict[str, Any]:
    """Queue the current safe simulation after its caller has enforced Git gating."""

    scheduled = trigger == "schedule"
    profile["lastRun"] = "Scheduled simulation queued" if scheduled else "Manual simulation queued"
    store_record("profiles", profile)
    expected_source_instances = _expected_source_instances(profile)
    name = f"{'Scheduled' if scheduled else 'Manual'} collection: {profile['name']}"
    summary = f"{'Scheduled' if scheduled else 'Manual'} profile run accepted - no target contacted"
    run = create_run(
        name,
        summary,
        collection_context={
            "profile": profile,
            "source_type": "profile",
            "trigger": trigger,
            "expected_source_instances": expected_source_instances,
        },
    )
    return {"profile": profile, **simulated_result(f"collection run for {profile['name']}"), "run": run}


def _expected_source_instances(profile: dict[str, Any]) -> list[dict[str, str]]:
    """Resolve target ownership from Sentinel records, never a static inventory group."""

    playbook = str(profile.get("playbook", "")).lower()
    if "olvm" in playbook:
        return [{"type": "olvm", "id": str(manager["id"])} for manager in records("managers")]
    manual_scope = "manual" in str(profile.get("scope", "")).lower()
    return [
        {"type": "linux", "id": str(host["name"])}
        for host in records("hosts")
        if host.get("lifecycle", "active") == "active"
        and (not manual_scope or host.get("sourceType") == "manual")
    ]


def simulated_result(subject: str) -> dict[str, str]:
    return {
        "mode": "simulation",
        "message": f"Simulated {subject}. No infrastructure was contacted.",
    }
