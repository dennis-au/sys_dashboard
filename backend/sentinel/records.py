from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from .config import DATABASE_URL, TABLES


def database_connection() -> psycopg.Connection:
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL must be configured before Sentinel can access PostgreSQL.")
    return psycopg.connect(DATABASE_URL, autocommit=True)


def records(kind: str) -> list[dict[str, Any]]:
    table_name, _ = TABLES[kind]
    with database_connection() as connection, connection.cursor() as cursor:
        cursor.execute(f"SELECT payload FROM {table_name} ORDER BY created_at, id")
        return [row[0] for row in cursor.fetchall()]


def get_record(kind: str, record_id: str) -> dict[str, Any] | None:
    table_name, _ = TABLES[kind]
    with database_connection() as connection, connection.cursor() as cursor:
        cursor.execute(f"SELECT payload FROM {table_name} WHERE id = %s", (record_id,))
        row = cursor.fetchone()
        return row[0] if row else None


def _store_record(kind: str, record: dict[str, Any], previous_id: str | None = None) -> dict[str, Any]:
    table_name, id_field = TABLES[kind]
    record_id = str(record[id_field])
    with database_connection() as connection, connection.cursor() as cursor:
        if previous_id and previous_id != record_id:
            cursor.execute(f"DELETE FROM {table_name} WHERE id = %s", (previous_id,))
        cursor.execute(
            f"""
            INSERT INTO {table_name} (id, payload) VALUES (%s, %s)
            ON CONFLICT (id) DO UPDATE SET payload = EXCLUDED.payload, updated_at = NOW()
            """,
            (record_id, Jsonb(record)),
        )
    return record


def store_record(kind: str, record: dict[str, Any], previous_id: str | None = None) -> dict[str, Any]:
    if kind == "hosts":
        from .inventory import store_host

        return store_host(record, previous_id)
    return _store_record(kind, record, previous_id)


def delete_record(kind: str, record_id: str) -> None:
    table_name, _ = TABLES[kind]
    with database_connection() as connection, connection.cursor() as cursor:
        cursor.execute(f"DELETE FROM {table_name} WHERE id = %s", (record_id,))
