import os
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import quote


def configured_database_url(environment: Mapping[str, str] | None = None) -> str:
    """Resolve either an explicit URL or a password-file-backed PostgreSQL URL."""

    values = os.environ if environment is None else environment
    explicit_url = values.get("DATABASE_URL", "").strip()
    if explicit_url:
        return explicit_url

    password_file = values.get("DATABASE_PASSWORD_FILE", "").strip()
    if not password_file:
        return ""
    try:
        password = Path(password_file).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError("DATABASE_PASSWORD_FILE could not be read.") from exc
    if not password:
        raise RuntimeError("DATABASE_PASSWORD_FILE must contain a password.")

    host = values.get("DATABASE_HOST", "postgres").strip() or "postgres"
    port = values.get("DATABASE_PORT", "5432").strip() or "5432"
    database = values.get("DATABASE_NAME", "sentinel_db").strip() or "sentinel_db"
    user = values.get("DATABASE_USER", "sentinel").strip() or "sentinel"
    return f"postgresql://{quote(user, safe='')}:{quote(password, safe='')}@{host}:{port}/{quote(database, safe='')}"


DATABASE_URL = configured_database_url()
INTERNAL_AUDIT_LOG_MAX_EVENTS = 10_000

TABLES = {
    "hosts": ("sentinel.hosts", "id"),
    "credentials": ("sentinel.credentials", "id"),
    "managers": ("sentinel.managers", "id"),
    "profiles": ("sentinel.collection_profiles", "id"),
    "runs": ("sentinel.collection_runs", "id"),
    "dashboards": ("sentinel.grafana_dashboards", "id"),
}

DEFAULT_OLVM_PRUNE_AFTER_MISSING_RUNS = 3
OLVM_LIFECYCLE_STATES = {"active", "retired"}
OLVM_RECONCILIATION_STATES = {"present", "missing", "retained", "pending-prune", "retired"}
