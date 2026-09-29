import base64
import json
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import main
from forgejo_client import (
    ForgejoClient,
    ForgejoConfigurationError,
    ForgejoHistoryEntry,
    ForgejoReadiness,
    ForgejoRepository,
    ForgejoRequestError,
    ForgejoSettings,
)


TOKEN = "test-service-token-not-for-production"


def client_for(tmp_path: Path, handler) -> ForgejoClient:
    token_file = tmp_path / "forgejo_api_token"
    token_file.write_text(f"{TOKEN}\n", encoding="utf-8")
    settings = ForgejoSettings(
        base_url="http://forgejo.test",
        token_file=token_file,
        owner="sentinel",
        repository="sentinel-playbooks",
        timeout_seconds=1.0,
    )
    return ForgejoClient(settings, httpx.Client(transport=httpx.MockTransport(handler)))


def repository_payload() -> dict[str, object]:
    return {
        "full_name": "sentinel/sentinel-playbooks",
        "private": True,
        "default_branch": "main",
        "html_url": "http://forgejo.test/sentinel/sentinel-playbooks",
    }


def test_readiness_checks_health_then_authenticated_private_repository(tmp_path: Path):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/healthz":
            assert "authorization" not in request.headers
            return httpx.Response(200, json={"status": "pass"})
        assert request.url.path == "/api/v1/repos/sentinel/sentinel-playbooks"
        assert request.headers["authorization"] == f"token {TOKEN}"
        return httpx.Response(200, json=repository_payload())

    readiness = client_for(tmp_path, handler).readiness()

    assert readiness.repository.full_name == "sentinel/sentinel-playbooks"
    assert readiness.repository.private is True
    assert [request.url.path for request in requests] == [
        "/api/healthz",
        "/api/v1/repos/sentinel/sentinel-playbooks",
    ]


def test_read_file_preserves_source_bytes_and_uses_requested_revision(tmp_path: Path):
    source = b"---\n- name: Preserve final newline\n  hosts: all\n"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/repos/sentinel/sentinel-playbooks/contents/collections/linux.yml"
        assert request.url.params["ref"] == "4c2b6e7"
        return httpx.Response(
            200,
            json={
                "path": "collections/linux.yml",
                "sha": "file-sha",
                "content": base64.b64encode(source).decode("ascii"),
            },
        )

    result = client_for(tmp_path, handler).read_file("collections/linux.yml", "4c2b6e7")

    assert result.content == source
    assert result.sha == "file-sha"


def test_branch_file_commit_pull_request_and_revision_requests(tmp_path: Path):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        payload = json.loads(request.content) if request.content else {}
        if request.url.path.endswith("/branches"):
            assert payload == {"new_branch_name": "profiles/linux", "old_branch_name": "main"}
            return httpx.Response(201, json={"name": "profiles/linux", "commit": {"id": "base-sha"}})
        if request.url.path.endswith("/contents/collections/linux.yml"):
            assert request.method == "PUT"
            assert payload["branch"] == "profiles/linux"
            assert payload["message"] == "Update Linux inventory profile"
            assert base64.b64decode(payload["content"]) == b"hosts: all\n"
            assert payload["sha"] == "old-file-sha"
            return httpx.Response(
                200,
                json={
                    "content": {"path": "collections/linux.yml", "sha": "new-file-sha"},
                    "commit": {"sha": "commit-sha"},
                },
            )
        if request.url.path.endswith("/pulls"):
            assert payload["head"] == "profiles/linux"
            assert payload["base"] == "main"
            assert "draft" not in payload
            return httpx.Response(
                201,
                json={
                    "number": 12,
                    "html_url": "http://forgejo.test/sentinel/sentinel-playbooks/pulls/12",
                    "head": {"ref": "profiles/linux"},
                    "base": {"ref": "main"},
                    "state": "open",
                },
            )
        if request.url.path.endswith("/git/refs/heads/profiles/linux"):
            return httpx.Response(
                200,
                json=[
                    {
                        "ref": "refs/heads/profiles/linux",
                        "object": {"type": "commit", "sha": "commit-sha"},
                    }
                ],
            )
        raise AssertionError(f"Unexpected Forgejo request: {request.method} {request.url}")

    client = client_for(tmp_path, handler)
    branch = client.create_branch("profiles/linux", "main")
    file_commit = client.create_or_update_file(
        "collections/linux.yml",
        branch.name,
        b"hosts: all\n",
        "Update Linux inventory profile",
        current_sha="old-file-sha",
    )
    pull_request = client.create_pull_request(branch.name, "main", "Review Linux profile")
    revision = client.revision(branch.name)

    assert branch.commit_sha == "base-sha"
    assert file_commit.commit_sha == "commit-sha"
    assert pull_request.number == 12
    assert revision.sha == "commit-sha"
    assert len(requests) == 4


def test_file_creation_uses_post_without_a_current_sha(tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path.endswith("/contents/collections/new-profile.yml")
        payload = json.loads(request.content)
        assert "sha" not in payload
        assert base64.b64decode(payload["content"]) == b"---\n"
        return httpx.Response(
            201,
            json={
                "content": {"path": "collections/new-profile.yml", "sha": "new-file-sha"},
                "commit": {"sha": "commit-sha"},
            },
        )

    committed = client_for(tmp_path, handler).create_or_update_file(
        "collections/new-profile.yml",
        "profiles/linux",
        "---\n",
        "Create collection profile",
    )

    assert committed.path == "collections/new-profile.yml"
    assert committed.commit_sha == "commit-sha"


def test_find_open_pull_request_is_scoped_to_the_repository_owner_and_branch(tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/v1/repos/sentinel/sentinel-playbooks/pulls"
        assert request.url.params["state"] == "open"
        assert request.url.params["head"] == "sentinel:migration/playbook-source-123"
        assert request.url.params["base"] == "main"
        return httpx.Response(
            200,
            json=[
                {
                    "number": 19,
                    "html_url": "http://forgejo.test/sentinel/sentinel-playbooks/pulls/19",
                    "head": {"ref": "migration/playbook-source-123"},
                    "base": {"ref": "main"},
                    "state": "open",
                }
            ],
        )

    pull_request = client_for(tmp_path, handler).find_open_pull_request(
        "migration/playbook-source-123", "main"
    )

    assert pull_request is not None
    assert pull_request.number == 19
    assert pull_request.head == "migration/playbook-source-123"


def test_revision_uses_the_forgejo_git_commit_route_for_an_immutable_sha(tmp_path: Path):
    sha = "0123456789abcdef0123456789abcdef01234567"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == f"/api/v1/repos/sentinel/sentinel-playbooks/git/commits/{sha}"
        return httpx.Response(
            200,
            json={"sha": sha, "commit": {"message": "Pinned collection revision"}},
        )

    revision = client_for(tmp_path, handler).revision(sha)

    assert revision.sha == sha
    assert revision.message == "Pinned collection revision"


def test_pull_request_and_file_history_use_bounded_forgejo_read_apis(tmp_path: Path):
    sha = "0123456789abcdef0123456789abcdef01234567"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/pulls/23"):
            assert request.method == "GET"
            return httpx.Response(
                200,
                json={
                    "number": 23,
                    "html_url": "http://forgejo.test/sentinel/sentinel-playbooks/pulls/23",
                    "head": {"ref": "sentinel/profile-linux"},
                    "base": {"ref": "main"},
                    "state": "closed",
                    "merged": True,
                    "merge_commit_sha": sha,
                    "merged_at": "2026-09-28T12:00:00Z",
                },
            )
        if request.url.path.endswith("/commits"):
            assert request.method == "GET"
            assert request.url.params["path"] == "collections/linux.yml"
            assert request.url.params["sha"] == "main"
            assert request.url.params["limit"] == "50"
            return httpx.Response(
                200,
                json=[
                    {
                        "sha": sha,
                        "commit": {
                            "message": "Review-approved profile update",
                            "author": {"date": "2026-09-28T12:00:00Z"},
                        },
                    }
                ],
            )
        raise AssertionError(f"Unexpected Forgejo request: {request.method} {request.url}")

    client = client_for(tmp_path, handler)
    pull_request = client.pull_request(23)
    history = client.file_history("collections/linux.yml", "main")

    assert pull_request.merged is True
    assert pull_request.merge_commit_sha == sha
    assert history == [
        ForgejoHistoryEntry(
            sha=sha,
            message="Review-approved profile update",
            authored_at="2026-09-28T12:00:00Z",
        )
    ]


def test_token_is_read_per_request_and_never_appears_in_errors(tmp_path: Path):
    token_file = tmp_path / "forgejo_api_token"
    token_file.write_text("first-token", encoding="utf-8")
    seen_tokens: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_tokens.append(request.headers["authorization"])
        if len(seen_tokens) == 1:
            token_file.write_text("second-token", encoding="utf-8")
            return httpx.Response(200, json=repository_payload())
        return httpx.Response(500, text="second-token must not leak")

    settings = ForgejoSettings(
        base_url="http://forgejo.test",
        token_file=token_file,
        owner="sentinel",
        repository="sentinel-playbooks",
        timeout_seconds=1.0,
    )
    client = ForgejoClient(settings, httpx.Client(transport=httpx.MockTransport(handler)))

    client.repository_metadata()
    with pytest.raises(ForgejoRequestError) as raised:
        client.repository_metadata()

    assert seen_tokens == ["token first-token", "token second-token"]
    assert "second-token" not in str(raised.value)
    assert "second-token" not in repr(raised.value)


def test_missing_token_and_invalid_configuration_fail_without_secret_details(tmp_path: Path):
    missing_file = tmp_path / "missing-token"
    settings = ForgejoSettings(
        base_url="http://forgejo.test",
        token_file=missing_file,
        owner="sentinel",
        repository="sentinel-playbooks",
    )
    client = ForgejoClient(settings, httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200))))

    with pytest.raises(ForgejoConfigurationError, match="credential is unavailable"):
        client.repository_metadata()
    with pytest.raises(ForgejoConfigurationError, match="URL configuration is invalid"):
        ForgejoSettings.from_environment({"FORGEJO_URL": "http://user:secret@forgejo.test"})


def test_readiness_api_is_dependency_injected_and_redacts_configuration_errors(monkeypatch):
    class ReadyClient:
        def readiness(self) -> ForgejoReadiness:
            return ForgejoReadiness(
                repository=ForgejoRepository(
                    full_name="sentinel/sentinel-playbooks",
                    private=True,
                    default_branch="main",
                    html_url=None,
                )
            )

    monkeypatch.setattr(main, "initialize_database", lambda: None)
    main.app.dependency_overrides[main.get_forgejo_client] = lambda: ReadyClient()
    try:
        with TestClient(main.app) as api:
            response = api.get("/api/integrations/forgejo/readiness")
    finally:
        main.app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "repository": {
            "fullName": "sentinel/sentinel-playbooks",
            "private": True,
            "defaultBranch": "main",
        },
    }


def test_readiness_api_never_returns_a_service_token(monkeypatch):
    token = "sensitive-token-value"

    class BrokenClient:
        def readiness(self) -> ForgejoReadiness:
            raise ForgejoConfigurationError(f"credential {token} is unavailable")

    monkeypatch.setattr(main, "initialize_database", lambda: None)
    main.app.dependency_overrides[main.get_forgejo_client] = lambda: BrokenClient()
    try:
        with TestClient(main.app) as api:
            response = api.get("/api/integrations/forgejo/readiness")
    finally:
        main.app.dependency_overrides.clear()

    assert response.status_code == 503
    assert response.json() == {"detail": "Forgejo service credential is unavailable."}
    assert token not in response.text


def test_readiness_api_sanitizes_invalid_client_configuration(monkeypatch):
    monkeypatch.setattr(main, "initialize_database", lambda: None)
    monkeypatch.setattr(
        main.ForgejoClient,
        "from_environment",
        classmethod(lambda cls: (_ for _ in ()).throw(ForgejoConfigurationError("invalid token value"))),
    )
    try:
        with TestClient(main.app) as api:
            response = api.get("/api/integrations/forgejo/readiness")
    finally:
        main.app.dependency_overrides.clear()

    assert response.status_code == 503
    assert response.json() == {"detail": "Forgejo configuration is unavailable."}
    assert "invalid token value" not in response.text
