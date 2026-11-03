from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest
from chatcore_release_control.git_evidence import collect_release_bundle
from chatcore_release_control.git_fetch import GitFetchError, GitObjectFetcher
from chatcore_release_control.policy import PolicyManifest, TrustedCheck


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["/usr/bin/git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(cwd),
            "LC_ALL": "C",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
        },
    )
    return result.stdout.decode("ascii").strip()


def _source(tmp_path: Path, *, head_value: str = "H", bad_history: bool = False):
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "--quiet")
    _git(source, "config", "user.name", "Fixture")
    _git(source, "config", "user.email", "fixture@example.invalid")
    (source / "app.py").write_text("D\n")
    _git(source, "add", ".")
    _git(source, "commit", "--quiet", "-m", "deployed")
    deployed = _git(source, "rev-parse", "HEAD")
    (source / "app.py").write_text("B\n")
    _git(source, "commit", "--quiet", "-am", "base")
    base = _git(source, "rev-parse", "HEAD")
    if bad_history:
        (source / "private.ini").write_text("private fixture data\n")
        _git(source, "add", "private.ini")
        _git(source, "commit", "--quiet", "-m", "introduce private fixture")
        (source / "private.ini").unlink()
        _git(source, "add", "-u")
        _git(source, "commit", "--quiet", "-m", "remove private fixture")
    (source / "app.py").write_text(f"{head_value}\n")
    _git(source, "commit", "--quiet", "-am", "candidate")
    head = _git(source, "rev-parse", "HEAD")
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", "--quiet", str(remote))
    _git(source, "push", "--quiet", str(remote), f"{head}:refs/pull/73/head")
    objects = tmp_path / "object-store"
    (objects / "objects/info").mkdir(parents=True, mode=0o700)
    (objects / "objects/pack").mkdir(mode=0o700)
    return source, remote, objects, deployed, base, head


class _LocalGitExecutor:
    def __init__(self, remote: Path, *, fail_fetch: bool = False) -> None:
        self.remote = remote
        self.fail_fetch = fail_fetch
        self.calls: list[tuple[str, ...]] = []
        self.token_modes: list[int] = []
        self.askpass_modes: list[int] = []

    def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout: float,
        output_limit: int = 65536,
    ) -> bytes:
        call = tuple(args)
        self.calls.append(call)
        assert timeout > 0
        assert env["GIT_DIR"].endswith("quarantine.git")
        assert "synthetic-git-token-not-real" not in " ".join(call)
        assert all("synthetic-git-token-not-real" not in item for item in env.values())
        token_file = Path(env["GIT_RELEASE_CONTROL_TOKEN_FILE"])
        askpass = Path(env["GIT_ASKPASS"])
        if call[-1] == "fetch" or "fetch" in call:
            self.token_modes.append(token_file.stat().st_mode & 0o777)
            self.askpass_modes.append(askpass.stat().st_mode & 0o777)
            if self.fail_fetch:
                raise GitFetchError("git_fetch_timeout")
            mutable = list(call)
            remote_index = mutable.index("https://github.com/acme/plugin.git")
            mutable[remote_index] = str(self.remote)
            config_index = mutable.index("protocol.https.allow=always")
            mutable[config_index] = "protocol.file.allow=always"
            result = subprocess.run(
                mutable,
                cwd=cwd,
                env=dict(env),
                check=True,
                capture_output=True,
                timeout=timeout,
            )
            return result.stdout[:output_limit]
        result = subprocess.run(
            list(call),
            cwd=cwd,
            env=dict(env),
            check=True,
            capture_output=True,
            timeout=timeout,
        )
        if len(result.stdout) > output_limit or len(result.stderr) > output_limit:
            raise GitFetchError("git_fetch_size_limit")
        return result.stdout


def _policy() -> PolicyManifest:
    return PolicyManifest(
        repository_id=101,
        owner_id=202,
        target="production",
        workflow_digests=((".github/workflows/ci.yml", "a" * 64),),
        allowed_paths=("app.py",),
        protected_paths=(".github/workflows/ci.yml",),
        checks=(TrustedCheck("required", 303, ".github/workflows/ci.yml", "a" * 64),),
    )


def test_fetches_exact_unshallow_pr_history_and_promotes_only_verified_pack(tmp_path):
    _source_repo, remote, object_repository, deployed, base, head = _source(tmp_path)
    executor = _LocalGitExecutor(remote)
    fetcher = GitObjectFetcher(
        repository=object_repository,
        owner="acme",
        name="plugin",
        token="synthetic-git-token-not-real",
        git_executable="/usr/bin/git",
        executor=executor,
    )

    fetcher.ensure_history(
        pull_request_number=73,
        deployed_commit=deployed,
        base_commit=base,
        head_commit=head,
        policy=_policy(),
    )

    bundle = collect_release_bundle(object_repository, deployed, base, head, _policy())
    assert bundle.base_is_ancestor_of_head and bundle.base_is_descendant_of_deployed
    assert executor.token_modes == [0o600]
    assert executor.askpass_modes == [0o700]
    fetch_call = next(call for call in executor.calls if "fetch" in call)
    assert "https://github.com/acme/plugin.git" in fetch_call
    assert "+refs/pull/73/head:refs/ci/pull/73/head" in fetch_call
    assert all(flag not in fetch_call for flag in ("--depth", "--filter", "--mirror", "--force"))
    assert list((object_repository / "objects/pack").glob("*.pack"))
    assert list((object_repository / "objects/pack").glob("*.idx"))
    assert not (object_repository / "objects/info/alternates").exists()


def test_missing_objects_fetch_timeout_does_not_promote_objects(tmp_path):
    _source_repo, remote, object_repository, deployed, base, head = _source(tmp_path)
    executor = _LocalGitExecutor(remote, fail_fetch=True)
    fetcher = GitObjectFetcher(
        repository=object_repository,
        owner="acme",
        name="plugin",
        token="synthetic-git-token-not-real",
        git_executable="/usr/bin/git",
        executor=executor,
    )

    with pytest.raises(GitFetchError, match="git_fetch_timeout"):
        fetcher.ensure_history(
            pull_request_number=73,
            deployed_commit=deployed,
            base_commit=base,
            head_commit=head,
            policy=_policy(),
        )

    assert not list((object_repository / "objects/pack").glob("*.pack"))


def test_complete_fetch_rejects_unapproved_path_added_and_removed_in_history(tmp_path):
    _source_repo, remote, object_repository, deployed, base, head = _source(
        tmp_path, bad_history=True
    )
    executor = _LocalGitExecutor(remote)
    fetcher = GitObjectFetcher(
        repository=object_repository,
        owner="acme",
        name="plugin",
        token="synthetic-git-token-not-real",
        git_executable="/usr/bin/git",
        executor=executor,
    )

    with pytest.raises(GitFetchError, match="git_fetch_policy_denied"):
        fetcher.ensure_history(
            pull_request_number=73,
            deployed_commit=deployed,
            base_commit=base,
            head_commit=head,
            policy=_policy(),
        )

    assert not list((object_repository / "objects/pack").glob("*.pack"))


def test_already_materialized_policy_valid_history_skips_remote_fetch(tmp_path):
    _source_repo, _remote, object_repository, deployed, base, head = _source(tmp_path)
    shutil.copytree(
        Path(_source_repo / ".git/objects/pack"),
        object_repository / "objects/pack",
        dirs_exist_ok=True,
    )
    for file in Path(_source_repo / ".git/objects").iterdir():
        if len(file.name) == 2 and file.is_dir():
            shutil.copytree(file, object_repository / "objects" / file.name, dirs_exist_ok=True)
    executor = _LocalGitExecutor(Path("/unused"))
    fetcher = GitObjectFetcher(
        repository=object_repository,
        owner="acme",
        name="plugin",
        token="synthetic-git-token-not-real",
        git_executable="/usr/bin/git",
        executor=executor,
    )

    fetcher.ensure_history(
        pull_request_number=73,
        deployed_commit=deployed,
        base_commit=base,
        head_commit=head,
        policy=_policy(),
    )

    assert executor.calls == []
