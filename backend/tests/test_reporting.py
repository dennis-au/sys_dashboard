from datetime import datetime, timezone
import os
import sys
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from sentinel.reporting import build_summary
from sentinel.bootstrap import initialize_database


NOW = datetime(2026, 9, 28, 10, 0, tzinfo=timezone.utc)


def reporting_data(**overrides):
    data = {"inventory": [], "runs": [], "results": [], "capacity": [], "alerts": []}
    data.update(overrides)
    return data


def test_summary_reports_sensible_empty_state_without_fixture_display_strings():
    result = build_summary(reporting_data(), now=NOW)

    assert result["mode"] == {
        "kind": "unavailable",
        "message": "No completed live collection is available.",
    }
    assert result["freshness"] == {
        "state": "unavailable",
        "latestCollectedAt": None,
        "ageSeconds": None,
        "staleAfterSeconds": 7200,
    }
    assert result["inventory"]["total"] == 0
    assert result["collections"]["latest"] is None
    assert result["collections"]["outcomes"]["successRate"] is None
    assert result["capacity"]["hostsWithFacts"] == 0
    assert result["alerts"]["items"] == []


def test_summary_uses_structured_partial_results_capacity_and_alerts():
    run = {
        "run_id": "run-1",
        "run_name": "Collection",
        "state": "partial",
        "execution_mode": "live",
        "requested_at": "2026-09-28T09:40:00Z",
        "completed_at": "2026-09-28T09:45:00Z",
        "duration_ms": 300000,
        "source_type": "inventory",
        "profile_id": "profile-1",
        "profile_name": "Facts",
        "source_path": "inventory/facts.yml",
        "source_commit_sha": "a" * 40,
        # This deliberately contradictory legacy field must be ignored.
        "display_summary": "999 successful · 0 unreachable",
    }
    result = build_summary(
        reporting_data(
            inventory=[
                {"host_id": "one", "host_name": "one", "source_type": "olvm", "lifecycle": "active", "inventory_status": "healthy"},
                {"host_id": "two", "host_name": "two", "source_type": "manual", "lifecycle": "active", "inventory_status": "review"},
                {"host_id": "old", "host_name": "old", "source_type": "manual", "lifecycle": "decommissioned", "inventory_status": "healthy"},
            ],
            runs=[run],
            results=[
                {"run_id": "run-1", "state": "success"},
                {"run_id": "run-1", "state": "unreachable"},
            ],
            capacity=[
                {"cpu_cores": 4, "memory_total_bytes": 100, "memory_used_bytes": 55, "disk_total_bytes": 200, "disk_used_bytes": 150}
            ],
            alerts=[
                {"id": "alert-1", "severity": "warning", "state": "open", "category": "capacity.disk", "summary": "Disk review", "observed_at": "2026-09-28T09:45:00Z", "execution_mode": "live"}
            ],
        ),
        now=NOW,
    )

    assert result["freshness"]["state"] == "fresh"
    assert result["mode"]["kind"] == "degraded"
    assert result["inventory"]["total"] == 3
    assert result["inventory"]["active"] == 2
    assert result["inventory"]["health"] == {"healthy": 1, "review": 1, "unreachable": 0, "unknown": 0}
    assert result["inventory"]["sources"] == {"olvm": 1, "manual": 1}
    assert result["collections"]["outcomes"] == {
        "total": 2,
        "successful": 1,
        "unreachable": 1,
        "failed": 0,
        "queued": 0,
        "successRate": 50.0,
        "allUnreachable": False,
    }
    assert result["collections"]["latest"]["source"]["commitSha"] == "a" * 40
    assert result["capacity"]["memory"]["utilizationPercent"] == 55.0
    assert result["capacity"]["disk"]["utilizationPercent"] == 75.0
    assert result["alerts"]["bySeverity"] == {"critical": 0, "warning": 1, "info": 0}


def test_summary_marks_all_unreachable_and_stale_from_timestamps_not_copy():
    result = build_summary(
        reporting_data(
            runs=[
                {
                    "run_id": "run-2",
                    "run_name": "Collection",
                    "state": "unreachable",
                    "execution_mode": "live",
                    "requested_at": "2026-09-28T05:00:00Z",
                    "completed_at": "2026-09-28T05:01:00Z",
                    "source_type": "inventory",
                }
            ],
            results=[
                {"run_id": "run-2", "state": "unreachable"},
                {"run_id": "run-2", "state": "unreachable"},
            ],
        ),
        now=NOW,
    )

    assert result["freshness"]["state"] == "stale"
    assert result["mode"]["kind"] == "degraded"
    assert result["freshness"]["ageSeconds"] == 17940
    assert result["collections"]["outcomes"]["allUnreachable"] is True
    assert result["collections"]["outcomes"]["successRate"] == 0.0


def test_summary_ignores_queued_runs_for_latest_completed_collection():
    result = build_summary(
        reporting_data(
            runs=[
                {
                    "run_id": "queued",
                    "run_name": "Queued collection",
                    "state": "queued",
                    "execution_mode": "live",
                    "requested_at": "2026-09-28T09:59:00Z",
                    "source_type": "inventory",
                },
                {
                    "run_id": "completed",
                    "run_name": "Completed collection",
                    "state": "completed",
                    "execution_mode": "live",
                    "requested_at": "2026-09-28T09:00:00Z",
                    "completed_at": "2026-09-28T09:01:00Z",
                    "source_type": "inventory",
                },
            ],
            results=[
                {"run_id": "queued", "state": "queued"},
                {"run_id": "completed", "state": "success"},
            ],
        ),
        now=NOW,
    )

    assert result["collections"]["latest"]["runId"] == "completed"
    assert result["mode"]["kind"] == "operational"
    assert result["collections"]["outcomes"]["successful"] == 1
    assert result["recentActivity"][0]["runId"] == "queued"


def test_startup_replaces_legacy_reporting_views_before_adding_new_columns():
    """A pre-reporting development volume must not prevent the API from starting."""

    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as connection, connection.cursor() as cursor:
        cursor.execute("DROP VIEW IF EXISTS reporting.host_health")
        cursor.execute("DROP VIEW IF EXISTS reporting.inventory_health")
        cursor.execute("DROP VIEW IF EXISTS reporting.open_alerts")
        cursor.execute("DROP VIEW IF EXISTS reporting.host_capacity_latest")
        cursor.execute("DROP VIEW IF EXISTS reporting.host_collection_results")
        cursor.execute("DROP VIEW IF EXISTS reporting.collection_runs")
        cursor.execute(
            """
            CREATE VIEW reporting.collection_runs AS
            SELECT
              payload->>'name' AS run_name,
              payload->>'date' AS run_date,
              payload->>'summary' AS summary,
              payload->>'status' AS status
            FROM sentinel.collection_runs
            """
        )
        cursor.execute(
            """
            CREATE VIEW reporting.host_health AS
            SELECT
              payload->>'name' AS host_name,
              payload->>'sourceType' AS source_type,
              payload->>'environment' AS environment,
              payload->>'status' AS status,
              payload->>'collected' AS last_collection
            FROM sentinel.hosts
            """
        )

    initialize_database()

    with psycopg.connect(os.environ["DATABASE_URL"]) as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'reporting' AND table_name = 'collection_runs'
            ORDER BY ordinal_position
            """
        )
        assert [row[0] for row in cursor.fetchall()] == [
            "run_id",
            "run_name",
            "profile_id",
            "profile_name",
            "manager_id",
            "source_type",
            "source_path",
            "source_commit_sha",
            "source_state",
            "execution_mode",
            "state",
            "requested_at",
            "started_at",
            "completed_at",
            "duration_ms",
            "failure_reason",
            "display_summary",
        ]


def test_startup_reconciles_terminal_live_runs_left_running_in_legacy_activity():
    run_id = "reporting-legacy-activity-reconciliation-regression"
    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as connection, connection.cursor() as cursor:
        try:
            cursor.execute("DELETE FROM sentinel.collection_run_details WHERE run_id = %s", (run_id,))
            cursor.execute("DELETE FROM sentinel.collection_runs WHERE id = %s", (run_id,))
            cursor.execute(
                "INSERT INTO sentinel.collection_runs (id, payload) VALUES (%s, %s)",
                (
                    run_id,
                    Jsonb(
                        {
                            "id": run_id,
                            "name": "reporting legacy reconciliation regression",
                            "date": "Just now",
                            "summary": "Manual profile run queued for worker execution",
                            "status": "Running",
                            "type": "success",
                        }
                    ),
                ),
            )
            cursor.execute(
                """
                INSERT INTO sentinel.collection_run_details (
                    run_id, name, source_type, execution_mode, state, requested_at, completed_at
                ) VALUES (%s, %s, 'profile', 'live', 'partial', NOW() - INTERVAL '1 minute', NOW())
                """,
                (run_id, "reporting legacy reconciliation regression"),
            )

            initialize_database()

            cursor.execute("SELECT payload FROM sentinel.collection_runs WHERE id = %s", (run_id,))
            payload = cursor.fetchone()[0]
            assert payload["status"] == "Partial"
            assert payload["summary"] == "Live collection completed with target issues."
            assert payload["type"] == "warn"
        finally:
            cursor.execute("DELETE FROM sentinel.collection_run_details WHERE run_id = %s", (run_id,))
            cursor.execute("DELETE FROM sentinel.collection_runs WHERE id = %s", (run_id,))
