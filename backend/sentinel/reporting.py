"""Structured, simulation-only reporting records and summary calculations."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable
from uuid import uuid4

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .records import database_connection


SUMMARY_STALE_AFTER = timedelta(hours=2)
SIMULATION_MESSAGE = "Sentinel actions are currently simulated; no target or external service was contacted."


def reporting_schema_statements() -> list[str]:
    """Return the append-only operational tables and Grafana-safe reporting views."""

    return [
        """
        CREATE TABLE IF NOT EXISTS sentinel.collection_run_details (
            run_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            profile_id TEXT,
            profile_name TEXT,
            manager_id TEXT,
            source_type TEXT NOT NULL,
            source_path TEXT,
            source_commit_sha TEXT,
            source_state TEXT,
            execution_mode TEXT NOT NULL CHECK (execution_mode IN ('simulation')),
            state TEXT NOT NULL CHECK (state IN ('queued', 'completed', 'partial', 'unreachable', 'failed')),
            requested_at TIMESTAMPTZ NOT NULL,
            started_at TIMESTAMPTZ,
            completed_at TIMESTAMPTZ,
            duration_ms INTEGER CHECK (duration_ms IS NULL OR duration_ms >= 0),
            failure_reason TEXT,
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS sentinel.collection_host_results (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            host_id TEXT NOT NULL,
            host_name TEXT NOT NULL,
            source_type TEXT NOT NULL,
            manager_id TEXT,
            profile_id TEXT,
            source_path TEXT,
            source_commit_sha TEXT,
            source_state TEXT,
            execution_mode TEXT NOT NULL CHECK (execution_mode IN ('simulation')),
            state TEXT NOT NULL CHECK (state IN ('queued', 'success', 'unreachable', 'failed', 'skipped')),
            started_at TIMESTAMPTZ,
            completed_at TIMESTAMPTZ,
            duration_ms INTEGER CHECK (duration_ms IS NULL OR duration_ms >= 0),
            reason TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS sentinel.host_capacity_facts (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            host_id TEXT NOT NULL,
            host_name TEXT NOT NULL,
            observed_at TIMESTAMPTZ NOT NULL,
            cpu_cores INTEGER CHECK (cpu_cores IS NULL OR cpu_cores >= 0),
            memory_total_bytes BIGINT CHECK (memory_total_bytes IS NULL OR memory_total_bytes >= 0),
            memory_used_bytes BIGINT CHECK (memory_used_bytes IS NULL OR memory_used_bytes >= 0),
            disk_total_bytes BIGINT CHECK (disk_total_bytes IS NULL OR disk_total_bytes >= 0),
            disk_used_bytes BIGINT CHECK (disk_used_bytes IS NULL OR disk_used_bytes >= 0),
            execution_mode TEXT NOT NULL CHECK (execution_mode IN ('simulation'))
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS sentinel.collection_alerts (
            id TEXT PRIMARY KEY,
            run_id TEXT,
            host_id TEXT,
            host_name TEXT,
            severity TEXT NOT NULL CHECK (severity IN ('info', 'warning', 'critical')),
            state TEXT NOT NULL CHECK (state IN ('open', 'acknowledged', 'resolved')),
            category TEXT NOT NULL,
            summary TEXT NOT NULL,
            observed_at TIMESTAMPTZ NOT NULL,
            resolved_at TIMESTAMPTZ,
            execution_mode TEXT NOT NULL CHECK (execution_mode IN ('simulation'))
        )
        """,
        # The development build formerly exposed an unrelated display-string
        # view with this name. It has no data of its own, so replacing it does
        # not delete run history.
        "DROP VIEW IF EXISTS reporting.host_health",
        "DROP VIEW IF EXISTS reporting.inventory_health",
        "DROP VIEW IF EXISTS reporting.open_alerts",
        "DROP VIEW IF EXISTS reporting.host_capacity_latest",
        "DROP VIEW IF EXISTS reporting.host_collection_results",
        "DROP VIEW IF EXISTS reporting.collection_runs",
        "ALTER TABLE sentinel.collection_run_details ADD COLUMN IF NOT EXISTS source_state TEXT",
        "ALTER TABLE sentinel.collection_host_results ADD COLUMN IF NOT EXISTS source_state TEXT",
        "CREATE INDEX IF NOT EXISTS collection_host_results_run_id_idx ON sentinel.collection_host_results (run_id)",
        "CREATE INDEX IF NOT EXISTS collection_host_results_completed_at_idx ON sentinel.collection_host_results (completed_at DESC)",
        "CREATE INDEX IF NOT EXISTS host_capacity_facts_host_observed_idx ON sentinel.host_capacity_facts (host_id, observed_at DESC)",
        "CREATE INDEX IF NOT EXISTS collection_alerts_state_observed_idx ON sentinel.collection_alerts (state, observed_at DESC)",
        """
        CREATE OR REPLACE VIEW reporting.collection_runs AS
        SELECT
          details.run_id,
          details.name AS run_name,
          details.profile_id,
          details.profile_name,
          details.manager_id,
          details.source_type,
          details.source_path,
          details.source_commit_sha,
          details.source_state,
          details.execution_mode,
          details.state,
          details.requested_at,
          details.started_at,
          details.completed_at,
          details.duration_ms,
          details.failure_reason,
          legacy.payload->>'summary' AS display_summary
        FROM sentinel.collection_run_details AS details
        LEFT JOIN sentinel.collection_runs AS legacy ON legacy.id = details.run_id
        """,
        """
        CREATE OR REPLACE VIEW reporting.host_collection_results AS
        SELECT
          id,
          run_id,
          host_id,
          host_name,
          source_type,
          manager_id,
          profile_id,
          source_path,
          source_commit_sha,
          source_state,
          execution_mode,
          state,
          started_at,
          completed_at,
          duration_ms,
          reason
        FROM sentinel.collection_host_results
        """,
        """
        CREATE OR REPLACE VIEW reporting.host_capacity_latest AS
        SELECT DISTINCT ON (host_id)
          id,
          run_id,
          host_id,
          host_name,
          observed_at,
          cpu_cores,
          memory_total_bytes,
          memory_used_bytes,
          disk_total_bytes,
          disk_used_bytes,
          execution_mode
        FROM sentinel.host_capacity_facts
        ORDER BY host_id, observed_at DESC, id DESC
        """,
        """
        CREATE OR REPLACE VIEW reporting.open_alerts AS
        SELECT
          id,
          run_id,
          host_id,
          host_name,
          severity,
          state,
          category,
          summary,
          observed_at,
          execution_mode
        FROM sentinel.collection_alerts
        WHERE state IN ('open', 'acknowledged')
        """,
        """
        CREATE OR REPLACE VIEW reporting.inventory_health AS
        SELECT
          hosts.id AS host_id,
          hosts.payload->>'name' AS host_name,
          hosts.payload->>'sourceType' AS source_type,
          hosts.payload->>'environment' AS environment,
          hosts.payload->>'lifecycle' AS lifecycle,
          COALESCE(hosts.payload->>'status', 'unknown') AS inventory_status,
          latest_result.state AS latest_collection_state,
          latest_result.completed_at AS latest_collection_at
        FROM sentinel.hosts AS hosts
        LEFT JOIN LATERAL (
          SELECT state, completed_at
          FROM sentinel.collection_host_results
          WHERE host_id = hosts.id
          ORDER BY completed_at DESC NULLS LAST, id DESC
          LIMIT 1
        ) AS latest_result ON TRUE
        """,
        """
        CREATE OR REPLACE VIEW reporting.host_health AS
        SELECT
          host_name,
          source_type,
          environment,
          inventory_status AS status,
          latest_collection_state,
          latest_collection_at
        FROM reporting.inventory_health
        """,
    ]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime | str | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def iso_timestamp(value: datetime | str | None) -> str | None:
    timestamp = as_utc(value)
    if timestamp is None:
        return None
    return timestamp.isoformat(timespec="seconds").replace("+00:00", "Z")


def percentage(used: int, total: int) -> float | None:
    if total <= 0:
        return None
    return round((used / total) * 100, 2)


def _run_context(profile: dict[str, Any] | None) -> dict[str, str | None]:
    source = profile.get("source", {}) if profile else {}
    return {
        "profile_id": str(profile["id"]) if profile and profile.get("id") else None,
        "profile_name": str(profile["name"]) if profile and profile.get("name") else None,
        "source_path": str(source["path"]) if source.get("path") else None,
        "source_commit_sha": str(source["commitSha"]) if source.get("commitSha") else None,
        "source_state": str(source["state"]) if source.get("state") else None,
    }


def record_simulated_collection(
    run: dict[str, Any],
    *,
    hosts: Iterable[dict[str, Any]] = (),
    profile: dict[str, Any] | None = None,
    manager_id: str | None = None,
    source_type: str = "inventory",
    trigger: str = "manual",
    expected_source_instances: Iterable[dict[str, str]] = (),
) -> None:
    """Append a queued simulation record without claiming a completed collection."""

    requested_at = utc_now()
    context = _run_context(profile)
    host_list = list(hosts)
    with database_connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO sentinel.collection_run_details (
                run_id, name, profile_id, profile_name, manager_id, source_type,
                source_path, source_commit_sha, source_state, execution_mode, state, requested_at, metadata
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'simulation', 'queued', %s, %s)
            ON CONFLICT (run_id) DO NOTHING
            """,
            (
                str(run["id"]),
                str(run["name"]),
                context["profile_id"],
                context["profile_name"],
                manager_id,
                source_type,
                context["source_path"],
                context["source_commit_sha"],
                context["source_state"],
                requested_at,
                Jsonb(
                    {
                        "message": SIMULATION_MESSAGE,
                        "trigger": trigger,
                        "expectedSourceInstances": list(expected_source_instances),
                    }
                ),
            ),
        )
        for host in host_list:
            cursor.execute(
                """
                INSERT INTO sentinel.collection_host_results (
                    id, run_id, host_id, host_name, source_type, manager_id, profile_id,
                    source_path, source_commit_sha, source_state, execution_mode, state
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'simulation', 'queued')
                """,
                (
                    f"result-{uuid4().hex}",
                    str(run["id"]),
                    str(host["name"]),
                    str(host["name"]),
                    str(host.get("sourceType", source_type)),
                    str(host.get("sourceManagerId")) if host.get("sourceManagerId") else manager_id,
                    context["profile_id"],
                    context["source_path"],
                    context["source_commit_sha"],
                    context["source_state"],
                ),
            )


def _rows(query: str) -> list[dict[str, Any]]:
    with database_connection() as connection, connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(query)
        return list(cursor.fetchall())


def load_summary_data() -> dict[str, list[dict[str, Any]]]:
    """Load only curated reporting views, keeping the API independent of UI strings."""

    return {
        "inventory": _rows("SELECT * FROM reporting.inventory_health ORDER BY host_name"),
        "runs": _rows("SELECT * FROM reporting.collection_runs ORDER BY requested_at DESC"),
        "results": _rows("SELECT * FROM reporting.host_collection_results"),
        "capacity": _rows("SELECT * FROM reporting.host_capacity_latest"),
        "alerts": _rows("SELECT * FROM reporting.open_alerts ORDER BY observed_at DESC, id DESC"),
    }


def latest_completed_run(runs: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    completed = [
        run
        for run in runs
        if run.get("state") in {"completed", "partial", "unreachable", "failed"}
        and run.get("completed_at") is not None
    ]
    return max(completed, key=lambda run: as_utc(run["completed_at"]) or datetime.min.replace(tzinfo=timezone.utc), default=None)


def build_summary(
    data: dict[str, list[dict[str, Any]]], *, now: datetime | None = None
) -> dict[str, Any]:
    """Build a stable API document from structured reporting rows only."""

    generated_at = (now or utc_now()).astimezone(timezone.utc)
    inventory = data.get("inventory", [])
    runs = data.get("runs", [])
    results = data.get("results", [])
    capacity = data.get("capacity", [])
    alerts = data.get("alerts", [])

    health = Counter(
        str(host.get("inventory_status") or "unknown")
        for host in inventory
        if host.get("lifecycle") not in {"disabled", "decommissioned", "retired"}
    )
    source_counts = Counter(
        str(host.get("source_type") or "unknown")
        for host in inventory
        if host.get("lifecycle") not in {"disabled", "decommissioned", "retired"}
    )
    active_inventory = [
        host
        for host in inventory
        if host.get("lifecycle") not in {"disabled", "decommissioned", "retired"}
    ]

    completed = latest_completed_run(runs)
    completed_results = [
        result for result in results if completed and result.get("run_id") == completed.get("run_id")
    ]
    outcome_counts = Counter(str(result.get("state") or "unknown") for result in completed_results)
    completed_total = sum(
        outcome_counts[state] for state in ("success", "unreachable", "failed", "skipped")
    )
    attempted_total = sum(outcome_counts[state] for state in ("success", "unreachable", "failed"))
    successful = outcome_counts["success"]
    unreachable = outcome_counts["unreachable"]
    failed = outcome_counts["failed"]
    all_unreachable = attempted_total > 0 and unreachable == attempted_total

    completed_at = as_utc(completed.get("completed_at")) if completed else None
    age_seconds = (
        max(0, int((generated_at - completed_at).total_seconds())) if completed_at is not None else None
    )
    freshness_state = "unavailable"
    if completed_at is not None:
        freshness_state = "stale" if generated_at - completed_at > SUMMARY_STALE_AFTER else "fresh"

    memory_total = sum(int(row.get("memory_total_bytes") or 0) for row in capacity)
    memory_used = sum(int(row.get("memory_used_bytes") or 0) for row in capacity)
    disk_total = sum(int(row.get("disk_total_bytes") or 0) for row in capacity)
    disk_used = sum(int(row.get("disk_used_bytes") or 0) for row in capacity)
    cpu_cores = sum(int(row.get("cpu_cores") or 0) for row in capacity)

    severity_counts = Counter(str(alert.get("severity") or "info") for alert in alerts)
    recent_activity = []
    for run in sorted(runs, key=lambda row: as_utc(row.get("requested_at")) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)[:10]:
        run_results = [result for result in results if result.get("run_id") == run.get("run_id")]
        run_outcomes = Counter(str(result.get("state") or "unknown") for result in run_results)
        recent_activity.append(
            {
                "runId": run.get("run_id"),
                "name": run.get("run_name"),
                "state": run.get("state"),
                "mode": run.get("execution_mode"),
                "requestedAt": iso_timestamp(run.get("requested_at")),
                "completedAt": iso_timestamp(run.get("completed_at")),
                "durationMs": run.get("duration_ms"),
                "source": {
                    "type": run.get("source_type"),
                    "managerId": run.get("manager_id"),
                    "profileId": run.get("profile_id"),
                    "profileName": run.get("profile_name"),
                    "path": run.get("source_path"),
                    "commitSha": run.get("source_commit_sha"),
                    "state": run.get("source_state"),
                },
                "outcomes": {
                    "total": sum(run_outcomes.values()),
                    "successful": run_outcomes["success"],
                    "unreachable": run_outcomes["unreachable"],
                    "failed": run_outcomes["failed"],
                    "queued": run_outcomes["queued"],
                },
            }
        )

    return {
        "generatedAt": iso_timestamp(generated_at),
        "mode": {"kind": "simulation", "message": SIMULATION_MESSAGE},
        "freshness": {
            "state": freshness_state,
            "latestCollectedAt": iso_timestamp(completed_at),
            "ageSeconds": age_seconds,
            "staleAfterSeconds": int(SUMMARY_STALE_AFTER.total_seconds()),
        },
        "inventory": {
            "total": len(inventory),
            "active": len(active_inventory),
            "health": {
                "healthy": health["healthy"],
                "review": health["review"],
                "unreachable": health["unreachable"],
                "unknown": health["unknown"],
            },
            "sources": {"olvm": source_counts["olvm"], "manual": source_counts["manual"]},
        },
        "collections": {
            "latest": (
                {
                    "runId": completed.get("run_id"),
                    "name": completed.get("run_name"),
                    "state": completed.get("state"),
                    "completedAt": iso_timestamp(completed_at),
                    "durationMs": completed.get("duration_ms"),
                    "source": {
                        "type": completed.get("source_type"),
                        "managerId": completed.get("manager_id"),
                        "profileId": completed.get("profile_id"),
                        "profileName": completed.get("profile_name"),
                        "path": completed.get("source_path"),
                        "commitSha": completed.get("source_commit_sha"),
                        "state": completed.get("source_state"),
                    },
                }
                if completed
                else None
            ),
            "outcomes": {
                "total": completed_total,
                "successful": successful,
                "unreachable": unreachable,
                "failed": failed,
                "queued": outcome_counts["queued"],
                "successRate": percentage(successful, attempted_total),
                "allUnreachable": all_unreachable,
            },
        },
        "capacity": {
            "hostsWithFacts": len(capacity),
            "cpuCores": cpu_cores,
            "memory": {
                "totalBytes": memory_total,
                "usedBytes": memory_used,
                "utilizationPercent": percentage(memory_used, memory_total),
            },
            "disk": {
                "totalBytes": disk_total,
                "usedBytes": disk_used,
                "utilizationPercent": percentage(disk_used, disk_total),
            },
        },
        "alerts": {
            "openCount": len(alerts),
            "bySeverity": {
                "critical": severity_counts["critical"],
                "warning": severity_counts["warning"],
                "info": severity_counts["info"],
            },
            "items": [
                {
                    "id": alert.get("id"),
                    "runId": alert.get("run_id"),
                    "hostId": alert.get("host_id"),
                    "hostName": alert.get("host_name"),
                    "severity": alert.get("severity"),
                    "state": alert.get("state"),
                    "category": alert.get("category"),
                    "summary": alert.get("summary"),
                    "observedAt": iso_timestamp(alert.get("observed_at")),
                    "mode": alert.get("execution_mode"),
                }
                for alert in alerts[:10]
            ],
        },
        "recentActivity": recent_activity,
    }


def summary() -> dict[str, Any]:
    return build_summary(load_summary_data())
