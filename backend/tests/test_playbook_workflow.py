import subprocess
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from forgejo_client import (  # noqa: E402
    ForgejoFile,
    ForgejoFileCommit,
    ForgejoHistoryEntry,
    ForgejoPullRequest,
    ForgejoRepository,
    ForgejoRequestError,
    ForgejoRevision,
)
from playbook_migration import (  # noqa: E402
    MIGRATION_PENDING_REVIEW,
    PlaybookMigrationError,
    finalize_migrated_profiles,
)
from sentinel.playbooks import (  # noqa: E402
    create_or_update_profile_draft,
    pin_profile_revision,
    profile_playbook_history,
    read_profile_playbook,
    require_runnable_source,
    run_ansible_syntax_check,
    syntax_check_pinned_profile,
)
from sentinel.routers import collections as collection_routes  # noqa: E402
import main  # noqa: E402


MAIN_SHA = "1" * 40
HISTORY_SHA = "2" * 40
DRAFT_SHA = "3" * 40
MERGE_SHA = "4" * 40
PLAYBOOK = b"---\n- name: Gather facts\n  hosts: all\n  tasks: []\n"


def profile(*, state="pinned", commit_sha=MAIN_SHA):
    return {
        "id": "linux-inventory-facts",
        "name": "Linux inventory facts",
        "playbook": "inventory/linux-facts.yml",
        "revision": commit_sha,
        "state": "enabled",
        "source": {
            "repository": "sentinel/sentinel-playbooks",
            "path": "inventory/linux-facts.yml",
            "commitSha": commit_sha,
            "state": state,
        },
    }


class WorkflowForgejo:
    def __init__(self):
        self.files = {
            (MAIN_SHA, "inventory/linux-facts.yml"): PLAYBOOK,
            (HISTORY_SHA, "inventory/linux-facts.yml"): PLAYBOOK,
        }
        self.branches = {"main": MAIN_SHA}
        self.created_prs = 0
        self.written = []
        self.pull = ForgejoPullRequest(
            number=7,
            url=None,
            head="sentinel/profile-linux-inventory-facts",
            base="main",
            state="open",
        )

    def repository_metadata(self):
        return ForgejoRepository("sentinel/sentinel-playbooks", True, "main", None)

    def revision(self, ref):
        if ref in self.branches:
            return ForgejoRevision(self.branches[ref], "revision")
        return ForgejoRevision(ref, "revision")

    def read_file(self, path, ref):
        content = self.files.get((ref, path))
        if content is None:
            raise ForgejoRequestError("missing", 404)
        return ForgejoFile(path, "file-sha", content)

    def file_history(self, path, ref, limit=50):
        assert path == "inventory/linux-facts.yml"
        assert ref == "main"
        return [
            ForgejoHistoryEntry(MAIN_SHA, "Current", "2026-09-28T12:00:00Z"),
            ForgejoHistoryEntry(HISTORY_SHA, "Older", None),
        ]

    def create_branch(self, name, from_ref):
        if name in self.branches:
            raise ForgejoRequestError("exists", 409)
        assert from_ref == "main"
        self.branches[name] = MAIN_SHA
        return type("Branch", (), {"name": name, "commit_sha": MAIN_SHA})()

    def create_or_update_file(self, path, branch, content, message, current_sha=None):
        assert branch in self.branches
        assert path == "inventory/linux-facts.yml"
        self.files[(branch, path)] = content
        self.branches[branch] = DRAFT_SHA
        self.written.append(content)
        return ForgejoFileCommit(path, "file-sha", DRAFT_SHA)

    def find_open_pull_request(self, head, base):
        return self.pull if self.pull.state == "open" else None

    def create_pull_request(self, head, base, title, body=""):
        self.created_prs += 1
        self.pull = ForgejoPullRequest(7, None, head, base, "open")
        return self.pull

    def pull_request(self, number):
        assert number == 7
        return self.pull


def test_pending_source_is_rejected_for_run_and_syntax_gating():
    pending = profile(state="migration-pending-review")

    with pytest.raises(HTTPException) as raised:
        require_runnable_source(pending)

    assert raised.value.status_code == 409
    assert "cannot be run or syntax checked" in raised.value.detail


def test_pending_source_is_rejected_by_run_and_syntax_api_routes(monkeypatch):
    pending = profile(state="migration-pending-review")
    monkeypatch.setattr(main, "initialize_database", lambda: None)
    monkeypatch.setattr(collection_routes, "current_record_or_404", lambda kind, record_id: pending)
    main.app.dependency_overrides[main.get_forgejo_client] = lambda: WorkflowForgejo()
    try:
        with TestClient(main.app) as api:
            run = api.post("/api/profiles/linux-inventory-facts/run")
            syntax = api.post("/api/profiles/linux-inventory-facts/syntax-check", json={})
    finally:
        main.app.dependency_overrides.clear()

    assert run.status_code == 409
    assert syntax.status_code == 409
    assert "awaiting Git review" in run.json()["detail"]
    assert "awaiting Git review" in syntax.json()["detail"]


def test_playbook_read_and_history_are_forgejo_backed_without_local_source():
    client = WorkflowForgejo()
    item = profile()

    read = read_profile_playbook(item, client)
    history = profile_playbook_history(item, client)

    assert read["content"] == PLAYBOOK.decode("utf-8")
    assert read["source"]["commitSha"] == MAIN_SHA
    assert history["revisions"][0]["selected"] is True
    assert "playbookContent" not in item


def test_pending_migration_history_inspects_the_selected_review_revision():
    history = profile_playbook_history(profile(state="migration-pending-review"), WorkflowForgejo())

    assert history["source"]["state"] == "migration-pending-review"
    assert history["revisions"] == [
        {"commitSha": MAIN_SHA, "message": "revision", "authoredAt": None, "selected": True}
    ]


def test_draft_writes_exact_browser_bytes_to_forgejo_and_persists_metadata_only():
    client = WorkflowForgejo()
    item = profile()
    draft = "  ---\n- name: Preserve whitespace\n  hosts: all\n  tasks: []\n\n"

    updated = create_or_update_profile_draft(item, {"source": draft}, client)

    assert client.written == [draft.encode("utf-8")]
    assert updated["source"]["draft"] == {
        "branch": "sentinel/profile-linux-inventory-facts",
        "pullRequestNumber": 7,
        "commitSha": DRAFT_SHA,
        "state": "open",
        "base": "main",
    }
    assert "source" not in updated["source"]["draft"]
    assert "playbookContent" not in updated


def test_pin_only_allows_a_revision_reachable_from_protected_main_history():
    client = WorkflowForgejo()
    item = profile()

    updated = pin_profile_revision(item, {"commitSha": HISTORY_SHA}, client)

    assert updated["source"]["commitSha"] == HISTORY_SHA
    with pytest.raises(HTTPException, match="not in protected main history"):
        pin_profile_revision(item, {"commitSha": DRAFT_SHA}, client)


def test_syntax_runner_is_target_free_and_sanitizes_error_output():
    observed = {}

    def valid_runner(command, **kwargs):
        observed["command"] = command
        observed["inventory"] = Path(command[3]).read_text(encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="secret output", stderr="secret output")

    valid = run_ansible_syntax_check(PLAYBOOK, valid_runner)
    assert valid == {"valid": True, "diagnostics": []}
    assert observed["command"][:3] == ["ansible-playbook", "--syntax-check", "--inventory"]
    assert observed["inventory"] == "[all]\n"

    invalid = run_ansible_syntax_check(
        PLAYBOOK,
        lambda command, **kwargs: subprocess.CompletedProcess(command, 4, stdout="token", stderr="token"),
    )
    assert invalid == {"valid": False, "diagnostics": ["Ansible syntax check failed (exit code 4)."]}

    unavailable = run_ansible_syntax_check(PLAYBOOK, lambda *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError()))
    assert unavailable["valid"] is False
    assert "not installed" in unavailable["diagnostics"][0]


def test_syntax_check_fetches_selected_pinned_commit_not_browser_content():
    client = WorkflowForgejo()
    observed = []

    def runner(command, **kwargs):
        observed.append(command)
        return subprocess.CompletedProcess(command, 0)

    result = syntax_check_pinned_profile(profile(), client, runner)

    assert result["valid"] is True
    assert result["commitSha"] == MAIN_SHA
    assert result["mode"] == "ansible-playbook-syntax-check"
    assert observed


class FinalizationForgejo:
    def __init__(self, merged=True, content=PLAYBOOK):
        self.merged = merged
        self.content = content

    def repository_metadata(self):
        return ForgejoRepository("sentinel/sentinel-playbooks", True, "main", None)

    def pull_request(self, number):
        return ForgejoPullRequest(
            number=17,
            url=None,
            head="migration/playbook-source-batch-1",
            base="main",
            state="closed" if self.merged else "open",
            merged=self.merged,
            merge_commit_sha=MERGE_SHA if self.merged else None,
            merged_at="2026-09-28T12:00:00Z" if self.merged else None,
        )

    def revision(self, ref):
        assert ref == "main"
        return ForgejoRevision(MAIN_SHA, "main")

    def read_file(self, path, ref):
        assert ref == MAIN_SHA
        return ForgejoFile(path, "file-sha", self.content)


def migration_profile():
    item = profile(state="migration-pending-review")
    item["source"]["migration"] = {
        "batchId": "batch-1",
        "branch": "migration/playbook-source-batch-1",
        "pullRequestNumber": 17,
    }
    return item


def test_migration_finalization_requires_human_merged_pr_and_preserves_bytes():
    batch = {
        "id": "batch-1",
        "branch": "migration/playbook-source-batch-1",
        "pull_request_number": 17,
        "state": MIGRATION_PENDING_REVIEW,
    }
    backups = [{"id": "linux-inventory-facts", "payload": {"playbookContent": PLAYBOOK.decode("utf-8")}}]

    with pytest.raises(PlaybookMigrationError, match="has not been merged"):
        finalize_migrated_profiles(FinalizationForgejo(merged=False), batch, [migration_profile()], backups)

    finalized = finalize_migrated_profiles(FinalizationForgejo(), batch, [migration_profile()], backups)
    source = finalized.profiles[0]["source"]
    assert finalized.commit_sha == MAIN_SHA
    assert source["state"] == "pinned"
    assert source["merge"]["pullRequestNumber"] == 17
    assert "migration" not in source

    with pytest.raises(PlaybookMigrationError, match="does not match"):
        finalize_migrated_profiles(FinalizationForgejo(content=b"changed"), batch, [migration_profile()], backups)


def test_migration_finalization_is_stable_when_retried_after_a_merged_review():
    batch = {
        "id": "batch-1",
        "branch": "migration/playbook-source-batch-1",
        "pull_request_number": 17,
        "state": MIGRATION_PENDING_REVIEW,
    }
    backups = [{"id": "linux-inventory-facts", "payload": {"playbookContent": PLAYBOOK.decode("utf-8")}}]

    first = finalize_migrated_profiles(FinalizationForgejo(), batch, [migration_profile()], backups)
    second = finalize_migrated_profiles(FinalizationForgejo(), batch, [migration_profile()], backups)

    assert first == second
