import sys
from pathlib import Path

import pytest
from fastapi import HTTPException


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import main
from forgejo_client import ForgejoFile, ForgejoHistoryEntry, ForgejoRepository, ForgejoRequestError, ForgejoRevision


SHA = "0123456789abcdef0123456789abcdef01234567"


class SourceClient:
    def __init__(self, *, revision=SHA, missing_file=False, denied=False):
        self.revision_sha = revision
        self.missing_file = missing_file
        self.denied = denied

    def repository_metadata(self):
        return ForgejoRepository(
            full_name="sentinel/sentinel-playbooks",
            private=True,
            default_branch="main",
            html_url=None,
        )

    def revision(self, ref):
        assert ref == SHA
        return ForgejoRevision(sha=self.revision_sha, message="Pinned playbook")

    def read_file(self, path, ref):
        assert path == "inventory/linux-facts.yml"
        assert ref == SHA
        if self.missing_file:
            raise ForgejoRequestError("missing", 404)
        if self.denied:
            raise ForgejoRequestError("denied", 403)
        return ForgejoFile(path=path, sha="file-sha", content=b"---\n")

    def file_history(self, path, ref, limit=50):
        assert path == "inventory/linux-facts.yml"
        assert ref == "main"
        return [ForgejoHistoryEntry(sha=SHA, message="Pinned playbook", authored_at=None)]


def source_request(**overrides):
    source = {
        "repository": "sentinel/sentinel-playbooks",
        "path": "inventory/linux-facts.yml",
        "commitSha": SHA,
    }
    source.update(overrides)
    return {"playbook": source["path"], "revision": source["commitSha"], "source": source}


def test_profile_source_requires_the_exact_pinned_commit_and_preserves_metadata():
    source = main.resolve_profile_source(source_request(), SourceClient())

    assert source == {
        "repository": "sentinel/sentinel-playbooks",
        "path": "inventory/linux-facts.yml",
        "commitSha": SHA,
        "state": "pinned",
    }


def test_profile_source_rejects_a_revision_that_resolves_to_different_commit():
    with pytest.raises(HTTPException) as raised:
        main.resolve_profile_source(source_request(), SourceClient(revision="f" * 40))

    assert raised.value.status_code == 422
    assert "did not resolve" in raised.value.detail


def test_profile_source_rejects_a_commit_that_is_not_reachable_from_protected_main():
    client = SourceClient()
    client.file_history = lambda path, ref, limit=50: []

    with pytest.raises(HTTPException) as raised:
        main.resolve_profile_source(source_request(), client)

    assert raised.value.status_code == 422
    assert "protected main" in raised.value.detail


@pytest.mark.parametrize("client", [SourceClient(missing_file=True), SourceClient(denied=True)])
def test_profile_source_reports_missing_or_inaccessible_content_without_exposing_forgejo_details(client):
    with pytest.raises(HTTPException) as raised:
        main.resolve_profile_source(source_request(), client)

    assert raised.value.status_code in {422, 503}
    assert "Forgejo" in raised.value.detail or "Git playbook" in raised.value.detail
    assert "missing" not in raised.value.detail
    assert "denied" not in raised.value.detail


def test_public_profile_never_serializes_legacy_playbook_content():
    result = main.public_profile(
        {
            "id": "profile-1",
            "playbookContent": "---\n",
            "source": {"repository": "sentinel/sentinel-playbooks", "path": "inventory/linux-facts.yml"},
        }
    )

    assert result == {
        "id": "profile-1",
        "source": {"repository": "sentinel/sentinel-playbooks", "path": "inventory/linux-facts.yml"},
    }
