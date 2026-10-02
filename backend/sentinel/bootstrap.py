from playbook_migration import (
    PlaybookMigrationError,
    migrate_database_profiles,
    schema_statements as playbook_migration_schema_statements,
)

from .audit import audit_schema_statements
from .config import DATABASE_URL, TABLES
from .credentials import migrate_legacy_secret_references
from .inventory import migrate_host_identities, migrate_olvm_provenance, olvm_provenance_schema_statements
from .ingestion import apply_ingestion_migration, apply_linux_system_fact_migration
from .records import database_connection
from .reporting import reconcile_terminal_live_run_activity, reporting_schema_statements
from .scheduling import migrate_profile_schedules, scheduler_schema_statements


def initialize_database() -> None:
    statements = [
        "CREATE SCHEMA IF NOT EXISTS sentinel",
        "CREATE SCHEMA IF NOT EXISTS reporting",
    ]
    statements.extend(
        f"""
        CREATE TABLE IF NOT EXISTS {table_name} (
            id TEXT PRIMARY KEY,
            payload JSONB NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
        for table_name, _ in TABLES.values()
    )
    statements.extend(playbook_migration_schema_statements())
    statements.extend(audit_schema_statements())
    statements.extend(olvm_provenance_schema_statements())
    statements.extend(reporting_schema_statements())
    statements.extend(scheduler_schema_statements())
    with database_connection() as connection, connection.cursor() as cursor:
        for statement in statements:
            cursor.execute(statement)
        apply_ingestion_migration(cursor)
        apply_linux_system_fact_migration(cursor)
    migrate_legacy_secret_references()
    migrate_host_identities()
    migrate_olvm_provenance()
    migrate_profile_schedules()
    reconcile_terminal_live_run_activity()
    try:
        migrate_database_profiles(DATABASE_URL)
    except PlaybookMigrationError as exc:
        raise RuntimeError(f"Playbook source migration is incomplete: {exc}") from exc
