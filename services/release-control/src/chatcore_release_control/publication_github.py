"""Fixed-origin publication with exact-object, non-force Git push adapters."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qs, urljoin, urlsplit

import httpx

from .github_app import GitHubAppError, InstallationToken
from .publication import PublicationError, PublishCommand, PublishResult

_API = "https://api.github.com"
_WEB = "https://github.com"
_API_VERSION = "2022-11-28"
_MAX_RESPONSE = 1024 * 1024
_MAX_BODY = 256 * 1024
_MAX_SECONDS = 15.0
_MAX_PAGES = 20
_OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_REQUEST_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,63}\Z")
_TOKEN = re.compile(r"[A-Za-z0-9_.,-]{1,4096}\Z")
_REPOSITORY_PART = re.compile(r"[A-Za-z0-9_.-]{1,100}\Z")


def _valid_publication_ref(ref: str) -> bool:
    if not isinstance(ref, str):
        return False
    final = re.fullmatch(r"refs/heads/clawdot/([a-z0-9][a-z0-9-]{0,63})", ref)
    staging = re.fullmatch(
        r"refs/heads/clawdot/objects/([a-z0-9][a-z0-9-]{0,63})/([0-9a-f]{40}|[0-9a-f]{64})",
        ref,
    )
    return final is not None or staging is not None


class GitHubPublicationError(ValueError):
    """Safe bounded failure code for GitHub API and Git operations."""

    def __init__(self, code: str = "github_publication_failed") -> None:
        if code not in {
            "invalid_configuration",
            "repository_mismatch",
            "remote_ref_mismatch",
            "pull_request_mismatch",
            "request_failed",
            "invalid_response",
            "git_push_failed",
            "publication_cleanup_pending",
        }:
            code = "github_publication_failed"
        super().__init__(code)
        self.code = code


class TokenIssuer(Protocol):
    def get_token(self, *, force_refresh: bool = False) -> InstallationToken: ...


GitExecutor = Callable[[Sequence[str], Mapping[str, str], float, int], Any]


@dataclass(frozen=True, repr=False)
class PullRequestReceipt:
    pull_request_number: int
    request_id: str
    head_sha: str
    base_sha: str
    head_ref: str
    base_ref: str
    state: str

    def __repr__(self) -> str:
        return (
            "PullRequestReceipt(pull_request_number="
            f"{self.pull_request_number}, request_id={self.request_id!r}, "
            f"head_sha={self.head_sha!r}, base_sha={self.base_sha!r}, state={self.state!r})"
        )


class GitHubAppHttpPost:
    """Bounded concrete POST adapter for the App installation-token endpoint."""

    def __init__(
        self,
        *,
        installation_id: int,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if type(installation_id) is not int or installation_id <= 0:
            raise GitHubPublicationError("invalid_configuration")
        self.installation_id = installation_id
        self._client = httpx.Client(
            transport=transport,
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(5.0, connect=2.0),
        )

    def __call__(
        self,
        url: str,
        headers: Mapping[str, str],
        body: Mapping[str, object],
        /,
    ) -> Mapping[str, object]:
        expected = f"{_API}/app/installations/{self.installation_id}/access_tokens"
        if (
            url != expected
            or not isinstance(headers, Mapping)
            or not isinstance(body, Mapping)
            or len(_canonical(body)) > 16 * 1024
        ):
            raise GitHubPublicationError("invalid_configuration")
        try:
            response = self._send("POST", expected, headers=headers, content=_canonical(body))
            if response.status_code not in {200, 201}:
                raise GitHubPublicationError("request_failed")
            return _decode_object(response.content)
        except GitHubPublicationError:
            raise
        except Exception:
            raise GitHubPublicationError("request_failed") from None

    def close(self) -> None:
        self._client.close()

    def _send(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        content: bytes,
        params: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        request = self._client.build_request(
            method,
            url,
            headers=headers,
            content=content,
            params=params,
        )
        response = self._client.send(request, stream=True)
        data = bytearray()
        started = time.monotonic()
        try:
            if response.status_code < 200 or response.status_code >= 300:
                response.close()
                raise GitHubPublicationError("request_failed")
            for chunk in response.iter_bytes(16 * 1024):
                if (
                    len(data) + len(chunk) > _MAX_RESPONSE
                    or time.monotonic() - started > _MAX_SECONDS
                ):
                    raise GitHubPublicationError("invalid_response")
                data.extend(chunk)
        finally:
            response.close()
        response._content = bytes(data)
        return response


class GitHubPublicationGateway:
    """Verified repository API client plus exact frozen commit Git push."""

    def __init__(
        self,
        *,
        owner: str,
        repository: str,
        repository_id: int,
        owner_id: int,
        token_issuer: TokenIssuer,
        transport: httpx.BaseTransport | None = None,
        git_executable: str | None = None,
        git_executor: GitExecutor | None = None,
        temporary_directory: str | os.PathLike[str] | None = None,
    ) -> None:
        git = git_executable or shutil.which("git")
        if (
            not isinstance(owner, str)
            or not _REPOSITORY_PART.fullmatch(owner)
            or not isinstance(repository, str)
            or not _REPOSITORY_PART.fullmatch(repository)
            or type(repository_id) is not int
            or repository_id <= 0
            or type(owner_id) is not int
            or owner_id <= 0
            or not callable(getattr(token_issuer, "get_token", None))
            or not git
            or not Path(git).is_absolute()
        ):
            raise GitHubPublicationError("invalid_configuration")
        self.owner = owner
        self.repository = repository
        self.repository_id = repository_id
        self.owner_id = owner_id
        self._issuer = token_issuer
        self._git = git
        self._executor = git_executor or _subprocess_git
        self._tmp = Path(temporary_directory).resolve(strict=True) if temporary_directory else None
        if self._tmp is not None and not self._tmp.is_dir():
            raise GitHubPublicationError("invalid_configuration")
        self._client = httpx.Client(
            transport=transport,
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(8.0, connect=3.0),
        )
        self._verified = False
        self._started = 0.0

    def __repr__(self) -> str:
        return f"GitHubPublicationGateway(repository_id={self.repository_id})"

    def close(self) -> None:
        self._client.close()
        close = getattr(getattr(self._issuer, "_http_post", None), "close", None)
        if callable(close):
            close()

    def verify_repository(self) -> None:
        payload = self._api_json("GET", f"/repos/{self.owner}/{self.repository}")
        owner = payload.get("owner")
        if (
            type(payload.get("id")) is not int
            or payload["id"] != self.repository_id
            or payload.get("full_name") != f"{self.owner}/{self.repository}"
            or payload.get("private") is not True
            or not isinstance(owner, dict)
            or type(owner.get("id")) is not int
            or owner["id"] != self.owner_id
            or owner.get("login", "").casefold() != self.owner.casefold()
        ):
            raise GitHubPublicationError("repository_mismatch")
        self._verified = True

    def publish(self, command: PublishCommand, /) -> PublishResult:
        self._require_verified()
        if not isinstance(command, PublishCommand):
            raise PublicationError("publication_failed")
        expected_ref = f"refs/heads/clawdot/{command.request_id}"
        staging_ref = f"refs/heads/clawdot/objects/{command.request_id}/{command.frozen_commit}"
        if (
            command.repository_id != self.repository_id
            or not _REQUEST_ID.fullmatch(command.request_id)
            or not _OID.fullmatch(command.frozen_commit)
            or command.refspec != f"{command.frozen_commit}:{expected_ref}"
        ):
            raise PublicationError("publication_mismatch")
        current = self.get_ref(command.request_id)
        if current is not None:
            if current == command.frozen_commit:
                self._cleanup_staging_ref(staging_ref, command.frozen_commit)
                return PublishResult(current, expected_ref)
            raise PublicationError("publication_mismatch")
        source = Path(command.quarantine_git_dir)
        try:
            source = source.resolve(strict=True)
        except (OSError, RuntimeError):
            raise PublicationError("publication_failed") from None
        if (
            not source.is_dir()
            or not (source / "objects").is_dir()
            or not (source / "HEAD").is_file()
            or (source / "objects" / "info" / "alternates").exists()
        ):
            raise PublicationError("publication_failed")

        with tempfile.TemporaryDirectory(
            prefix="publication-push-", dir=str(self._tmp) if self._tmp else None
        ) as temporary:
            root = Path(temporary)
            push_git = root / "push.git"
            token_path = root / "token"
            askpass = root / "askpass"
            self._write_token(token_path)
            self._write_askpass(askpass, token_path)
            env = {
                "PATH": str(Path(self._git).parent),
                "HOME": str(root),
                "TMPDIR": str(root),
                "LC_ALL": "C",
                "GIT_DIR": str(push_git),
                "GIT_OBJECT_DIRECTORY": str(push_git / "objects"),
                "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(source / "objects"),
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_SYSTEM": os.devnull,
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_ASKPASS": str(askpass),
                "GIT_ASKPASS_REQUIRE": "force",
                "GITHUB_APP_TOKEN_FILE": str(token_path),
            }
            self._git_run(
                root,
                env,
                "init",
                "--bare",
                "--quiet",
                _object_format(len(command.frozen_commit)),
                str(push_git),
            )
            try:
                self._git_run(
                    root,
                    env,
                    "-c",
                    "core.hooksPath=/dev/null",
                    "-c",
                    "http.followRedirects=false",
                    "-c",
                    "protocol.allow=never",
                    "push",
                    "--porcelain",
                    "--no-follow-tags",
                    "--no-recurse-submodules",
                    "--no-verify",
                    "--",
                    f"{_WEB}/{self.owner}/{self.repository}.git",
                    f"{command.frozen_commit}:{staging_ref}",
                )
            except GitHubPublicationError:
                # A lost upload reply is reconciled only against the deterministic stage ref.
                if self._read_remote_ref(staging_ref) != command.frozen_commit:
                    raise PublicationError("publication_failed") from None
            if self._read_remote_ref(staging_ref) != command.frozen_commit:
                raise PublicationError("publication_mismatch")

        try:
            self._api_json(
                "POST",
                f"/repos/{self.owner}/{self.repository}/git/refs",
                body={"ref": expected_ref, "sha": command.frozen_commit},
                expected={201},
            )
        except GitHubPublicationError:
            # GitHub's create-ref endpoint is the final absent-ref compare-and-set.
            current = self.get_ref(command.request_id)
            if current != command.frozen_commit:
                if current is not None:
                    raise PublicationError("publication_mismatch") from None
                raise PublicationError("publication_failed") from None
        current = self.get_ref(command.request_id)
        if current != command.frozen_commit:
            raise PublicationError("publication_mismatch")
        self._cleanup_staging_ref(staging_ref, command.frozen_commit)
        return PublishResult(current, expected_ref)

    def get_ref(self, request_id: str) -> str | None:
        self._require_verified()
        if not isinstance(request_id, str) or not _REQUEST_ID.fullmatch(request_id):
            raise GitHubPublicationError("invalid_configuration")
        return self._read_remote_ref(f"refs/heads/clawdot/{request_id}")

    def _read_remote_ref(self, ref: str) -> str | None:
        if not _valid_publication_ref(ref):
            raise GitHubPublicationError("invalid_configuration")
        api_ref = ref.removeprefix("refs/heads/")
        try:
            payload = self._api_json(
                "GET",
                f"/repos/{self.owner}/{self.repository}/git/ref/heads/{api_ref}",
                allow_not_found=True,
            )
        except GitHubPublicationError:
            raise
        if payload is None:
            return None
        value = payload.get("object")
        if (
            payload.get("ref") != ref
            or not isinstance(value, dict)
            or value.get("type") != "commit"
            or not isinstance(value.get("sha"), str)
            or not _OID.fullmatch(value["sha"])
        ):
            raise GitHubPublicationError("invalid_response")
        return value["sha"]

    def _cleanup_staging_ref(self, ref: str, expected_sha: str) -> None:
        try:
            current = self._read_remote_ref(ref)
            if current is None:
                return
            if current != expected_sha:
                raise GitHubPublicationError("publication_cleanup_pending")
            self._api_json_with_headers(
                "DELETE",
                f"/repos/{self.owner}/{self.repository}/git/refs/{ref.removeprefix('refs/')}",
                expected={204},
                allow_not_found=True,
            )
        except GitHubPublicationError:
            raise GitHubPublicationError("publication_cleanup_pending") from None

    def create_or_get_pull_request(
        self,
        *,
        request_id: str,
        head_sha: str,
        base_sha: str,
        title: str,
        body: str,
        base_branch: str = "main",
    ) -> PullRequestReceipt:
        self._require_verified()
        _validate_pr_inputs(request_id, head_sha, base_sha, title, body, base_branch)
        if self.get_ref(request_id) != head_sha:
            raise GitHubPublicationError("remote_ref_mismatch")
        found = self._find_pull_requests(request_id, base_branch)
        if found:
            return _one_matching_pr(found, request_id, head_sha, base_sha, title, body, self)
        payload = {
            "title": title,
            "head": f"{self.owner}:clawdot/{request_id}",
            "base": base_branch,
            "body": body,
            "maintainer_can_modify": False,
            "draft": False,
        }
        try:
            created = self._api_json(
                "POST", f"/repos/{self.owner}/{self.repository}/pulls", body=payload, expected={201}
            )
        except GitHubPublicationError:
            # A timeout or lost 201 is resolved by exhaustive exact-branch lookup.
            reconciled = self._find_pull_requests(request_id, base_branch)
            if reconciled:
                return _one_matching_pr(
                    reconciled, request_id, head_sha, base_sha, title, body, self
                )
            raise
        return _parse_pr(created, request_id, head_sha, base_sha, title, body, self)

    def _find_pull_requests(self, request_id: str, base_branch: str) -> list[object]:
        found: list[object] = []
        page = 1
        while page <= _MAX_PAGES:
            payload, headers = self._api_json_with_headers(
                "GET",
                f"/repos/{self.owner}/{self.repository}/pulls",
                params={
                    "state": "all",
                    "head": f"{self.owner}:clawdot/{request_id}",
                    "base": base_branch,
                    "per_page": "100",
                    "page": str(page),
                },
                expected={200},
            )
            if not isinstance(payload, list) or len(payload) > 100:
                raise GitHubPublicationError("invalid_response")
            found.extend(payload)
            next_url = _next_link(
                headers.get("Link"), f"/repos/{self.owner}/{self.repository}/pulls"
            )
            if next_url is None:
                if len(payload) == 100:
                    raise GitHubPublicationError("invalid_response")
                return found
            query = parse_qs(urlsplit(next_url).query)
            next_page = query.get("page", [])
            if next_page != [str(page + 1)]:
                raise GitHubPublicationError("invalid_response")
            page += 1
        raise GitHubPublicationError("invalid_response")

    def _api_json(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        body: Mapping[str, object] | None = None,
        expected: set[int] | None = None,
        allow_not_found: bool = False,
    ) -> dict[str, Any] | None:
        expected = {200} if expected is None else expected
        payload, _ = self._api_json_with_headers(
            method,
            path,
            params=params,
            body=body,
            expected=expected,
            allow_not_found=allow_not_found,
        )
        if payload is None and allow_not_found:
            return None
        if not isinstance(payload, dict):
            raise GitHubPublicationError("invalid_response")
        return payload

    def _api_json_with_headers(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        body: Mapping[str, object] | None = None,
        expected: set[int] | None = None,
        allow_not_found: bool = False,
    ) -> tuple[Any, Mapping[str, str]]:
        expected = {200} if expected is None else expected
        if not path.startswith("/repos/") or ".." in path or len(path) > 2048:
            raise GitHubPublicationError("invalid_configuration")
        token = self._get_token()
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": _API_VERSION,
            "Content-Type": "application/json",
        }
        content = b"" if body is None else _canonical(body)
        if len(content) > _MAX_BODY:
            raise GitHubPublicationError("invalid_configuration")
        request = self._client.build_request(
            method, _API + path, params=params, headers=headers, content=content
        )
        started = time.monotonic()
        try:
            response = self._client.send(request, stream=True)
            raw = bytearray()
            try:
                if response.status_code == 404 and allow_not_found:
                    return None, response.headers
                if response.status_code not in expected:
                    raise GitHubPublicationError("request_failed")
                if response.status_code == 204:
                    if next(iter(response.iter_bytes(16 * 1024)), b""):
                        raise GitHubPublicationError("invalid_response")
                    return None, response.headers
                for chunk in response.iter_bytes(16 * 1024):
                    if (
                        len(raw) + len(chunk) > _MAX_RESPONSE
                        or time.monotonic() - started > _MAX_SECONDS
                    ):
                        raise GitHubPublicationError("invalid_response")
                    raw.extend(chunk)
                decoded = json.loads(bytes(raw), object_pairs_hook=_unique_pairs)
                return decoded, response.headers
            finally:
                response.close()
        except GitHubPublicationError:
            raise
        except Exception:
            raise GitHubPublicationError("request_failed") from None

    def _get_token(self) -> str:
        try:
            token = self._issuer.get_token().access_token
        except (GitHubAppError, AttributeError):
            raise GitHubPublicationError("request_failed") from None
        if not isinstance(token, str) or not _TOKEN.fullmatch(token):
            raise GitHubPublicationError("request_failed")
        return token

    def _require_verified(self) -> None:
        if not self._verified:
            raise GitHubPublicationError("repository_mismatch")

    def _git_run(self, root: Path, env: dict[str, str], *args: str) -> bytes:
        argv = [
            self._git,
            "--git-dir",
            env["GIT_DIR"],
            "--no-pager",
            "--no-replace-objects",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "http.followRedirects=false",
            "-c",
            "protocol.allow=never",
            "-c",
            "protocol.https.allow=always",
            *args,
        ]
        try:
            result = self._executor(argv, env, _MAX_SECONDS, 64 * 1024)
        except Exception:
            raise GitHubPublicationError("git_push_failed") from None
        if (
            not hasattr(result, "returncode")
            or not hasattr(result, "stdout")
            or not hasattr(result, "stderr")
            or type(result.returncode) is not int
            or not isinstance(result.stdout, bytes)
            or not isinstance(result.stderr, bytes)
            or len(result.stdout) > 64 * 1024
            or len(result.stderr) > 64 * 1024
        ):
            raise GitHubPublicationError("git_push_failed")
        if result.returncode != 0:
            raise GitHubPublicationError("git_push_failed")
        return result.stdout

    def _write_token(self, path: Path) -> None:
        token = self._get_token()
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags, 0o600)
            try:
                view = memoryview(token.encode("ascii"))
                while view:
                    written = os.write(descriptor, view)
                    if written <= 0:
                        raise OSError
                    view = view[written:]
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError:
            raise GitHubPublicationError("git_push_failed") from None

    @staticmethod
    def _write_askpass(path: Path, token_path: Path) -> None:
        token_literal = repr(str(token_path))
        script = (
            f"#!{os.sys.executable}\n"
            "import pathlib, sys\n"
            "prompt = sys.argv[1].casefold() if len(sys.argv) == 2 else ''\n"
            "if 'username' in prompt:\n    print('x-access-token')\n"
            "elif 'password' in prompt:\n"
            f"    print(pathlib.Path({token_literal}).read_text().strip())\n"
            "else:\n    raise SystemExit(1)\n"
        ).encode()
        try:
            descriptor = os.open(
                path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o700,
            )
            try:
                os.write(descriptor, script)
                os.fchmod(descriptor, 0o700)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError:
            raise GitHubPublicationError("git_push_failed") from None


def _parse_pr(
    value: object,
    request_id: str,
    head_sha: str,
    base_sha: str,
    title: str,
    body: str,
    gateway: GitHubPublicationGateway,
) -> PullRequestReceipt:
    if not isinstance(value, dict):
        raise GitHubPublicationError("invalid_response")
    number = value.get("number")
    head = value.get("head")
    base = value.get("base")
    head_repo = head.get("repo") if isinstance(head, dict) else None
    base_repo = base.get("repo") if isinstance(base, dict) else None
    head_owner = head_repo.get("owner") if isinstance(head_repo, dict) else None
    base_owner = base_repo.get("owner") if isinstance(base_repo, dict) else None
    valid = (
        type(number) is int
        and number > 0
        and value.get("state") == "open"
        and value.get("merged_at") is None
        and value.get("title") == title
        and value.get("body") == body
        and isinstance(head, dict)
        and head.get("sha") == head_sha
        and head.get("ref") == f"clawdot/{request_id}"
        and isinstance(head_repo, dict)
        and head_repo.get("id") == gateway.repository_id
        and isinstance(head_owner, dict)
        and head_owner.get("id") == gateway.owner_id
        and isinstance(base, dict)
        and base.get("sha") == base_sha
        and base.get("ref") == "main"
        and isinstance(base_repo, dict)
        and base_repo.get("id") == gateway.repository_id
        and isinstance(base_owner, dict)
        and base_owner.get("id") == gateway.owner_id
    )
    if not valid:
        raise GitHubPublicationError("pull_request_mismatch")
    return PullRequestReceipt(
        number, request_id, head_sha, base_sha, head["ref"], base["ref"], "open"
    )


def _one_matching_pr(
    values: list[object],
    request_id: str,
    head_sha: str,
    base_sha: str,
    title: str,
    body: str,
    gateway: GitHubPublicationGateway,
) -> PullRequestReceipt:
    if len(values) != 1:
        raise GitHubPublicationError("pull_request_mismatch")
    return _parse_pr(values[0], request_id, head_sha, base_sha, title, body, gateway)


def _validate_pr_inputs(
    request_id: str, head_sha: str, base_sha: str, title: str, body: str, base_branch: str
) -> None:
    if (
        not isinstance(request_id, str)
        or not _REQUEST_ID.fullmatch(request_id)
        or not isinstance(head_sha, str)
        or not _OID.fullmatch(head_sha)
        or not isinstance(base_sha, str)
        or not _OID.fullmatch(base_sha)
        or len(head_sha) != len(base_sha)
        or not isinstance(title, str)
        or not title.strip()
        or not isinstance(body, str)
        or max(len(title.encode("utf-8")), len(body.encode("utf-8"))) > 64 * 1024
        or not isinstance(base_branch, str)
        or base_branch != "main"
    ):
        raise GitHubPublicationError("invalid_configuration")


def _next_link(value: str | None, expected_path: str) -> str | None:
    if value is None:
        return None
    links = []
    for part in value.split(","):
        match = re.fullmatch(r'\s*<([^>]+)>;\s*rel="([a-z]+)"\s*', part)
        if match is None:
            raise GitHubPublicationError("invalid_response")
        if match.group(2) == "next":
            url = urljoin(_API, match.group(1))
            parsed = urlsplit(url)
            if (
                parsed.scheme != "https"
                or parsed.netloc != "api.github.com"
                or parsed.path != expected_path
                or parsed.fragment
            ):
                raise GitHubPublicationError("invalid_response")
            links.append(url)
    if len(links) > 1:
        raise GitHubPublicationError("invalid_response")
    return links[0] if links else None


def _canonical(value: Mapping[str, object]) -> bytes:
    try:
        return json.dumps(
            value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise GitHubPublicationError("invalid_configuration") from None


def _decode_object(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw, object_pairs_hook=_unique_pairs)
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (ValueError, TypeError, UnicodeError, json.JSONDecodeError, RecursionError):
        raise GitHubPublicationError("invalid_response") from None


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _object_format(oid_length: int) -> str:
    return f"--object-format={'sha256' if oid_length == 64 else 'sha1'}"


def _subprocess_git(
    argv: Sequence[str], env: Mapping[str, str], timeout: float, max_output_bytes: int
) -> Any:
    from .development import CommandResult

    try:
        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            process = subprocess.Popen(  # noqa: S603 - fixed Git argv and private environment.
                list(argv),
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                env=dict(env),
                close_fds=True,
                preexec_fn=_limit_git_output,
            )
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                raise GitHubPublicationError("git_push_failed") from None
            stdout.seek(0, os.SEEK_END)
            stderr.seek(0, os.SEEK_END)
            if stdout.tell() > max_output_bytes or stderr.tell() > max_output_bytes:
                raise GitHubPublicationError("git_push_failed")
            stdout.seek(0)
            stderr.seek(0)
            return CommandResult(
                process.returncode,
                stdout.read(max_output_bytes + 1),
                stderr.read(max_output_bytes + 1),
            )
    except GitHubPublicationError:
        raise
    except (OSError, subprocess.SubprocessError):
        raise GitHubPublicationError("git_push_failed") from None


def _limit_git_output() -> None:
    import resource

    resource.setrlimit(resource.RLIMIT_FSIZE, (64 * 1024, 64 * 1024))
