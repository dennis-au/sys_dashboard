"""Structured live-run reporting records and summary calculations."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable
from uuid import uuid4

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .records import database_connection, get_record, store_record


SUMMARY_STALE_AFTER = timedelta(hours=2)
LEGACY_SIMULATION_MESSAGE = "Historical simulation record. No target or external service was contacted."
LIVE_EXECUTION_MODE = "live"
LEGACY_EXECUTION_MODE = "simulation"
RUN_STATES = frozenset(
    {
        "queued",
        "dispatched",
        "running",
        "artifact-uploaded",
        "ingesting",
        "completed",
        "partial",
        "unreachable",
        "failed",
    }
)
TERMINAL_RUN_STATES = frozenset({"completed", "partial", "unreachable", "failed"})
HOST_RESULT_STATES = frozenset({"queued", "success", "unreachable", "failed", "skipped"})


def _replace_check_constraint_statement(
    table: str, column: str, constraint: str, definition: str
) -> str:
    """Build an idempotent migration for an existing column check constraint."""

    return f"""
    DO $$
    DECLARE existing_constraint TEXT;
    BEGIN
      FOR existing_constraint IN
        SELECT constraint_row.conname
        FROM pg_constraint constraint_row
        JOIN pg_class table_row ON table_row.oid = constraint_row.conrelid
        JOIN pg_namespace table_schema ON table_schema.oid = table_row.relnamespace
        JOIN pg_attribute column_row
          ON column_row.attrelid = table_row.oid
         AND column_row.attnum = ANY (constraint_row.conkey)
        WHERE table_schema.nspname = 'sentinel'
          AND table_row.relname = '{table}'
          AND column_row.attname = '{column}'
          AND constraint_row.contype = 'c'
      LOOP
        EXECUTE format('ALTER TABLE sentinel.{table} DROP CONSTRAINT %I', existing_constraint);
      END LOOP;
      IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint constraint_row
        JOIN pg_class table_row ON table_row.oid = constraint_row.conrelid
        JOIN pg_namespace table_schema ON table_schema.oid = table_row.relnamespace
        WHERE table_schema.nspname = 'sentinel'
          AND table_row.relname = '{table}'
          AND constraint_row.conname = '{constraint}'
      ) THEN
        ALTER TABLE sentinel.{table}
          ADD CONSTRAINT {constraint} {definition};
      END IF;
    END $$
    """


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
            execution_mode TEXT NOT NULL CONSTRAINT collection_run_details_execution_mode_check CHECK (execution_mode IN ('live', 'simulation')),
            state TEXT NOT NULL CONSTRAINT collection_run_details_state_check CHECK (state IN ('queued', 'dispatched', 'running', 'artifact-uploaded', 'ingesting', 'completed', 'partial', 'unreachable', 'failed')),
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
            execution_mode TEXT NOT NULL CONSTRAINT collection_host_results_execution_mode_check CHECK (execution_mode IN ('live', 'simulation')),
            state TEXT NOT NULL CONSTRAINT collection_host_results_state_check CHECK (state IN ('queued', 'success', 'unreachable', 'failed', 'skipped')),
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
            execution_mode TEXT NOT NULL CONSTRAINT host_capacity_facts_execution_mode_check CHECK (execution_mode IN ('live', 'simulation'))
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
            execution_mode TEXT NOT NULL CONSTRAINT collection_alerts_execution_mode_check CHECK (execution_mode IN ('live', 'simulation'))
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
        # Existing development volumes were created with simulation-only checks.
        # Replace only constraints tied to these columns; row data remains intact.
        _replace_check_constraint_statement(
            "collection_run_details", "execution_mode", "collection_run_details_execution_mode_check",
            "CHECK (execution_mode IN ('live', 'simulation'))",
        ),
        _replace_check_constraint_statement(
            "collection_run_details", "state", "collection_run_details_state_check",
            "CHECK (state IN ('queued', 'dispatched', 'running', 'artifact-uploaded', 'ingesting', 'completed', 'partial', 'unreachable', 'failed'))",
        ),
        _replace_check_constraint_statement(
            "collection_host_results", "execution_mode", "collection_host_results_execution_mode_check",
            "CHECK (execution_mode IN ('live', 'simulation'))",
        ),
        _replace_check_constraint_statement(
            "collection_host_results", "state", "collection_host_results_state_check",
            "CHECK (state IN ('queued', 'success', 'unreachable', 'failed', 'skipped'))",
        ),
        _replace_check_constraint_statement(
            "host_capacity_facts", "execution_mode", "host_capacity_facts_execution_mode_check",
            "CHECK (execution_mode IN ('live', 'simulation'))",
        ),
        _replace_check_constraint_statement(
            "collection_alerts", "execution_mode", "collection_alerts_execution_mode_check",
            "CHECK (execution_mode IN ('live', 'simulation'))",
        ),
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


def reserve_live_collection(
    run: dict[str, Any],
    *,
    hosts: Iterable[dict[str, Any]] = (),
    profile: dict[str, Any] | None = None,
    manager_id: str | None = None,
    source_type: str = "inventory",
    trigger: str = "manual",
    expected_source_instances: Iterable[dict[str, str]] = (),
) -> None:
    """Reserve one live collection before an adapter contacts a source."""

    requested_at = utc_now()
    context = _run_context(profile)
    host_list = list(hosts)
    with database_connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO sentinel.collection_run_details (
                run_id, name, profile_id, profile_name, manager_id, source_type,
                source_path, source_commit_sha, source_state, execution_mode, state, requested_at, metadata
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'live', 'queued', %s, %s)
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
                    {"trigger": trigger, "expectedSourceInstances": list(expected_source_instances)}
                ),
            ),
        )
        for host in host_list:
            cursor.execute(
                """
                INSERT INTO sentinel.collection_host_results (
                    id, run_id, host_id, host_name, source_type, manager_id, profile_id,
                    source_path, source_commit_sha, source_state, execution_mode, state
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'live', 'queued')
                """,
                (
                    f"result-{uuid4().hex}",
                    str(run["id"]),
                    str(host.get("id") or host["name"]),
                    str(host["name"]),
                    str(host.get("sourceType", source_type)),
                    str(host.get("sourceManagerId")) if host.get("sourceManagerId") else manager_id,
                    context["profile_id"],
                    context["source_path"],
                    context["source_commit_sha"],
                    context["source_state"],
                ),
            )


def mark_collection_running(run_id: str) -> None:
    """Mark a reserved run as started without exposing adapter output."""

    with database_connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE sentinel.collection_run_details
            SET state = 'running', started_at = COALESCE(started_at, NOW())
            WHERE run_id = %s AND state IN ('queued', 'dispatched')
            """,
            (run_id,),
        )
    _update_legacy_run(run_id, "Running")


def record_host_outcomes(
    run_id: str,
    outcomes: Iterable[dict[str, Any]],
    *,
    manager_id: str | None = None,
) -> None:
    """Persist explicit target outcomes, without storing command output."""

    items = list(outcomes)
    if not items:
        return
    with database_connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT profile_id, source_path, source_commit_sha, source_state, source_type, manager_id
            FROM sentinel.collection_run_details
            WHERE run_id = %s
            """,
            (run_id,),
        )
        run = cursor.fetchone()
        if run is None:
            raise ValueError("Collection run does not exist.")
        profile_id, source_path, source_commit_sha, source_state, default_source_type, persisted_manager_id = run
        for item in items:
            host_id = str(item.get("host_id") or item.get("hostId") or "").strip()
            host_name = str(item.get("host_name") or item.get("hostName") or host_id).strip()
            state = str(item.get("state") or "").strip()
            if not host_id or not host_name or state not in HOST_RESULT_STATES:
                raise ValueError("Collection host outcome is invalid.")
            source_type = str(item.get("source_type") or item.get("sourceType") or default_source_type)
            source_manager_id = item.get("manager_id") or item.get("managerId") or manager_id or persisted_manager_id
            reason = item.get("reason")
            reason_text = str(reason)[:512] if isinstance(reason, str) and reason else None
            cursor.execute(
                """
                SELECT id FROM sentinel.collection_host_results
                WHERE run_id = %s AND host_id = %s
                ORDER BY id
                LIMIT 1
                """,
                (run_id, host_id),
            )
            existing = cursor.fetchone()
            if existing:
                cursor.execute(
                    """
                    UPDATE sentinel.collection_host_results
                    SET state = %s,
                        started_at = COALESCE(started_at, NOW()),
                        completed_at = CASE WHEN %s = 'queued' THEN completed_at ELSE NOW() END,
                        duration_ms = CASE
                            WHEN %s = 'queued' THEN duration_ms
                            ELSE GREATEST(0, (EXTRACT(EPOCH FROM (NOW() - COALESCE(started_at, NOW()))) * 1000)::INTEGER)
                        END,
                        reason = %s
                    WHERE id = %s
                    """,
                    (state, state, state, reason_text, existing[0]),
                )
            else:
                cursor.execute(
                    """
                    INSERT INTO sentinel.collection_host_results (
                        id, run_id, host_id, host_name, source_type, manager_id, profile_id,
                        source_path, source_commit_sha, source_state, execution_mode, state,
                        started_at, completed_at, duration_ms, reason
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'live', %s,
                        NOW(), CASE WHEN %s = 'queued' THEN NULL ELSE NOW() END,
                        CASE WHEN %s = 'queued' THEN NULL ELSE 0 END, %s
                    )
                    """,
                    (
                        f"result-{uuid4().hex}",
                        run_id,
                        host_id,
                        host_name,
                        source_type,
                        source_manager_id,
                        profile_id,
                        source_path,
                        source_commit_sha,
                        source_state,
                        state,
                        state,
                        state,
                        reason_text,
                    ),
                )


def complete_live_collection(run_id: str, state: str, *, failure_reason: str | None = None) -> None:
    """Complete one live run with a bounded operator-safe reason."""

    if state not in TERMINAL_RUN_STATES:
        raise ValueError("Collection terminal state is invalid.")
    safe_reason = str(failure_reason)[:512] if failure_reason else None
    with database_connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE sentinel.collection_run_details
            SET state = %s,
                started_at = COALESCE(started_at, requested_at),
                completed_at = NOW(),
                duration_ms = GREATEST(0, (EXTRACT(EPOCH FROM (NOW() - COALESCE(started_at, requested_at))) * 1000)::INTEGER),
                failure_reason = %s
            WHERE run_id = %s
            """,
            (state, safe_reason, run_id),
        )
    labels = {
        "completed": "Completed",
        "partial": "Partial",
        "unreachable": "Unreachable",
        "failed": "Failed",
    }
    _update_legacy_run(run_id, labels[state], safe_reason)


def reconcile_terminal_live_run_activity() -> None:
    """Repair legacy activity rows left running by an earlier live worker build."""

    with database_connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            WITH terminal_runs AS (
                SELECT
                    details.run_id,
                    CASE details.state
                        WHEN 'completed' THEN 'Completed'
                        WHEN 'partial' THEN 'Partial'
                        WHEN 'unreachable' THEN 'Unreachable'
                        WHEN 'failed' THEN 'Failed'
                    END AS status,
                    CASE
                        WHEN details.failure_reason IS NOT NULL AND BTRIM(details.failure_reason) <> ''
                            THEN details.failure_reason
                        WHEN details.state = 'completed' THEN 'Live collection completed.'
                        ELSE 'Live collection completed with target issues.'
                    END AS summary,
                    CASE WHEN details.state = 'completed' THEN 'success' ELSE 'warn' END AS type,
                    TO_CHAR(
                        COALESCE(details.completed_at, details.requested_at) AT TIME ZONE 'UTC',
                        'YYYY-MM-DD"T"HH24:MI:SS"Z"'
                    ) AS completed_at
                FROM sentinel.collection_run_details AS details
                WHERE details.execution_mode = 'live'
                  AND details.state IN ('completed', 'partial', 'unreachable', 'failed')
            )
            UPDATE sentinel.collection_runs AS legacy
            SET payload = jsonb_set(
                    jsonb_set(
                        jsonb_set(
                            jsonb_set(legacy.payload, '{status}', to_jsonb(terminal_runs.status), true),
                            '{summary}', to_jsonb(terminal_runs.summary), true
                        ),
                        '{type}', to_jsonb(terminal_runs.type), true
                    ),
                    '{date}', to_jsonb(terminal_runs.completed_at), true
                ),
                updated_at = NOW()
            FROM terminal_runs
            WHERE legacy.id = terminal_runs.run_id
              AND legacy.payload->>'status' IN ('Queued', 'Running')
            """
        )


def _update_legacy_run(run_id: str, status: str, failure_reason: str | None = None) -> None:
    """Keep the existing Collections activity surface aligned with live run state."""

    run = get_record("runs", run_id)
    if run is None:
        return
    updated = dict(run)
    updated["status"] = status
    updated["date"] = iso_timestamp(utc_now())
    if failure_reason:
        updated["summary"] = failure_reason
        updated["type"] = "warn"
    elif status == "Completed":
        updated["summary"] = "Live collection completed."
        updated["type"] = "success"
    elif status in {"Partial", "Unreachable", "Failed"}:
        updated["summary"] = "Live collection completed with target issues."
        updated["type"] = "warn"
    store_record("runs", updated)


def claim_queued_profile_runs(limit: int = 8) -> list[dict[str, Any]]:
    """Claim queued profile runs so only the supervised worker can execute them."""

    if limit < 1:
        return []
    with database_connection() as connection, connection.transaction(), connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            """
            WITH queued AS (
                SELECT run_id
                FROM sentinel.collection_run_details
                WHERE execution_mode = 'live'
                  AND state = 'queued'
                  AND profile_id IS NOT NULL
                ORDER BY requested_at, run_id
                FOR UPDATE SKIP LOCKED
                LIMIT %s
            )
            UPDATE sentinel.collection_run_details AS details
            SET state = 'dispatched'
            FROM queued
            WHERE details.run_id = queued.run_id
            RETURNING details.run_id, details.profile_id, details.name
            """,
            (limit,),
        )
        return list(cursor.fetchall())


def _rows(query: str) -> list[dict[str, Any]]:
    with database_connection() as connection, connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(query)
        return list(cursor.fetchall())


def _capacity_label(used: int | None, total: int | None) -> str | None:
    if used is None or total is None:
        return None
    value = percentage(used, total)
    return f"{value:g}%" if value is not None else None


def _operating_system_label(row: dict[str, Any]) -> str | None:
    distribution = str(row.get("distribution") or "").strip()
    version = str(row.get("distribution_version") or "").strip()
    architecture = str(row.get("architecture") or "").strip()
    name = " ".join(part for part in (distribution, version) if part)
    if not name:
        return None
    return f"{name} · {architecture}" if architecture else name


def inventory_read_model() -> list[dict[str, Any]]:
    """Join host configuration to accepted operational projections for the portal.

    Host ownership, lifecycle, and connection settings remain in ``sentinel.hosts``.
    Collection state and observations are read-only projections keyed by the
    immutable Sentinel host ID.
    """

    query = """
        SELECT
          hosts.id AS host_id,
          hosts.payload AS host_payload,
          latest_result.run_id AS collection_run_id,
          latest_result.state AS collection_state,
          latest_result.completed_at AS collection_completed_at,
          system_fact.distribution,
          system_fact.distribution_version,
          system_fact.architecture,
          capacity.memory_total_bytes,
          capacity.memory_used_bytes,
          capacity.disk_total_bytes,
          capacity.disk_used_bytes
        FROM sentinel.hosts AS hosts
        LEFT JOIN LATERAL (
          SELECT run_id, state, completed_at
          FROM sentinel.collection_host_results
          WHERE host_id = hosts.id
            AND execution_mode = 'live'
            AND state <> 'queued'
            AND completed_at IS NOT NULL
          ORDER BY completed_at DESC, id DESC
          LIMIT 1
        ) AS latest_result ON TRUE
        LEFT JOIN sentinel.linux_system_current AS system_fact
          ON system_fact.host_id = hosts.id
        LEFT JOIN LATERAL (
          SELECT memory_total_bytes, memory_used_bytes, disk_total_bytes, disk_used_bytes
          FROM sentinel.host_capacity_facts
          WHERE host_id = hosts.id
            AND execution_mode = 'live'
          ORDER BY observed_at DESC, id DESC
          LIMIT 1
        ) AS capacity ON TRUE
        ORDER BY hosts.payload->>'name', hosts.id
    """
    with database_connection() as connection, connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(query)
        rows = list(cursor.fetchall())

    inventory: list[dict[str, Any]] = []
    for row in rows:
        host = dict(row["host_payload"])
        host["id"] = str(row["host_id"])
        completed_at = iso_timestamp(row["collection_completed_at"])
        collection_state = row["collection_state"]
        host["collection"] = {
            "state": collection_state or "not_collected",
            "collectedAt": completed_at,
            "runId": row["collection_run_id"],
        }
        host["collected"] = completed_at or "Not collected"

        operating_system = _operating_system_label(row)
        if operating_system:
            host["os"] = operating_system
        memory = _capacity_label(row["memory_used_bytes"], row["memory_total_bytes"])
        disk = _capacity_label(row["disk_used_bytes"], row["disk_total_bytes"])
        if memory:
            host["memory"] = memory
        if disk:
            host["disk"] = disk

        if collection_state == "unreachable":
            host["status"] = "unreachable"
        elif collection_state in {"failed", "skipped"}:
            host["status"] = "review"
        inventory.append(host)
    return inventory


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
        if run.get("execution_mode") == LIVE_EXECUTION_MODE
        and run.get("state") in TERMINAL_RUN_STATES
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

    collected_run = next(
        (
            run
            for run in sorted(
                runs,
                key=lambda row: as_utc(row.get("completed_at"))
                or datetime.min.replace(tzinfo=timezone.utc),
                reverse=True,
            )
            if run.get("execution_mode") == LIVE_EXECUTION_MODE
            and run.get("state") in TERMINAL_RUN_STATES
            and run.get("completed_at") is not None
        ),
        None,
    )
    completed_at = as_utc(collected_run.get("completed_at")) if collected_run else None
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

    if completed is None:
        mode = {
            "kind": "unavailable",
            "message": "No completed live collection is available.",
        }
    elif completed.get("state") == "completed":
        mode = {
            "kind": "operational",
            "message": "Reporting is based on accepted live collection data.",
        }
    else:
        mode = {
            "kind": "degraded",
            "message": "The latest live collection completed with unavailable or partial targets.",
        }

    return {
        "generatedAt": iso_timestamp(generated_at),
        "mode": mode,
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
