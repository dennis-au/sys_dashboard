"""Opt-in Docker integration coverage for the real Forgejo client.

This module deliberately does not match pytest's default ``test_*.py`` file
pattern. Run it only through tests/forgejo-integration/run.sh, which creates
an isolated Forgejo data volume and removes it afterwards.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx
import pytest


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from forgejo_client import ForgejoClient, ForgejoRequestError, ForgejoSettings


def _settings() -> ForgejoSettings:
    return ForgejoSettings.from_environment()


def _repository_url(settings: ForgejoSettings) -> str:
    return f"{settings.base_url}/api/v1/repos/{settings.owner}/{settings.repository}"


def test_real_forgejo_client_write_path(tmp_path: Path) -> None:
    settings = _settings()
    client = ForgejoClient(settings)
    branch = "integration/write-path"
    path = "collections/byte-preservation.yml"
    source = b"---\n- name: Retain exact source bytes\n  hosts: all\n  vars:\n    accent: caf\xc3\xa9\n"

    try:
        readiness = client.readiness()
        assert readiness.repository.full_name == "sentinel/sentinel-playbooks"
        assert readiness.repository.private is True
        assert readiness.repository.default_branch == "main"

        anonymous = httpx.get(_repository_url(settings), timeout=settings.timeout_seconds)
        assert anonymous.status_code in {401, 403}

        branch_result = client.create_branch(branch, "main")
        assert branch_result.name == branch
        assert branch_result.commit_sha

        file_commit = client.create_or_update_file(
            path,
            branch,
            source,
            "Add Forgejo integration fixture",
        )
        assert file_commit.path == path
        assert file_commit.commit_sha

        saved_file = client.read_file(path, branch)
        assert saved_file.content == source

        pull_request = client.create_pull_request(
            branch,
            "main",
            "Forgejo client integration fixture",
        )
        assert pull_request.head == branch
        assert pull_request.base == "main"
        assert pull_request.state == "open"

        with pytest.raises(ForgejoRequestError) as protected_main:
            client.create_or_update_file(
                "collections/direct-main-write.yml",
                "main",
                b"---\n- hosts: all\n",
                "This write must be rejected",
            )
        assert protected_main.value.status_code in {401, 403, 409, 422}

        invalid_token_file = tmp_path / "invalid-forgejo-token"
        invalid_token_file.write_text("invalid-token", encoding="utf-8")
        invalid_client = ForgejoClient(
            ForgejoSettings(
                base_url=settings.base_url,
                token_file=invalid_token_file,
                owner=settings.owner,
                repository=settings.repository,
                timeout_seconds=settings.timeout_seconds,
            )
        )
        try:
            with pytest.raises(ForgejoRequestError) as invalid_access:
                invalid_client.repository_metadata()
        finally:
            invalid_client.close()
        assert invalid_access.value.status_code in {401, 403}
    finally:
        client.close()
