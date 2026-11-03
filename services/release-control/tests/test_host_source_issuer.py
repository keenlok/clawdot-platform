from __future__ import annotations

from pathlib import Path

import pytest
from chatcore_release_control.host_service import (
    CommandResult,
    FixedOfflineImageBuilder,
    GitExactCommitSource,
    GitSourceConfig,
    HostServiceError,
)


def test_host_fetch_obtains_a_fresh_renewable_source_token_for_each_operation(tmp_path):
    tokens, calls = [], []

    def token():
        value = f"synthetic-source-token-{len(tokens) + 1}"
        tokens.append(value)
        return value

    class Runner:
        def run(self, argv, **kwargs):
            calls.append(tuple(argv))
            assert not any(value in str(argv) + str(kwargs["env"]) for value in tokens)
            if "init" in argv:
                directory = Path(argv[-1])
                directory.mkdir(parents=True)
                (directory / "objects").mkdir()
                (directory / "HEAD").write_text("ref: refs/heads/main\n")
            return CommandResult(0, b"")

    source = GitExactCommitSource(
        GitSourceConfig(
            repository_url="https://github.com/acme/plugin.git",
            git_path="/usr/bin/git",
            credential_file=None,
            token_provider=token,
            scratch_directory=tmp_path / "scratch",
        ),
        Runner(),
    )
    for _ in range(2):
        with source.fetch("a" * 40, "b" * 40) as repository:
            assert repository.is_dir()
    assert len(tokens) == 2 and len([call for call in calls if "fetch" in call]) == 2
    assert "synthetic-source-token" not in repr(source)


def test_host_source_issuer_failure_is_safe_and_never_invokes_git(tmp_path):
    def token():
        raise RuntimeError("synthetic-secret-must-not-escape")

    class Runner:
        def run(self, *args, **kwargs):
            pytest.fail("failed source credentials cannot invoke Git")

    source = GitExactCommitSource(
        GitSourceConfig(
            repository_url="https://github.com/acme/plugin.git",
            git_path="/usr/bin/git",
            credential_file=None,
            token_provider=token,
            scratch_directory=tmp_path / "scratch",
        ),
        Runner(),
    )
    with pytest.raises(HostServiceError) as error:
        with source.fetch("a" * 40, "b" * 40):
            pytest.fail("source must not be returned")
    assert str(error.value) == "build_denied" and error.value.__cause__ is None


def test_source_app_private_key_cannot_be_readable_by_other_users(tmp_path):
    key = tmp_path / "synthetic-source-app.pem"
    key.write_bytes(b"synthetic-private-key")
    for mode in (0o644, 0o640, 0o604):
        key.chmod(mode)
        with pytest.raises(HostServiceError):
            FixedOfflineImageBuilder._read_installed(key, 32 * 1024, private=True)
    key.chmod(0o600)
    assert FixedOfflineImageBuilder._read_installed(key, 32 * 1024, private=True) == (
        b"synthetic-private-key"
    )
    link = tmp_path / "linked-key.pem"
    link.symlink_to(key)
    with pytest.raises(HostServiceError):
        FixedOfflineImageBuilder._read_installed(link, 32 * 1024, private=True)
