"""Small, bounded Forgejo API client used by Sentinel's backend services.

The client deliberately reads its service token from a mounted file for each
authenticated request. It never keeps the token in a response object, error
message, or configuration value that is returned to a caller.
"""

from __future__ import annotations

import base64
import binascii
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote, urlsplit

import httpx


DEFAULT_FORGEJO_URL = "http://forgejo:3000"
DEFAULT_REPOSITORY_OWNER = "sentinel"
DEFAULT_REPOSITORY_NAME = "sentinel-playbooks"
DEFAULT_TOKEN_FILE = "/run/secrets/forgejo_api_token"
DEFAULT_TIMEOUT_SECONDS = 5.0


class ForgejoError(RuntimeError):
    """A safe-to-report Forgejo integration failure."""


class ForgejoConfigurationError(ForgejoError):
    """Forgejo settings or its mounted service credential are unavailable."""


class ForgejoUnavailableError(ForgejoError):
    """Forgejo could not be contacted within the configured request bound."""


class ForgejoRequestError(ForgejoError):
    """Forgejo returned an unexpected status without exposing response content."""

    def __init__(self, message: str, status_code: int):
        super().__init__(message)
        self.status_code = status_code


class ForgejoProtocolError(ForgejoError):
    """Forgejo returned a response that does not match the expected API shape."""


@dataclass(frozen=True)
class ForgejoSettings:
    base_url: str
    token_file: Path
    owner: str
    repository: str
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS

    @classmethod
    def from_environment(cls, env: Mapping[str, str] | None = None) -> "ForgejoSettings":
        values = os.environ if env is None else env
        base_url = values.get("FORGEJO_URL", DEFAULT_FORGEJO_URL).strip().rstrip("/")
        token_file = values.get("FORGEJO_API_TOKEN_FILE", DEFAULT_TOKEN_FILE).strip()
        owner = values.get("FORGEJO_REPOSITORY_OWNER", DEFAULT_REPOSITORY_OWNER).strip()
        repository = values.get("FORGEJO_REPOSITORY_NAME", DEFAULT_REPOSITORY_NAME).strip()
        timeout_value = values.get("FORGEJO_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS)).strip()

        parsed = urlsplit(base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ForgejoConfigurationError("Forgejo URL configuration is invalid.")
        if not token_file:
            raise ForgejoConfigurationError("Forgejo service credential configuration is missing.")
        _require_segment(owner, "repository owner")
        _require_segment(repository, "repository name")
        try:
            timeout_seconds = float(timeout_value)
        except ValueError as exc:
            raise ForgejoConfigurationError("Forgejo timeout configuration is invalid.") from exc
        if timeout_seconds <= 0:
            raise ForgejoConfigurationError("Forgejo timeout configuration is invalid.")
        return cls(
            base_url=base_url,
            token_file=Path(token_file),
            owner=owner,
            repository=repository,
            timeout_seconds=timeout_seconds,
        )


@dataclass(frozen=True)
class ForgejoRepository:
    full_name: str
    private: bool
    default_branch: str
    html_url: str | None


@dataclass(frozen=True)
class ForgejoFile:
    path: str
    sha: str
    content: bytes


@dataclass(frozen=True)
class ForgejoBranch:
    name: str
    commit_sha: str


@dataclass(frozen=True)
class ForgejoFileCommit:
    path: str
    content_sha: str
    commit_sha: str


@dataclass(frozen=True)
class ForgejoPullRequest:
    number: int
    url: str | None
    head: str
    base: str
    state: str
    merged: bool = False
    merge_commit_sha: str | None = None
    merged_at: str | None = None


@dataclass(frozen=True)
class ForgejoRevision:
    sha: str
    message: str


@dataclass(frozen=True)
class ForgejoHistoryEntry:
    sha: str
    message: str
    authored_at: str | None


@dataclass(frozen=True)
class ForgejoReadiness:
    repository: ForgejoRepository


def _require_segment(value: str, label: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value):
        raise ForgejoConfigurationError(f"Forgejo {label} configuration is invalid.")
    return value


def _require_branch(branch: str) -> str:
    if not branch or branch.startswith("/") or branch.endswith("/"):
        raise ForgejoConfigurationError("Forgejo branch name is invalid.")
    segments = branch.split("/")
    if any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", segment) for segment in segments):
        raise ForgejoConfigurationError("Forgejo branch name is invalid.")
    return branch


def _require_path(path: str) -> str:
    normalized = path.strip().lstrip("/")
    if not normalized or any(part in {"", ".", ".."} for part in normalized.split("/")):
        raise ForgejoConfigurationError("Forgejo file path is invalid.")
    return normalized


class ForgejoClient:
    """A synchronous Forgejo client suitable for FastAPI's threadpool handlers."""

    def __init__(
        self,
        settings: ForgejoSettings,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.settings = settings
        self._http = http_client or httpx.Client(timeout=settings.timeout_seconds)
        self._owns_http_client = http_client is None

    @classmethod
    def from_environment(cls) -> "ForgejoClient":
        return cls(ForgejoSettings.from_environment())

    def close(self) -> None:
        if self._owns_http_client:
            self._http.close()

    def health(self) -> None:
        self._request("GET", "/api/healthz", "health check", authenticated=False)

    def readiness(self) -> ForgejoReadiness:
        self.health()
        return ForgejoReadiness(repository=self.repository_metadata())

    def repository_metadata(self) -> ForgejoRepository:
        response = self._request("GET", self._repository_path(), "repository lookup")
        payload = self._json_object(response, "repository lookup")
        return ForgejoRepository(
            full_name=self._required_string(payload, "full_name", "repository lookup"),
            private=bool(payload.get("private", False)),
            default_branch=self._required_string(payload, "default_branch", "repository lookup"),
            html_url=self._optional_string(payload, "html_url"),
        )

    def read_file(self, path: str, ref: str) -> ForgejoFile:
        normalized_path = _require_path(path)
        if not ref.strip():
            raise ForgejoConfigurationError("Forgejo revision is required.")
        response = self._request(
            "GET",
            f"{self._repository_path()}/contents/{quote(normalized_path, safe='/')}",
            "file read",
            params={"ref": ref},
        )
        payload = self._json_object(response, "file read")
        encoded_content = self._required_string(payload, "content", "file read")
        try:
            content = base64.b64decode(encoded_content.encode("ascii"), validate=False)
        except (UnicodeEncodeError, binascii.Error) as exc:
            raise ForgejoProtocolError("Forgejo returned invalid file content.") from exc
        return ForgejoFile(
            path=self._required_string(payload, "path", "file read"),
            sha=self._required_string(payload, "sha", "file read"),
            content=content,
        )

    def create_branch(self, name: str, from_ref: str) -> ForgejoBranch:
        branch_name = _require_branch(name)
        if not from_ref.strip():
            raise ForgejoConfigurationError("Forgejo source revision is required.")
        response = self._request(
            "POST",
            f"{self._repository_path()}/branches",
            "branch creation",
            json_body={"new_branch_name": branch_name, "old_branch_name": from_ref},
            expected_statuses={201},
        )
        payload = self._json_object(response, "branch creation")
        commit = self._required_object(payload, "commit", "branch creation")
        return ForgejoBranch(
            name=self._required_string(payload, "name", "branch creation"),
            commit_sha=self._required_string(commit, "id", "branch creation"),
        )

    def create_or_update_file(
        self,
        path: str,
        branch: str,
        content: str | bytes,
        message: str,
        current_sha: str | None = None,
    ) -> ForgejoFileCommit:
        normalized_path = _require_path(path)
        branch_name = _require_branch(branch)
        if not message.strip():
            raise ForgejoConfigurationError("Forgejo commit message is required.")
        content_bytes = content.encode("utf-8") if isinstance(content, str) else content
        if not isinstance(content_bytes, bytes):
            raise ForgejoConfigurationError("Forgejo file content is invalid.")
        payload: dict[str, str] = {
            "branch": branch_name,
            "content": base64.b64encode(content_bytes).decode("ascii"),
            "message": message,
        }
        if current_sha:
            payload["sha"] = current_sha
        method = "PUT" if current_sha else "POST"
        response = self._request(
            method,
            f"{self._repository_path()}/contents/{quote(normalized_path, safe='/')}",
            "file commit",
            json_body=payload,
            expected_statuses={200} if current_sha else {201},
        )
        result = self._json_object(response, "file commit")
        committed_file = self._required_object(result, "content", "file commit")
        commit = self._required_object(result, "commit", "file commit")
        return ForgejoFileCommit(
            path=self._required_string(committed_file, "path", "file commit"),
            content_sha=self._required_string(committed_file, "sha", "file commit"),
            commit_sha=self._required_string(commit, "sha", "file commit"),
        )

    def create_pull_request(
        self,
        head: str,
        base: str,
        title: str,
        body: str = "",
    ) -> ForgejoPullRequest:
        head_branch = _require_branch(head)
        base_branch = _require_branch(base)
        if not title.strip():
            raise ForgejoConfigurationError("Forgejo pull request title is required.")
        response = self._request(
            "POST",
            f"{self._repository_path()}/pulls",
            "pull request creation",
            json_body={
                "head": head_branch,
                "base": base_branch,
                "title": title,
                "body": body,
            },
            expected_statuses={201},
        )
        payload = self._json_object(response, "pull request creation")
        return self._pull_request_from_payload(payload, "pull request creation")

    def find_open_pull_request(self, head: str, base: str) -> ForgejoPullRequest | None:
        """Return an existing open pull request for an idempotent workflow."""
        head_branch = _require_branch(head)
        base_branch = _require_branch(base)
        response = self._request(
            "GET",
            f"{self._repository_path()}/pulls",
            "pull request lookup",
            params={
                "state": "open",
                "head": f"{self.settings.owner}:{head_branch}",
                "base": base_branch,
                "limit": "1",
            },
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise ForgejoProtocolError("Forgejo returned an invalid pull request lookup response.") from exc
        if not isinstance(payload, list):
            raise ForgejoProtocolError("Forgejo returned an invalid pull request lookup response.")
        if not payload:
            return None
        first = payload[0]
        if not isinstance(first, dict):
            raise ForgejoProtocolError("Forgejo returned an invalid pull request lookup response.")
        return self._pull_request_from_payload(first, "pull request lookup")

    def pull_request(self, number: int) -> ForgejoPullRequest:
        if not isinstance(number, int) or number <= 0:
            raise ForgejoConfigurationError("Forgejo pull request number is invalid.")
        response = self._request(
            "GET",
            f"{self._repository_path()}/pulls/{number}",
            "pull request lookup",
        )
        return self._pull_request_from_payload(self._json_object(response, "pull request lookup"), "pull request lookup")

    def file_history(self, path: str, ref: str, limit: int = 50) -> list[ForgejoHistoryEntry]:
        normalized_path = _require_path(path)
        if not ref.strip():
            raise ForgejoConfigurationError("Forgejo revision is required.")
        if not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ForgejoConfigurationError("Forgejo history limit is invalid.")
        response = self._request(
            "GET",
            f"{self._repository_path()}/commits",
            "file history lookup",
            params={"path": normalized_path, "sha": ref, "limit": str(limit)},
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise ForgejoProtocolError("Forgejo returned an invalid file history lookup response.") from exc
        if not isinstance(payload, list):
            raise ForgejoProtocolError("Forgejo returned an invalid file history lookup response.")

        history: list[ForgejoHistoryEntry] = []
        for item in payload:
            if not isinstance(item, dict):
                raise ForgejoProtocolError("Forgejo returned an invalid file history lookup response.")
            commit = self._required_object(item, "commit", "file history lookup")
            author = commit.get("author")
            authored_at = None
            if isinstance(author, dict):
                authored_at = self._optional_string(author, "date")
            history.append(
                ForgejoHistoryEntry(
                    sha=self._required_string(item, "sha", "file history lookup"),
                    message=self._required_string(commit, "message", "file history lookup"),
                    authored_at=authored_at,
                )
            )
        return history

    def revision(self, ref: str) -> ForgejoRevision:
        if not ref.strip():
            raise ForgejoConfigurationError("Forgejo revision is required.")
        if not re.fullmatch(r"[0-9a-fA-F]{7,64}", ref):
            return self._branch_revision(ref)
        response = self._request(
            "GET",
            f"{self._repository_path()}/git/commits/{quote(ref, safe='')}",
            "revision lookup",
        )
        payload = self._json_object(response, "revision lookup")
        commit = self._required_object(payload, "commit", "revision lookup")
        return ForgejoRevision(
            sha=self._required_string(payload, "sha", "revision lookup"),
            message=self._required_string(commit, "message", "revision lookup"),
        )

    def _branch_revision(self, branch: str) -> ForgejoRevision:
        branch_name = _require_branch(branch)
        response = self._request(
            "GET",
            f"{self._repository_path()}/git/refs/heads/{quote(branch_name, safe='/')}",
            "branch revision lookup",
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise ForgejoProtocolError("Forgejo returned an invalid branch revision lookup response.") from exc
        if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
            raise ForgejoProtocolError("Forgejo returned an invalid branch revision lookup response.")
        reference = payload[0]
        object_payload = self._required_object(reference, "object", "branch revision lookup")
        return ForgejoRevision(
            sha=self._required_string(object_payload, "sha", "branch revision lookup"),
            message="",
        )

    @classmethod
    def _pull_request_from_payload(cls, payload: dict[str, Any], operation: str) -> ForgejoPullRequest:
        head_payload = cls._required_object(payload, "head", operation)
        base_payload = cls._required_object(payload, "base", operation)
        merged = payload.get("merged", False)
        if not isinstance(merged, bool):
            raise ForgejoProtocolError(f"Forgejo returned an invalid {operation} response.")
        merge_commit_sha = cls._optional_string(payload, "merge_commit_sha")
        merged_at = cls._optional_string(payload, "merged_at")
        return ForgejoPullRequest(
            number=cls._required_integer(payload, "number", operation),
            url=cls._optional_string(payload, "html_url"),
            head=cls._required_string(head_payload, "ref", operation),
            base=cls._required_string(base_payload, "ref", operation),
            state=cls._required_string(payload, "state", operation),
            merged=merged,
            merge_commit_sha=merge_commit_sha,
            merged_at=merged_at,
        )

    def _repository_path(self) -> str:
        return f"/api/v1/repos/{quote(self.settings.owner, safe='')}/{quote(self.settings.repository, safe='')}"

    def _request(
        self,
        method: str,
        path: str,
        operation: str,
        *,
        params: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
        expected_statuses: set[int] | None = None,
        authenticated: bool = True,
    ) -> httpx.Response:
        headers = {"Accept": "application/json"}
        if authenticated:
            headers["Authorization"] = f"token {self._read_token()}"
        try:
            response = self._http.request(
                method,
                f"{self.settings.base_url}{path}",
                headers=headers,
                params=params,
                json=json_body,
                timeout=self.settings.timeout_seconds,
            )
        except httpx.HTTPError as exc:
            raise ForgejoUnavailableError(f"Forgejo {operation} is unavailable.") from exc
        allowed = expected_statuses or {200}
        if response.status_code not in allowed:
            if response.status_code in {401, 403}:
                raise ForgejoRequestError(f"Forgejo {operation} was not authorized.", response.status_code)
            raise ForgejoRequestError(f"Forgejo {operation} failed.", response.status_code)
        return response

    def _read_token(self) -> str:
        try:
            token = self.settings.token_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ForgejoConfigurationError("Forgejo service credential is unavailable.") from exc
        if not token:
            raise ForgejoConfigurationError("Forgejo service credential is unavailable.")
        return token

    @staticmethod
    def _json_object(response: httpx.Response, operation: str) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise ForgejoProtocolError(f"Forgejo returned an invalid {operation} response.") from exc
        if not isinstance(payload, dict):
            raise ForgejoProtocolError(f"Forgejo returned an invalid {operation} response.")
        return payload

    @staticmethod
    def _required_object(payload: dict[str, Any], key: str, operation: str) -> dict[str, Any]:
        value = payload.get(key)
        if not isinstance(value, dict):
            raise ForgejoProtocolError(f"Forgejo returned an invalid {operation} response.")
        return value

    @staticmethod
    def _required_string(payload: dict[str, Any], key: str, operation: str) -> str:
        value = payload.get(key)
        if not isinstance(value, str) or not value:
            raise ForgejoProtocolError(f"Forgejo returned an invalid {operation} response.")
        return value

    @staticmethod
    def _optional_string(payload: dict[str, Any], key: str) -> str | None:
        value = payload.get(key)
        return value if isinstance(value, str) and value else None

    @staticmethod
    def _required_integer(payload: dict[str, Any], key: str, operation: str) -> int:
        value = payload.get(key)
        if not isinstance(value, int):
            raise ForgejoProtocolError(f"Forgejo returned an invalid {operation} response.")
        return value
