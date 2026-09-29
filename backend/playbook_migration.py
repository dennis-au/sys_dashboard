"""One-time migration of collection profile source from JSONB to Forgejo.

The migration intentionally stages legacy source on a branch and opens a pull
request. ``main`` stays protected throughout; the migrated profile pins the
branch commit and records that the source is awaiting review. PostgreSQL keeps
the original payload only in the migration backup table so an operator can
roll back before the review is accepted.
"""

from __future__ import annotations

import argparse
import hashlib
import os
from dataclasses import dataclass
from typing import Any, Iterable
from uuid import uuid4

import psycopg
from psycopg.types.json import Jsonb

from forgejo_client import (
    ForgejoClient,
    ForgejoConfigurationError,
    ForgejoError,
    ForgejoPullRequest,
    ForgejoRepository,
    ForgejoRequestError,
)


MIGRATION_MODE_ENV = "SENTINEL_PLAYBOOK_MIGRATION_RESET"
MIGRATION_STAGING = "staging"
MIGRATION_PENDING_REVIEW = "pending_review"
MIGRATION_ROLLED_BACK = "rolled_back"
MIGRATION_FINALIZED = "finalized"


class PlaybookMigrationError(RuntimeError):
    """A safe, actionable error while moving legacy playbook source."""


@dataclass(frozen=True)
class StagedPlaybooks:
    repository: ForgejoRepository
    branch: str
    commit_sha: str
    pull_request: ForgejoPullRequest


@dataclass(frozen=True)
class FinalizedPlaybooks:
    repository: ForgejoRepository
    commit_sha: str
    pull_request: ForgejoPullRequest
    profiles: list[dict[str, Any]]


def schema_statements() -> list[str]:
    """Return storage for a recoverable, resumable JSONB-to-Git migration."""
    return [
        """
        CREATE TABLE IF NOT EXISTS sentinel.playbook_migration_batches (
            id TEXT PRIMARY KEY,
            branch TEXT NOT NULL UNIQUE,
            state TEXT NOT NULL CONSTRAINT playbook_migration_batches_state_check
              CHECK (state IN ('staging', 'pending_review', 'rolled_back', 'finalized')),
            repository TEXT,
            commit_sha TEXT,
            pull_request_number INTEGER,
            merge_commit_sha TEXT,
            merged_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """,
        "ALTER TABLE sentinel.playbook_migration_batches DROP CONSTRAINT IF EXISTS playbook_migration_batches_state_check",
        """
        ALTER TABLE sentinel.playbook_migration_batches
        ADD CONSTRAINT playbook_migration_batches_state_check
        CHECK (state IN ('staging', 'pending_review', 'rolled_back', 'finalized'))
        """,
        "ALTER TABLE sentinel.playbook_migration_batches ADD COLUMN IF NOT EXISTS merge_commit_sha TEXT",
        "ALTER TABLE sentinel.playbook_migration_batches ADD COLUMN IF NOT EXISTS merged_at TIMESTAMPTZ",
        """
        CREATE TABLE IF NOT EXISTS sentinel.collection_profile_playbook_backups (
            batch_id TEXT NOT NULL REFERENCES sentinel.playbook_migration_batches(id),
            profile_id TEXT NOT NULL,
            legacy_payload JSONB NOT NULL,
            source_sha256 TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (batch_id, profile_id)
        )
        """,
    ]


def profile_has_legacy_source(profile: dict[str, Any]) -> bool:
    return isinstance(profile.get("playbookContent"), str)


def profile_source_metadata(profile: dict[str, Any]) -> dict[str, Any]:
    source = profile.get("source")
    return source if isinstance(source, dict) else {}


def _require_legacy_source(profile: dict[str, Any]) -> str:
    source = profile.get("playbookContent")
    if not isinstance(source, str):
        raise PlaybookMigrationError(
            f"Collection profile {profile.get('id', '<unknown>')} has no recoverable legacy playbook source."
        )
    return source


def _require_profile_path(profile: dict[str, Any]) -> str:
    path = profile.get("playbook")
    if not isinstance(path, str) or not path.strip():
        raise PlaybookMigrationError(
            f"Collection profile {profile.get('id', '<unknown>')} has no valid playbook path."
        )
    # Source bytes must never pass through a normalizing helper. A path is
    # metadata, so removing accidental outer whitespace is safe here.
    return path.strip()


def _require_commit_sha(commit_sha: str) -> str:
    normalized = commit_sha.strip().lower()
    if len(normalized) != 40 or any(character not in "0123456789abcdef" for character in normalized):
        raise PlaybookMigrationError("Forgejo did not return an immutable commit SHA for the migration.")
    return normalized


def _profile_migration_branch(batch_id: str) -> str:
    return f"migration/playbook-source-{batch_id[:12]}"


def _existing_file_matches(client: ForgejoClient, path: str, branch: str, expected: bytes) -> bool:
    try:
        existing = client.read_file(path, branch)
    except ForgejoRequestError as exc:
        if exc.status_code == 404:
            return False
        raise
    if existing.content != expected:
        raise PlaybookMigrationError(
            f"Migration branch already contains different content for {path}; legacy source was not replaced."
        )
    return True


def _ensure_branch(client: ForgejoClient, branch: str, default_branch: str) -> None:
    try:
        client.create_branch(branch, default_branch)
    except ForgejoRequestError as exc:
        if exc.status_code != 409:
            raise
        # A retry after an interrupted migration must reuse only a real,
        # reachable branch, never assume a 409 means that it is usable.
        client.revision(branch)


def stage_legacy_playbooks(
    client: ForgejoClient,
    profiles: Iterable[dict[str, Any]],
    batch_id: str,
    branch: str,
    existing_pull_request: ForgejoPullRequest | None = None,
) -> StagedPlaybooks:
    """Copy source bytes to a review branch and return its immutable commit."""
    profiles_to_stage = list(profiles)
    if not profiles_to_stage:
        raise PlaybookMigrationError("No legacy playbooks are available to migrate.")

    try:
        repository = client.repository_metadata()
        if not repository.private:
            raise PlaybookMigrationError("Forgejo playbook repository must remain private during migration.")
        _ensure_branch(client, branch, repository.default_branch)

        for profile in profiles_to_stage:
            source = _require_legacy_source(profile)
            path = _require_profile_path(profile)
            source_bytes = source.encode("utf-8")
            if _existing_file_matches(client, path, branch, source_bytes):
                continue
            client.create_or_update_file(
                path,
                branch,
                source_bytes,
                f"Migrate collection profile {profile.get('id', path)}",
            )

        revision = client.revision(branch)
        commit_sha = _require_commit_sha(revision.sha)
        pull_request = existing_pull_request or client.find_open_pull_request(branch, repository.default_branch)
        if pull_request is None:
            pull_request = client.create_pull_request(
                branch,
                repository.default_branch,
                "Migrate Sentinel collection playbooks",
                "Initial migration of legacy Sentinel profile source. Review before merging into protected main.",
            )
        return StagedPlaybooks(
            repository=repository,
            branch=branch,
            commit_sha=commit_sha,
            pull_request=pull_request,
        )
    except PlaybookMigrationError:
        raise
    except ForgejoConfigurationError as exc:
        raise PlaybookMigrationError("Forgejo migration credential is unavailable.") from exc
    except ForgejoError as exc:
        raise PlaybookMigrationError("Forgejo is unavailable or rejected the playbook migration.") from exc


def migrated_profile_payload(
    profile: dict[str, Any],
    staged: StagedPlaybooks,
    batch_id: str,
) -> dict[str, Any]:
    """Replace one legacy profile's source text with stable source metadata."""
    path = _require_profile_path(profile)
    _require_legacy_source(profile)
    migrated = dict(profile)
    migrated.pop("playbookContent", None)
    migrated["playbook"] = path  # Current portal compatibility; path is also in source metadata.
    migrated["revision"] = staged.commit_sha
    migrated["source"] = {
        "repository": staged.repository.full_name,
        "path": path,
        "commitSha": staged.commit_sha,
        "state": "migration-pending-review",
        "migration": {
            "batchId": batch_id,
            "branch": staged.branch,
            "pullRequestNumber": staged.pull_request.number,
        },
    }
    return migrated


def _migration_backup_source(backup: dict[str, Any]) -> bytes:
    payload = backup.get("payload")
    if not isinstance(payload, dict):
        raise PlaybookMigrationError("Playbook migration backup is unavailable.")
    return _require_legacy_source(payload).encode("utf-8")


def finalize_migrated_profiles(
    client: ForgejoClient,
    batch: dict[str, Any],
    profiles: Iterable[dict[str, Any]],
    backups: Iterable[dict[str, Any]],
) -> FinalizedPlaybooks:
    """Repin a reviewed migration on immutable protected-main content.

    This function only observes the pull request and repository.  It cannot
    approve or merge a PR, and it verifies every legacy byte sequence before
    discarding the pending-review branch metadata from a profile.
    """
    batch_id = batch.get("id")
    branch = batch.get("branch")
    pull_request_number = batch.get("pull_request_number")
    if not isinstance(batch_id, str) or not batch_id or not isinstance(branch, str) or not branch:
        raise PlaybookMigrationError("Playbook migration batch metadata is unavailable.")
    if not isinstance(pull_request_number, int) or pull_request_number <= 0:
        raise PlaybookMigrationError("Playbook migration pull request metadata is unavailable.")
    profiles_by_id = {str(profile.get("id", "")): profile for profile in profiles}
    backups_by_id = {str(backup.get("id", "")): backup for backup in backups}
    if not profiles_by_id or set(profiles_by_id) != set(backups_by_id):
        raise PlaybookMigrationError("Playbook migration profile backups do not match pending profiles.")
    try:
        repository = client.repository_metadata()
        if not repository.private or repository.default_branch != "main":
            raise PlaybookMigrationError("Forgejo protected main repository is unavailable for migration finalization.")
        pull_request = client.pull_request(pull_request_number)
        if not pull_request.merged:
            raise PlaybookMigrationError(
                "Playbook migration pull request has not been merged by an authorized reviewer."
            )
        if pull_request.base != repository.default_branch or pull_request.head != branch:
            raise PlaybookMigrationError("Playbook migration pull request does not target its protected main branch.")
        commit_sha = _require_commit_sha(client.revision(repository.default_branch).sha)
        finalized: list[dict[str, Any]] = []
        for profile_id, profile in profiles_by_id.items():
            source = profile_source_metadata(profile)
            migration = source.get("migration") if isinstance(source, dict) else None
            if (
                source.get("state") != "migration-pending-review"
                or not isinstance(migration, dict)
                or migration.get("batchId") != batch_id
                or migration.get("branch") != branch
                or migration.get("pullRequestNumber") != pull_request_number
            ):
                raise PlaybookMigrationError("Collection profile migration metadata is unavailable.")
            path = _require_profile_path(profile)
            main_file = client.read_file(path, commit_sha)
            if main_file.content != _migration_backup_source(backups_by_id[profile_id]):
                raise PlaybookMigrationError(
                    f"Protected main content for {path} does not match the staged legacy playbook bytes."
                )
            updated = dict(profile)
            updated_source = dict(source)
            updated_source.pop("migration", None)
            updated_source["repository"] = repository.full_name
            updated_source["path"] = main_file.path
            updated_source["commitSha"] = commit_sha
            updated_source["state"] = "pinned"
            updated_source["merge"] = {
                "pullRequestNumber": pull_request.number,
                "mergeCommitSha": pull_request.merge_commit_sha or commit_sha,
                "mergedAt": pull_request.merged_at,
            }
            updated["source"] = updated_source
            updated["playbook"] = main_file.path
            updated["revision"] = commit_sha
            updated.pop("playbookContent", None)
            finalized.append(updated)
        return FinalizedPlaybooks(
            repository=repository,
            commit_sha=commit_sha,
            pull_request=pull_request,
            profiles=finalized,
        )
    except PlaybookMigrationError:
        raise
    except ForgejoConfigurationError as exc:
        raise PlaybookMigrationError("Forgejo migration credential is unavailable.") from exc
    except ForgejoRequestError as exc:
        if exc.status_code == 404:
            raise PlaybookMigrationError("Playbook migration pull request or protected main source is unavailable.") from exc
        raise PlaybookMigrationError("Forgejo rejected the playbook migration finalization request.") from exc
    except ForgejoError as exc:
        raise PlaybookMigrationError("Forgejo is unavailable for playbook migration finalization.") from exc


def _latest_batch(cursor: psycopg.Cursor) -> dict[str, Any] | None:
    cursor.execute(
        """
        SELECT id, branch, state, repository, commit_sha, pull_request_number
        FROM sentinel.playbook_migration_batches
        ORDER BY created_at DESC
        LIMIT 1
        """
    )
    row = cursor.fetchone()
    if not row:
        return None
    return {
        "id": row[0],
        "branch": row[1],
        "state": row[2],
        "repository": row[3],
        "commit_sha": row[4],
        "pull_request_number": row[5],
    }


def _batch_by_id(cursor: psycopg.Cursor, batch_id: str) -> dict[str, Any] | None:
    cursor.execute(
        """
        SELECT id, branch, state, repository, commit_sha, pull_request_number, merge_commit_sha, merged_at
        FROM sentinel.playbook_migration_batches
        WHERE id = %s
        """,
        (batch_id,),
    )
    row = cursor.fetchone()
    if not row:
        return None
    return {
        "id": row[0],
        "branch": row[1],
        "state": row[2],
        "repository": row[3],
        "commit_sha": row[4],
        "pull_request_number": row[5],
        "merge_commit_sha": row[6],
        "merged_at": row[7],
    }


def _active_batch(cursor: psycopg.Cursor) -> dict[str, Any] | None:
    cursor.execute(
        """
        SELECT id, branch, state, repository, commit_sha, pull_request_number
        FROM sentinel.playbook_migration_batches
        WHERE state IN ('staging', 'pending_review')
        ORDER BY created_at DESC
        LIMIT 1
        """
    )
    row = cursor.fetchone()
    if not row:
        return None
    return {
        "id": row[0],
        "branch": row[1],
        "state": row[2],
        "repository": row[3],
        "commit_sha": row[4],
        "pull_request_number": row[5],
    }


def _legacy_profiles(cursor: psycopg.Cursor) -> list[dict[str, Any]]:
    cursor.execute(
        "SELECT payload FROM sentinel.collection_profiles WHERE payload ? 'playbookContent' ORDER BY created_at, id"
    )
    return [row[0] for row in cursor.fetchall()]


def _batch_backups(cursor: psycopg.Cursor, batch_id: str) -> list[dict[str, Any]]:
    cursor.execute(
        """
        SELECT profile_id, legacy_payload
        FROM sentinel.collection_profile_playbook_backups
        WHERE batch_id = %s
        ORDER BY profile_id
        """,
        (batch_id,),
    )
    return [{"id": row[0], "payload": row[1]} for row in cursor.fetchall()]


def _pending_profiles(cursor: psycopg.Cursor, batch_id: str) -> list[dict[str, Any]]:
    cursor.execute(
        """
        SELECT payload
        FROM sentinel.collection_profiles
        WHERE payload->'source'->'migration'->>'batchId' = %s
        ORDER BY created_at, id
        """,
        (batch_id,),
    )
    return [row[0] for row in cursor.fetchall()]


def _write_backups(cursor: psycopg.Cursor, batch_id: str, profiles: Iterable[dict[str, Any]]) -> None:
    for profile in profiles:
        profile_id = str(profile.get("id", ""))
        source = _require_legacy_source(profile)
        if not profile_id:
            raise PlaybookMigrationError("A legacy collection profile has no stable identifier.")
        cursor.execute(
            """
            INSERT INTO sentinel.collection_profile_playbook_backups
              (batch_id, profile_id, legacy_payload, source_sha256)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (batch_id, profile_id) DO NOTHING
            """,
            (
                batch_id,
                profile_id,
                Jsonb(profile),
                hashlib.sha256(source.encode("utf-8")).hexdigest(),
            ),
        )


def _insert_batch(cursor: psycopg.Cursor) -> dict[str, Any]:
    batch_id = uuid4().hex
    branch = _profile_migration_branch(batch_id)
    cursor.execute(
        "INSERT INTO sentinel.playbook_migration_batches (id, branch, state) VALUES (%s, %s, %s)",
        (batch_id, branch, MIGRATION_STAGING),
    )
    return {"id": batch_id, "branch": branch, "state": MIGRATION_STAGING}


def _pending_pull_request(batch: dict[str, Any]) -> ForgejoPullRequest | None:
    number = batch.get("pull_request_number")
    if not isinstance(number, int):
        return None
    # The migration only needs the number after a crash between creation and
    # PostgreSQL finalization; other values are stored as stable profile metadata.
    return ForgejoPullRequest(number=number, url=None, head=batch["branch"], base="main", state="open")


def migrate_database_profiles(database_url: str, client: ForgejoClient | None = None) -> None:
    """Migrate all legacy profile payloads, or fail before any source is discarded."""
    owned_client = False
    try:
        with psycopg.connect(database_url, autocommit=True) as connection, connection.cursor() as cursor:
            legacy_profiles = _legacy_profiles(cursor)
            if not legacy_profiles:
                return

            if client is None:
                try:
                    client = ForgejoClient.from_environment()
                except ForgejoConfigurationError as exc:
                    raise PlaybookMigrationError("Forgejo migration credential is unavailable.") from exc
                owned_client = True

            latest = _latest_batch(cursor)
            if latest and latest["state"] == MIGRATION_ROLLED_BACK and os.environ.get(MIGRATION_MODE_ENV) != "1":
                raise PlaybookMigrationError(
                    "Playbook migration was rolled back. Set SENTINEL_PLAYBOOK_MIGRATION_RESET=1 after resolving the issue."
                )

            batch = _active_batch(cursor) or _insert_batch(cursor)
            _write_backups(cursor, batch["id"], legacy_profiles)
            backups = _batch_backups(cursor, batch["id"])
            staged_profiles = [item["payload"] for item in backups]

            staged = stage_legacy_playbooks(
                client,
                staged_profiles,
                batch["id"],
                batch["branch"],
                _pending_pull_request(batch),
            )

            with connection.transaction():
                for backup in backups:
                    migrated = migrated_profile_payload(backup["payload"], staged, batch["id"])
                    cursor.execute(
                        """
                        UPDATE sentinel.collection_profiles
                        SET payload = %s, updated_at = NOW()
                        WHERE id = %s
                        """,
                        (Jsonb(migrated), backup["id"]),
                    )
                cursor.execute(
                    """
                    UPDATE sentinel.playbook_migration_batches
                    SET state = %s, repository = %s, commit_sha = %s,
                        pull_request_number = %s, updated_at = NOW()
                    WHERE id = %s
                    """,
                    (
                        MIGRATION_PENDING_REVIEW,
                        staged.repository.full_name,
                        staged.commit_sha,
                        staged.pull_request.number,
                        batch["id"],
                    ),
                )
    except PlaybookMigrationError:
        raise
    except psycopg.Error as exc:
        raise PlaybookMigrationError("PostgreSQL could not record the playbook migration state.") from exc
    finally:
        if owned_client and client is not None:
            client.close()


def rollback_database_profiles(database_url: str, batch_id: str) -> int:
    """Restore the JSONB payloads from a pending-review migration backup."""
    try:
        with psycopg.connect(database_url, autocommit=True) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT state FROM sentinel.playbook_migration_batches WHERE id = %s",
                (batch_id,),
            )
            row = cursor.fetchone()
            if not row:
                raise PlaybookMigrationError("Playbook migration batch was not found.")
            if row[0] != MIGRATION_PENDING_REVIEW:
                raise PlaybookMigrationError("Only a pending-review playbook migration can be rolled back.")
            backups = _batch_backups(cursor, batch_id)
            if not backups:
                raise PlaybookMigrationError("Playbook migration backup is unavailable.")

            with connection.transaction():
                for backup in backups:
                    cursor.execute(
                        """
                        UPDATE sentinel.collection_profiles
                        SET payload = %s, updated_at = NOW()
                        WHERE id = %s
                        """,
                        (Jsonb(backup["payload"]), backup["id"]),
                    )
                cursor.execute(
                    """
                    UPDATE sentinel.playbook_migration_batches
                    SET state = %s, updated_at = NOW()
                    WHERE id = %s
                    """,
                    (MIGRATION_ROLLED_BACK, batch_id),
                )
            return len(backups)
    except PlaybookMigrationError:
        raise
    except psycopg.Error as exc:
        raise PlaybookMigrationError("PostgreSQL could not restore the playbook migration backup.") from exc


def finalize_database_profiles(
    database_url: str, batch_id: str, client: ForgejoClient | None = None
) -> dict[str, Any]:
    """Finalize one already-merged migration batch without mutating Forgejo."""
    owned_client = False
    try:
        with psycopg.connect(database_url, autocommit=True) as connection, connection.cursor() as cursor:
            batch = _batch_by_id(cursor, batch_id)
            if not batch:
                raise PlaybookMigrationError("Playbook migration batch was not found.")
            profiles = _pending_profiles(cursor, batch_id)
            if batch["state"] == MIGRATION_FINALIZED:
                finalized_count = len(_batch_backups(cursor, batch_id))
                return {
                    "batchId": batch_id,
                    "state": MIGRATION_FINALIZED,
                    "finalizedProfiles": finalized_count,
                    "alreadyFinalized": True,
                }
            if batch["state"] != MIGRATION_PENDING_REVIEW:
                raise PlaybookMigrationError("Only a pending-review playbook migration can be finalized.")
            backups = _batch_backups(cursor, batch_id)
            if client is None:
                try:
                    client = ForgejoClient.from_environment()
                except ForgejoConfigurationError as exc:
                    raise PlaybookMigrationError("Forgejo migration credential is unavailable.") from exc
                owned_client = True
            finalized = finalize_migrated_profiles(client, batch, profiles, backups)
            with connection.transaction():
                for profile in finalized.profiles:
                    cursor.execute(
                        """
                        UPDATE sentinel.collection_profiles
                        SET payload = %s, updated_at = NOW()
                        WHERE id = %s
                        """,
                        (Jsonb(profile), profile["id"]),
                    )
                cursor.execute(
                    """
                    UPDATE sentinel.playbook_migration_batches
                    SET state = %s, repository = %s, commit_sha = %s,
                        merge_commit_sha = %s, merged_at = %s, updated_at = NOW()
                    WHERE id = %s
                    """,
                    (
                        MIGRATION_FINALIZED,
                        finalized.repository.full_name,
                        finalized.commit_sha,
                        finalized.pull_request.merge_commit_sha or finalized.commit_sha,
                        finalized.pull_request.merged_at,
                        batch_id,
                    ),
                )
            return {
                "batchId": batch_id,
                "state": MIGRATION_FINALIZED,
                "finalizedProfiles": len(finalized.profiles),
                "alreadyFinalized": False,
                "commitSha": finalized.commit_sha,
            }
    except PlaybookMigrationError:
        raise
    except psycopg.Error as exc:
        raise PlaybookMigrationError("PostgreSQL could not finalize the playbook migration state.") from exc
    finally:
        if owned_client and client is not None:
            client.close()


def _main() -> None:
    parser = argparse.ArgumentParser(description="Sentinel legacy playbook migration recovery")
    parser.add_argument("command", choices=["rollback", "finalize"])
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    args = parser.parse_args()
    if not args.database_url:
        parser.error("DATABASE_URL or --database-url is required")
    if args.command == "rollback":
        restored = rollback_database_profiles(args.database_url, args.batch_id)
        print(f"Restored {restored} collection profile payloads from migration backup {args.batch_id}.")
        return
    result = finalize_database_profiles(args.database_url, args.batch_id)
    print(
        f"Finalized {result['finalizedProfiles']} collection profile sources from migration batch {args.batch_id}."
    )


if __name__ == "__main__":
    _main()
