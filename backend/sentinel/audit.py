"""Internal, machine-readable diagnostic events with strict secret redaction."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import traceback as traceback_module
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .config import internal_audit_log_enabled, internal_audit_log_max_events
from .records import database_connection


LOGGER = logging.getLogger(__name__)
_SENSITIVE_KEY = re.compile(
    r"(?:api[_-]?key|authorization|credential|password|passwd|private[_-]?key|secret|token)",
    re.IGNORECASE,
)
_REDACTION_RULES = (
    re.compile(r"(?i)(bearer\s+)[^\s,;]+"),
    re.compile(r"(?i)([\"']?\b(?:api[_-]?key|authorization|credential|password|passwd|private[_-]?key|secret|token)\b[\"']?\s*(?:=|:|%3[dD])\s*)[^\s,;&]+"),
    re.compile(r"(?i)([?&](?:api[_-]?key|password|passwd|secret|token)=)[^&\s]+"),
    re.compile(r"(?i)(postgres(?:ql)?://)[^/@\s]+@"),
)


def audit_schema_statements() -> list[str]:
    return [
        """
        CREATE TABLE IF NOT EXISTS sentinel.internal_audit_events (
            id TEXT PRIMARY KEY,
            fingerprint TEXT NOT NULL UNIQUE,
            first_occurred_at TIMESTAMPTZ NOT NULL,
            last_occurred_at TIMESTAMPTZ NOT NULL,
            occurrence_count INTEGER NOT NULL DEFAULT 1 CHECK (occurrence_count > 0),
            service TEXT NOT NULL,
            event_kind TEXT NOT NULL,
            request_id TEXT,
            route TEXT,
            method TEXT,
            status_code INTEGER,
            exception_type TEXT,
            message TEXT,
            traceback TEXT,
            context JSONB NOT NULL DEFAULT '{}'::jsonb
        )
        """,
        "CREATE INDEX IF NOT EXISTS internal_audit_events_last_occurred_at_idx ON sentinel.internal_audit_events (last_occurred_at DESC, id DESC)",
    ]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _redact_text(value: Any, *, maximum_length: int = 24000) -> str:
    text = str(value)
    for pattern in _REDACTION_RULES:
        text = pattern.sub(lambda match: f"{match.group(1)}[redacted]", text)
    return text[:maximum_length]


def _sanitize(value: Any, *, depth: int = 0) -> Any:
    if depth >= 5:
        return "[truncated]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _redact_text(value, maximum_length=4000)
    if isinstance(value, dict):
        return {
            str(key): "[redacted]" if _SENSITIVE_KEY.search(str(key)) else _sanitize(item, depth=depth + 1)
            for key, item in list(value.items())[:50]
        }
    if isinstance(value, (list, tuple, set)):
        return [_sanitize(item, depth=depth + 1) for item in list(value)[:50]]
    return _redact_text(repr(value), maximum_length=4000)


def _fingerprint(
    *,
    service: str,
    event_kind: str,
    route: str | None,
    method: str | None,
    status_code: int | None,
    exception_type: str | None,
    message: str,
) -> str:
    payload = "\x1f".join(
        [service, event_kind, route or "", method or "", str(status_code or ""), exception_type or "", message]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def record_event(
    *,
    service: str,
    event_kind: str,
    request_id: str | None = None,
    route: str | None = None,
    method: str | None = None,
    status_code: int | None = None,
    exception_type: str | None = None,
    message: Any = "",
    traceback: Any = "",
    context: dict[str, Any] | None = None,
) -> None:
    """Record one redacted event without allowing diagnostics to affect runtime behavior."""

    if not internal_audit_log_enabled():
        return
    message_value = message if isinstance(message, str) else _sanitize(message)
    sanitized_message = _redact_text(message_value, maximum_length=4000)
    sanitized_traceback = _redact_text(traceback, maximum_length=24000)
    sanitized_context = _sanitize(context or {})
    fingerprint = _fingerprint(
        service=service,
        event_kind=event_kind,
        route=route,
        method=method,
        status_code=status_code,
        exception_type=exception_type,
        message=sanitized_message,
    )
    occurred_at = _utc_now()
    try:
        with database_connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO sentinel.internal_audit_events (
                    id, fingerprint, first_occurred_at, last_occurred_at, occurrence_count,
                    service, event_kind, request_id, route, method, status_code,
                    exception_type, message, traceback, context
                ) VALUES (%s, %s, %s, %s, 1, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (fingerprint) DO UPDATE SET
                    last_occurred_at = EXCLUDED.last_occurred_at,
                    occurrence_count = sentinel.internal_audit_events.occurrence_count + 1,
                    request_id = EXCLUDED.request_id,
                    status_code = EXCLUDED.status_code,
                    exception_type = EXCLUDED.exception_type,
                    message = EXCLUDED.message,
                    traceback = EXCLUDED.traceback,
                    context = EXCLUDED.context
                """,
                (
                    f"audit-{uuid4().hex}",
                    fingerprint,
                    occurred_at,
                    occurred_at,
                    service,
                    event_kind,
                    request_id,
                    route,
                    method,
                    status_code,
                    exception_type,
                    sanitized_message,
                    sanitized_traceback or None,
                    Jsonb(sanitized_context),
                ),
            )
            cursor.execute(
                """
                DELETE FROM sentinel.internal_audit_events
                WHERE id IN (
                    SELECT id
                    FROM sentinel.internal_audit_events
                    ORDER BY last_occurred_at DESC, id DESC
                    OFFSET %s
                )
                """,
                (internal_audit_log_max_events(),),
            )
    except Exception:
        LOGGER.error("Internal audit event persistence failed for %s", event_kind)


def record_exception(
    exception: BaseException,
    *,
    service: str,
    event_kind: str,
    request_id: str | None = None,
    route: str | None = None,
    method: str | None = None,
    status_code: int | None = None,
    context: dict[str, Any] | None = None,
) -> None:
    record_event(
        service=service,
        event_kind=event_kind,
        request_id=request_id,
        route=route,
        method=method,
        status_code=status_code,
        exception_type=type(exception).__name__,
        message=str(exception),
        traceback="".join(traceback_module.format_exception(type(exception), exception, exception.__traceback__)),
        context=context,
    )


def export_events(limit: int = 1000) -> list[dict[str, Any]]:
    """Return the newest redacted events for local NDJSON export."""

    normalized_limit = min(max(limit, 1), 100000)
    with database_connection() as connection, connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            """
            SELECT id, fingerprint, first_occurred_at, last_occurred_at, occurrence_count,
                   service, event_kind, request_id, route, method, status_code,
                   exception_type, message, traceback, context
            FROM sentinel.internal_audit_events
            ORDER BY last_occurred_at DESC, id DESC
            LIMIT %s
            """,
            (normalized_limit,),
        )
        return list(cursor.fetchall())


def ndjson_events(limit: int = 1000) -> str:
    records = export_events(limit)
    return "\n".join(
        json.dumps(
            {
                **record,
                "first_occurred_at": record["first_occurred_at"].astimezone(timezone.utc).isoformat(),
                "last_occurred_at": record["last_occurred_at"].astimezone(timezone.utc).isoformat(),
            },
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        for record in records
    )
