"""Stable ASGI entry point and compatibility exports for Sentinel."""

from sentinel.app import app
from sentinel.bootstrap import initialize_database
from sentinel.collections import basic_playbook_issues, create_run, simulated_result
from sentinel.config import (
    DATABASE_URL,
    DEFAULT_OLVM_PRUNE_AFTER_MISSING_RUNS,
    OLVM_LIFECYCLE_STATES,
    OLVM_RECONCILIATION_STATES,
    TABLES,
)
from sentinel.credentials import build_credential, update_credential_references
from sentinel.dependencies import get_forgejo_client
from forgejo_client import ForgejoClient
from sentinel.inventory import (
    build_authoritative_olvm_host,
    build_host,
    find_olvm_host_by_identity,
    is_olvm_host,
    manager_exists,
    migrate_olvm_provenance,
    olvm_identity_fields,
    olvm_provenance_schema_statements,
    reconcile_authoritative_olvm_host,
    record_olvm_resource_missing,
    store_host,
    store_olvm_identity,
    validate_olvm_provenance,
)
from sentinel.managers import build_manager
from sentinel.profiles import (
    build_profile,
    profile_request_path,
    profile_request_revision,
    public_profile,
    require_commit_sha,
    resolve_profile_source,
)
from sentinel.records import database_connection, delete_record, get_record, records, store_record
from sentinel.validation import (
    credential_exists,
    current_record_or_404,
    find_case_insensitive,
    host_addresses,
    normalized_positive_integer,
    require_reference,
    require_text,
    safe_identifier,
)


__all__ = [
    "DATABASE_URL",
    "DEFAULT_OLVM_PRUNE_AFTER_MISSING_RUNS",
    "OLVM_LIFECYCLE_STATES",
    "OLVM_RECONCILIATION_STATES",
    "TABLES",
    "app",
    "basic_playbook_issues",
    "build_authoritative_olvm_host",
    "build_credential",
    "build_host",
    "build_manager",
    "build_profile",
    "create_run",
    "credential_exists",
    "current_record_or_404",
    "database_connection",
    "delete_record",
    "find_case_insensitive",
    "find_olvm_host_by_identity",
    "ForgejoClient",
    "get_forgejo_client",
    "get_record",
    "host_addresses",
    "initialize_database",
    "is_olvm_host",
    "manager_exists",
    "migrate_olvm_provenance",
    "normalized_positive_integer",
    "olvm_identity_fields",
    "olvm_provenance_schema_statements",
    "profile_request_path",
    "profile_request_revision",
    "public_profile",
    "reconcile_authoritative_olvm_host",
    "record_olvm_resource_missing",
    "records",
    "require_commit_sha",
    "require_reference",
    "require_text",
    "resolve_profile_source",
    "safe_identifier",
    "simulated_result",
    "store_host",
    "store_olvm_identity",
    "store_record",
    "update_credential_references",
    "validate_olvm_provenance",
]
