import os
import sys
from pathlib import Path

import psycopg
import pytest
from psycopg.types.json import Jsonb


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from forgejo_client import (
    ForgejoBranch,
    ForgejoFile,
    ForgejoFileCommit,
    ForgejoPullRequest,
    ForgejoRepository,
    ForgejoRequestError,
    ForgejoRevision,
)
from playbook_migration import (
    MIGRATION_PENDING_REVIEW,
    MIGRATION_ROLLED_BACK,
    PlaybookMigrationError,
    migrated_profile_payload,
    rollback_database_profiles,
    stage_legacy_playbooks,
)


SHA = "0123456789abcdef0123456789abcdef01234567"


class FakeForgejo:
    def __init__(self):
        self.files: dict[tuple[str, str], bytes] = {}
        self.branches: set[str] = set()
        self.create_branch_calls = 0
        self.file_commits = 0
        self.pull_requests = 0
        self.repository = ForgejoRepository(
            full_name="sentinel/sentinel-playbooks",
            private=True,
            default_branch="main",
            html_url=None,
        )

    def repository_metadata(self):
        return self.repository

    def create_branch(self, name, from_ref):
        self.create_branch_calls += 1
        if name in self.branches:
            raise ForgejoRequestError("branch exists", 409)
        assert from_ref == "main"
        self.branches.add(name)
        return ForgejoBranch(name=name, commit_sha=SHA)

    def revision(self, ref):
        assert ref == "main" or ref in self.branches
        return ForgejoRevision(sha=SHA, message="migration")

    def read_file(self, path, ref):
        content = self.files.get((ref, path))
        if content is None:
            raise ForgejoRequestError("missing", 404)
        return ForgejoFile(path=path, sha="file-sha", content=content)

    def create_or_update_file(self, path, branch, content, message, current_sha=None):
        assert current_sha is None
        assert branch in self.branches
        self.file_commits += 1
        self.files[(branch, path)] = content
        return ForgejoFileCommit(path=path, content_sha="file-sha", commit_sha=SHA)

    def find_open_pull_request(self, head, base):
        assert base == "main"
        return None

    def create_pull_request(self, head, base, title, body=""):
        self.pull_requests += 1
        assert head in self.branches
        assert base == "main"
        assert "review" in body.lower()
        return ForgejoPullRequest(number=17, url=None, head=head, base=base, state="open")


def legacy_profile(source: str = "  ---\n- name: Preserve bytes\n  hosts: all\n\n"):
    return {
        "id": "bytes-profile",
        "name": "Byte preserving profile",
        "playbook": "inventory/bytes.yml",
        "revision": "main · old",
        "playbookContent": source,
    }


def test_stages_exact_legacy_bytes_and_returns_immutable_source_metadata():
    client = FakeForgejo()
    profile = legacy_profile()

    staged = stage_legacy_playbooks(client, [profile], "batch-123", "migration/playbook-source-batch")
    migrated = migrated_profile_payload(profile, staged, "batch-123")

    assert client.files[(staged.branch, "inventory/bytes.yml")] == profile["playbookContent"].encode("utf-8")
    assert migrated["revision"] == SHA
    assert migrated["source"] == {
        "repository": "sentinel/sentinel-playbooks",
        "path": "inventory/bytes.yml",
        "commitSha": SHA,
        "state": "migration-pending-review",
        "migration": {
            "batchId": "batch-123",
            "branch": "migration/playbook-source-batch",
            "pullRequestNumber": 17,
        },
    }
    assert "playbookContent" not in migrated


def test_stage_is_idempotent_when_an_interrupted_migration_branch_already_has_exact_bytes():
    client = FakeForgejo()
    profile = legacy_profile()
    branch = "migration/playbook-source-batch"
    client.branches.add(branch)
    client.files[(branch, profile["playbook"])] = profile["playbookContent"].encode("utf-8")

    stage_legacy_playbooks(client, [profile], "batch-123", branch)

    assert client.create_branch_calls == 1
    assert client.file_commits == 0
    assert client.pull_requests == 1


def test_stage_refuses_to_overwrite_conflicting_content_on_an_interrupted_branch():
    client = FakeForgejo()
    profile = legacy_profile()
    branch = "migration/playbook-source-batch"
    client.branches.add(branch)
    client.files[(branch, profile["playbook"])] = b"---\n- name: Different\n"

    with pytest.raises(PlaybookMigrationError, match="different content"):
        stage_legacy_playbooks(client, [profile], "batch-123", branch)

    assert client.file_commits == 0


def test_migration_refuses_non_immutable_revision_values():
    client = FakeForgejo()
    client.revision = lambda ref: ForgejoRevision(sha="main", message="not immutable")

    with pytest.raises(PlaybookMigrationError, match="immutable commit SHA"):
        stage_legacy_playbooks(client, [legacy_profile()], "batch-123", "migration/playbook-source-batch")


def test_rollback_restores_the_exact_legacy_jsonb_payload_from_the_batch_backup():
    database_url = os.environ["DATABASE_URL"]
    batch_id = "playbook-migration-regression-batch"
    profile_id = "playbook-migration-regression-profile"
    legacy = legacy_profile(" \n---\n- name: Restore exact source\n\n")
    legacy["id"] = profile_id
    migrated = {
        "id": profile_id,
        "playbook": legacy["playbook"],
        "revision": SHA,
        "source": {"repository": "sentinel/sentinel-playbooks", "path": legacy["playbook"], "commitSha": SHA},
    }

    with psycopg.connect(database_url, autocommit=True) as connection, connection.cursor() as cursor:
        cursor.execute("DELETE FROM sentinel.collection_profile_playbook_backups WHERE batch_id = %s", (batch_id,))
        cursor.execute("DELETE FROM sentinel.playbook_migration_batches WHERE id = %s", (batch_id,))
        cursor.execute("DELETE FROM sentinel.collection_profiles WHERE id = %s", (profile_id,))
        cursor.execute(
            "INSERT INTO sentinel.collection_profiles (id, payload) VALUES (%s, %s)",
            (profile_id, Jsonb(migrated)),
        )
        cursor.execute(
            "INSERT INTO sentinel.playbook_migration_batches (id, branch, state) VALUES (%s, %s, %s)",
            (batch_id, "migration/playbook-source-regression", MIGRATION_PENDING_REVIEW),
        )
        cursor.execute(
            """
            INSERT INTO sentinel.collection_profile_playbook_backups
              (batch_id, profile_id, legacy_payload, source_sha256)
            VALUES (%s, %s, %s, %s)
            """,
            (batch_id, profile_id, Jsonb(legacy), "not-used-by-rollback"),
        )

    try:
        assert rollback_database_profiles(database_url, batch_id) == 1
        with psycopg.connect(database_url, autocommit=True) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT payload FROM sentinel.collection_profiles WHERE id = %s", (profile_id,))
            assert cursor.fetchone()[0] == legacy
            cursor.execute("SELECT state FROM sentinel.playbook_migration_batches WHERE id = %s", (batch_id,))
            assert cursor.fetchone()[0] == MIGRATION_ROLLED_BACK
    finally:
        with psycopg.connect(database_url, autocommit=True) as connection, connection.cursor() as cursor:
            cursor.execute("DELETE FROM sentinel.collection_profile_playbook_backups WHERE batch_id = %s", (batch_id,))
            cursor.execute("DELETE FROM sentinel.playbook_migration_batches WHERE id = %s", (batch_id,))
            cursor.execute("DELETE FROM sentinel.collection_profiles WHERE id = %s", (profile_id,))
