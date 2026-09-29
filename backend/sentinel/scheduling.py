"""Persistence, dispatch, and supervised execution of collection schedules."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from .audit import record_exception
from .collections import queue_profile_run
from .cron import CronExpressionError, describe_cron_expression, parse_cron_expression
from .execution import execute_profile_collection
from .playbooks import require_runnable_source
from .records import database_connection, get_record, records, store_record
from .reporting import claim_queued_profile_runs


LOGGER = logging.getLogger(__name__)
LEGACY_SCHEDULES = {
    "Every hour": "0 * * * *",
    "Every 4 hours": "0 */4 * * *",
    "Daily": "0 0 * * *",
    "Manual": None,
}


def scheduler_schema_statements() -> list[str]:
    return [
        """
        CREATE TABLE IF NOT EXISTS sentinel.collection_profile_schedule_slots (
            profile_id TEXT NOT NULL,
            scheduled_for TIMESTAMPTZ NOT NULL,
            run_id TEXT,
            PRIMARY KEY (profile_id, scheduled_for)
        )
        """,
        "CREATE INDEX IF NOT EXISTS collection_profile_schedule_slots_scheduled_for_idx ON sentinel.collection_profile_schedule_slots (scheduled_for DESC)",
    ]


def schedule_metadata(value: Any) -> tuple[str | None, str]:
    """Validate an optional expression and provide its stable display label."""

    if value is None:
        return None, "Manual only."
    if not isinstance(value, str):
        raise CronExpressionError("A five-field Linux cron expression is required.")
    expression = value.strip()
    if not expression:
        return None, "Manual only."
    parsed = parse_cron_expression(expression)
    return parsed.expression, describe_cron_expression(parsed)


def migrate_profile_schedules() -> None:
    """Convert representative display schedules to cron without changing source ownership."""

    for profile in records("profiles"):
        existing = profile.get("schedule")
        mapped = LEGACY_SCHEDULES.get(existing, existing)
        try:
            expression, description = schedule_metadata(mapped)
        except CronExpressionError:
            expression, description = None, "Schedule needs configuration."
        if (
            profile.get("schedule") != expression
            or profile.get("scheduleDescription") != description
            or profile.get("scheduleTimezone") != "UTC"
        ):
            profile["schedule"] = expression
            profile["scheduleDescription"] = description
            profile["scheduleTimezone"] = "UTC"
            store_record("profiles", profile)


def _minute_slot(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).replace(second=0, microsecond=0)


def reserve_schedule_slot(profile_id: str, scheduled_for: datetime) -> bool:
    """Atomically claim one profile/minute slot, including across API processes."""

    with database_connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO sentinel.collection_profile_schedule_slots (profile_id, scheduled_for)
            VALUES (%s, %s)
            ON CONFLICT DO NOTHING
            RETURNING profile_id
            """,
            (profile_id, _minute_slot(scheduled_for)),
        )
        return cursor.fetchone() is not None


def release_schedule_slot(profile_id: str, scheduled_for: datetime) -> None:
    with database_connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            "DELETE FROM sentinel.collection_profile_schedule_slots WHERE profile_id = %s AND scheduled_for = %s",
            (profile_id, _minute_slot(scheduled_for)),
        )


def run_due_collection_profiles(now: datetime | None = None) -> list[str]:
    """Queue each due, enabled, reviewed profile once for the current UTC minute."""

    scheduled_for = _minute_slot(now or datetime.now(timezone.utc))
    queued: list[str] = []
    for profile in records("profiles"):
        if profile.get("state") != "enabled" or not profile.get("schedule"):
            continue
        try:
            cron = parse_cron_expression(profile["schedule"])
            require_runnable_source(profile)
        except CronExpressionError as exc:
            LOGGER.warning("Skipping profile %s with invalid persisted cron schedule", profile.get("id"))
            record_exception(
                exc,
                service="worker",
                event_kind="invalid_schedule",
                context={"profile_id": profile.get("id")},
            )
            continue
        except Exception as exc:
            # Review-pending and unavailable sources remain non-runnable until
            # their ordinary workflow resolves them; a scheduler never bypasses it.
            record_exception(
                exc,
                service="worker",
                event_kind="scheduled_profile_unavailable",
                context={"profile_id": profile.get("id")},
            )
            continue
        if not cron.matches(scheduled_for) or not reserve_schedule_slot(str(profile["id"]), scheduled_for):
            continue
        try:
            queue_profile_run(profile, trigger="schedule")
        except Exception as exc:
            release_schedule_slot(str(profile["id"]), scheduled_for)
            LOGGER.exception("Unable to queue scheduled collection profile %s", profile.get("id"))
            record_exception(
                exc,
                service="worker",
                event_kind="scheduled_profile_queue_failure",
                context={"profile_id": profile.get("id"), "scheduled_for": scheduled_for.isoformat()},
            )
            continue
        queued.append(str(profile["id"]))
    return queued


def execute_queued_collection_profiles() -> list[str]:
    """Run claimed profile work from the single supervised worker authority."""

    claimed_runs = claim_queued_profile_runs()
    if not claimed_runs:
        return []
    try:
        from forgejo_client import ForgejoClient

        client = ForgejoClient.from_environment()
    except Exception as exc:
        record_exception(exc, service="worker", event_kind="collection_executor_unavailable")
        from .reporting import complete_live_collection

        for claimed in claimed_runs:
            complete_live_collection(
                str(claimed["run_id"]),
                "failed",
                failure_reason="The collection executor is unavailable.",
            )
        return []
    completed: list[str] = []
    try:
        for claimed in claimed_runs:
            run_id = str(claimed["run_id"])
            profile_id = str(claimed["profile_id"])
            profile = get_record("profiles", profile_id)
            run = get_record("runs", run_id)
            if profile is None or run is None:
                from .reporting import complete_live_collection

                complete_live_collection(run_id, "failed", failure_reason="Collection profile or run metadata is unavailable.")
                continue
            try:
                execute_profile_collection(profile, run, client)
            except Exception as exc:
                # The executor itself contains source-safe failure conversion;
                # this preserves worker availability for an unexpected defect.
                LOGGER.exception("Collection executor failed for profile %s", profile_id)
                record_exception(
                    exc,
                    service="worker",
                    event_kind="collection_executor_failure",
                    context={"profile_id": profile_id, "run_id": run_id},
                )
                continue
            completed.append(run_id)
    finally:
        client.close()
    return completed


async def collection_scheduler(stop_event: asyncio.Event) -> None:
    """Poll the current UTC minute while allowing FastAPI shutdown to be prompt."""

    while not stop_event.is_set():
        try:
            await asyncio.to_thread(run_due_collection_profiles)
            await asyncio.to_thread(execute_queued_collection_profiles)
        except Exception as exc:
            LOGGER.exception("Collection scheduler pass failed")
            record_exception(exc, service="worker", event_kind="scheduler_pass_failure")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=15)
        except TimeoutError:
            pass
