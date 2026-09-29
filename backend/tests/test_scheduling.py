from datetime import datetime, timezone
import sys
from pathlib import Path

import pytest


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from sentinel.cron import CronExpressionError, describe_cron_expression, parse_cron_expression
from sentinel import scheduling
from sentinel import collections


SHA = "a" * 40


def runnable_profile(**overrides):
    profile = {
        "id": "scheduled-profile",
        "name": "Scheduled facts",
        "state": "enabled",
        "schedule": "5 4 * * *",
        "source": {
            "repository": "sentinel/sentinel-playbooks",
            "path": "inventory/facts.yml",
            "commitSha": SHA,
            "state": "pinned",
        },
    }
    profile.update(overrides)
    return profile


def test_linux_cron_description_matches_the_profile_example():
    schedule = parse_cron_expression("5 4 * * *")

    assert schedule.expression == "5 4 * * *"
    assert describe_cron_expression(schedule) == "At 04:05."
    assert schedule.matches(datetime(2026, 9, 28, 4, 5, tzinfo=timezone.utc))
    assert not schedule.matches(datetime(2026, 9, 28, 4, 6, tzinfo=timezone.utc))


@pytest.mark.parametrize("expression", ["5 4 * *", "60 4 * * *", "5 4 * * * *", "0-60 * * * *"])
def test_linux_cron_rejects_invalid_expressions(expression):
    with pytest.raises(CronExpressionError):
        parse_cron_expression(expression)


def test_scheduler_queues_only_due_enabled_pinned_profiles(monkeypatch):
    queued = []
    profiles = [
        runnable_profile(),
        runnable_profile(id="paused", state="paused"),
        runnable_profile(id="pending", source={"state": "migration-pending-review"}),
        runnable_profile(id="manual", schedule=None),
    ]
    monkeypatch.setattr(scheduling, "records", lambda kind: profiles if kind == "profiles" else [])
    monkeypatch.setattr(scheduling, "reserve_schedule_slot", lambda profile_id, scheduled_for: True)
    monkeypatch.setattr(scheduling, "queue_profile_run", lambda profile, trigger: queued.append((profile["id"], trigger)))

    result = scheduling.run_due_collection_profiles(datetime(2026, 9, 28, 4, 5, tzinfo=timezone.utc))

    assert result == ["scheduled-profile"]
    assert queued == [("scheduled-profile", "schedule")]


def test_scheduler_does_not_queue_a_slot_that_is_already_reserved(monkeypatch):
    monkeypatch.setattr(scheduling, "records", lambda kind: [runnable_profile()])
    monkeypatch.setattr(scheduling, "reserve_schedule_slot", lambda profile_id, scheduled_for: False)
    monkeypatch.setattr(scheduling, "queue_profile_run", lambda profile, trigger: pytest.fail("must not queue"))

    assert scheduling.run_due_collection_profiles(datetime(2026, 9, 28, 4, 5, tzinfo=timezone.utc)) == []


def test_profile_queue_reserves_owned_source_instances_before_dispatch(monkeypatch):
    captured = {}
    profile = runnable_profile(playbook="inventory/linux-facts.yml", scope="All active Linux hosts")

    monkeypatch.setattr(
        collections,
        "records",
        lambda kind: [
            {"name": "linux-one", "lifecycle": "active"},
            {"name": "linux-disabled", "lifecycle": "disabled"},
            {"name": "manual-one", "lifecycle": "active", "sourceType": "manual"},
        ]
        if kind == "hosts"
        else [],
    )
    monkeypatch.setattr(collections, "store_record", lambda kind, value: value)
    monkeypatch.setattr(
        collections,
        "create_run",
        lambda name, summary, collection_context: captured.update(collection_context) or {"id": "run-1"},
    )

    collections.queue_profile_run(profile, trigger="schedule")

    assert captured["trigger"] == "schedule"
    assert captured["expected_source_instances"] == [
        {"type": "linux", "id": "linux-one"},
        {"type": "linux", "id": "manual-one"},
    ]
