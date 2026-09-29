import os


DATABASE_URL = os.environ.get("DATABASE_URL", "")


def internal_audit_log_enabled() -> bool:
    return os.environ.get("SENTINEL_INTERNAL_AUDIT_LOG_MODE", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def internal_audit_log_max_events() -> int:
    try:
        value = int(os.environ.get("SENTINEL_INTERNAL_AUDIT_LOG_MAX_EVENTS", "10000"))
    except ValueError:
        return 10000
    return min(max(value, 100), 100000)

TABLES = {
    "hosts": ("sentinel.hosts", "name"),
    "credentials": ("sentinel.credentials", "id"),
    "managers": ("sentinel.managers", "id"),
    "profiles": ("sentinel.collection_profiles", "id"),
    "runs": ("sentinel.collection_runs", "id"),
    "dashboards": ("sentinel.grafana_dashboards", "id"),
}

DEFAULT_OLVM_PRUNE_AFTER_MISSING_RUNS = 3
OLVM_LIFECYCLE_STATES = {"active", "retired"}
OLVM_RECONCILIATION_STATES = {"present", "missing", "retained", "pending-prune", "retired"}
