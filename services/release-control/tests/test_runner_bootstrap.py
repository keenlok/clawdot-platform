from __future__ import annotations

import hashlib
import io
import os
import termios
from dataclasses import replace
from pathlib import Path

import chatcore_release_control.runner_bootstrap as bootstrap_module
import pytest
from chatcore_release_control.runner_bootstrap import (
    BootstrapError,
    RunnerImageManifest,
    RunnerProcessResult,
    compute_runner_closure_digest,
    configure_runner_with_prompt,
    read_registration_token,
    run_runner_job,
    send_registration_token,
    verify_runner_files,
)
from chatcore_release_control.runner_supervisor import (
    SupervisorError,
    render_runner_image_recipe,
)


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def manifest_for(tmp_path):
    runner_root = tmp_path / "runner-image"
    runner_root.mkdir(parents=True)
    config = runner_root / "config.sh"
    config.write_bytes(b"synthetic pinned config wrapper")
    config.chmod(0o555)
    run_script = runner_root / "run.sh"
    run_script.write_bytes(b"synthetic pinned run wrapper")
    run_script.chmod(0o555)
    listener = runner_root / "bin/Runner.Listener"
    listener.parent.mkdir()
    listener.write_bytes(b"synthetic pinned runner listener")
    listener.chmod(0o555)
    helper = runner_root / "bin/Runner.Worker"
    helper.write_bytes(b"synthetic runner worker file")
    helper.chmod(0o444)
    file_hashes = {
        "config.sh": digest(config.read_bytes()),
        "run.sh": digest(run_script.read_bytes()),
        "bin/Runner.Listener": digest(listener.read_bytes()),
        "bin/Runner.Worker": digest(helper.read_bytes()),
    }
    bootstrap_path = Path(__file__).parents[1] / "src/chatcore_release_control/runner_bootstrap.py"
    bootstrap_digest = digest(bootstrap_path.read_bytes())
    relay_helper = Path(__file__).parents[1] / "src/chatcore_release_control/runner_relay.py"
    relay_helper_digest = digest(relay_helper.read_bytes())
    fields = {
        "schema_version": 1,
        "base_image": "python:3.12-slim@sha256:" + "a" * 64,
        "runner_archive_sha256": "b" * 64,
        "bootstrap_sha256": bootstrap_digest,
        "relay_helper_sha256": relay_helper_digest,
        "runner_root": str(runner_root),
        "runner_binary": "config.sh",
        "runner_run_script": "run.sh",
        "runner_version": "0.0.0",
        "runner_files": file_hashes,
        "registration_protocol": "actions-runner-config-prompt-v1",
    }
    fields["runner_closure_sha256"] = compute_runner_closure_digest(fields)
    return RunnerImageManifest.from_mapping(fields), runner_root, fields


def test_manifest_pins_image_closure_scripts_version_and_prompt_protocol(tmp_path):
    manifest, root, fields = manifest_for(tmp_path)

    assert manifest.base_image.endswith("sha256:" + "a" * 64)
    assert manifest.runner_binary == "config.sh"
    assert manifest.runner_run_script == "run.sh"
    assert verify_runner_files(root, manifest, expected_uid=os.getuid())
    with pytest.raises(BootstrapError):
        RunnerImageManifest.from_mapping({**fields, "runner_closure_sha256": "0" * 64})
    with pytest.raises(BootstrapError):
        RunnerImageManifest.from_mapping({**fields, "registration_protocol": "custom-stdin"})
    with pytest.raises(BootstrapError):
        RunnerImageManifest.from_mapping({**fields, "unreviewed": True})


def test_manifest_rejects_tampered_unlisted_and_symlinked_runner_files(tmp_path):
    manifest, root, _ = manifest_for(tmp_path)
    (root / "bin/Runner.Worker").chmod(0o644)
    (root / "bin/Runner.Worker").write_bytes(b"tampered")
    with pytest.raises(BootstrapError):
        verify_runner_files(root, manifest)

    manifest, root, _ = manifest_for(tmp_path / "second")
    (root / "unlisted").write_text("extra")
    with pytest.raises(BootstrapError):
        verify_runner_files(root, manifest)

    manifest, root, _ = manifest_for(tmp_path / "third")
    (root / "bin/Runner.Worker").unlink()
    (root / "bin/Runner.Worker").symlink_to(root / "bin/Runner.Listener")
    with pytest.raises(BootstrapError):
        verify_runner_files(root, manifest)


def test_registration_token_reader_is_bounded_and_never_logs_value():
    assert read_registration_token(io.BytesIO(b"short-lived-token")) == "short-lived-token"
    for value in (b"", b"x" * 4097, b"token\nvalue", b"\xff"):
        with pytest.raises(BootstrapError):
            read_registration_token(io.BytesIO(value))


class FakeSocket:
    def __init__(self):
        self.connected_to = None
        self.payload = None
        self.closed = False

    def connect(self, path):
        self.connected_to = path

    def settimeout(self, timeout):
        self.timeout = timeout

    def sendall(self, payload):
        self.payload = payload

    def shutdown(self, how):
        self.shutdown_how = how

    def close(self):
        self.closed = True


def test_registration_receiver_uses_only_expected_closure_and_private_socket(tmp_path):
    manifest, _, _ = manifest_for(tmp_path)
    fake = FakeSocket()
    send_registration_token(
        "short-lived-token",
        expected_closure=manifest.runner_closure_sha256,
        manifest=manifest,
        socket_path="/tmp/p09-registration.sock",
        socket_factory=lambda *_: fake,
    )
    assert fake.connected_to == "/tmp/p09-registration.sock"
    assert fake.payload == b"short-lived-token"
    assert fake.closed

    with pytest.raises(BootstrapError):
        send_registration_token(
            "short-lived-token",
            expected_closure="0" * 64,
            manifest=manifest,
            socket_path="/tmp/p09-registration.sock",
            socket_factory=lambda *_: fake,
        )


class FakePromptSession:
    def __init__(self, chunks, *, returncode=0, after_write=()):
        self.chunks = list(chunks)
        self.after_write = list(after_write)
        self.returncode = returncode
        self.writes = []
        self.closed = False
        self.killed = False

    def read(self, _timeout):
        return self.chunks.pop(0) if self.chunks else b""

    def write(self, payload):
        self.writes.append(bytes(payload))
        self.chunks.extend(self.after_write)

    def poll(self):
        return self.returncode if not self.chunks else None

    def wait(self, _timeout):
        return self.returncode

    def kill(self):
        self.killed = True

    def close(self):
        self.closed = True


def test_concrete_prompt_session_disables_terminal_echo_without_starting_a_process(monkeypatch):
    captured = {}

    class FakeProcess:
        pid = os.getpid()

        def poll(self):
            return 0

    def fake_popen(argv, **kwargs):
        captured["argv"] = argv
        captured.update(kwargs)
        captured["termios_flags"] = termios.tcgetattr(kwargs["stdin"])[3]
        return FakeProcess()

    monkeypatch.setattr(bootstrap_module.subprocess, "Popen", fake_popen)
    session = bootstrap_module._SubprocessPTYSession(
        ["/tmp/runner/config.sh", "--ephemeral"],
        cwd="/tmp/runner",
        env={"PATH": "/usr/bin:/bin"},
    )
    try:
        flags = captured["termios_flags"]
        assert not flags & (termios.ECHO | termios.ECHONL)
        assert captured["stdin"] == captured["stdout"] == captured["stderr"]
        assert captured["shell"] is False
        assert captured["close_fds"] is True
        assert captured["argv"] == ["/tmp/runner/config.sh", "--ephemeral"]
    finally:
        session.close()


def test_runner_job_configures_standard_runner_from_exact_prompt_and_cleans_tmpfs(tmp_path):
    manifest, root, _ = manifest_for(tmp_path)
    calls = []
    sessions = []

    def make_session(argv, *, cwd, env):
        session = FakePromptSession(
            [
                b"Runner vers",
                b"ion: '0.0.0'\nEnter the runner registration ",
                b"token:\r\n",
                b"configuration complete\r\n",
            ]
        )
        sessions.append((session, argv, cwd, env))
        return session

    def execute(argv, *, cwd, env, timeout, input_data=None):
        calls.append((argv, cwd, env, timeout, input_data))
        return RunnerProcessResult(0)

    result = run_runner_job(
        manifest,
        expected_closure=manifest.runner_closure_sha256,
        source_root=root,
        bootstrap_path=Path(__file__).parents[1]
        / "src/chatcore_release_control/runner_bootstrap.py",
        scratch_parent=tmp_path,
        runner_name="runner-" + "1" * 32,
        repository_url="https://github.com/owner/repo",
        registration_token="short-lived-token",
        execute=execute,
        prompt_session_factory=make_session,
        timeout_seconds=300,
        expected_owner_uid=os.getuid(),
    )

    assert result == 0
    assert len(calls) == 1
    launch = calls[0]
    assert launch[0] == [str(Path(launch[1]) / "run.sh"), "--once"]
    assert launch[4] is None
    session, argv, cwd, env = sessions[0]
    assert argv == [
        str(Path(cwd) / "config.sh"),
        "--url",
        "https://github.com/owner/repo",
        "--name",
        "runner-" + "1" * 32,
        "--no-default-labels",
        "--labels",
        "chatcore-p09-ephemeral",
        "--work",
        str(Path(cwd) / "_work"),
        "--ephemeral",
        "--disableupdate",
    ]
    assert "--token" not in argv and "short-lived-token" not in " ".join(argv)
    assert "short-lived-token" not in repr(env)
    assert session.writes == [b"short-lived-token\n"]
    assert session.closed and not session.killed
    assert cwd == launch[1]
    assert not Path(cwd).exists()


@pytest.mark.parametrize(
    "chunks",
    [
        [b"Enter a different prompt?"],
        [b"Enter the runner registration token:\n"],
        [b"Runner version: '0.0.0'\nEnter the runner registration token:\nEnter again:"],
        [b"Runner version: '0.0.0'\nEnter the runner registration token:\nshort-lived-token\n"],
        [b"Runner version: '9.999.9'\nEnter the runner registration token:\n"],
    ],
)
def test_prompt_wrapper_rejects_unknown_prompt_repetition_or_token_echo(chunks):
    session = FakePromptSession(chunks)
    with pytest.raises(BootstrapError) as caught:
        configure_runner_with_prompt(
            prompt_argv(),
            cwd="/tmp/runner",
            env=prompt_env(),
            timeout_seconds=5,
            registration_token="short-lived-token",
            expected_runner_version="0.0.0",
            session_factory=lambda *_args, **_kwargs: session,
        )
    assert session.closed
    assert session.killed
    if chunks in (
        [b"Enter a different prompt?"],
        [b"Enter the runner registration token:\n"],
    ):
        assert session.writes == []
    assert "short-lived-token" not in str(caught.value)


def test_prompt_wrapper_rejects_registration_token_echoed_by_runner():
    session = FakePromptSession(
        [b"Runner version: '0.0.0'\nEnter the runner registration token:\n"],
        after_write=[b"short-lived-token\n"],
    )
    with pytest.raises(BootstrapError) as caught:
        configure_runner_with_prompt(
            prompt_argv(),
            cwd="/tmp/runner",
            env=prompt_env(),
            timeout_seconds=5,
            registration_token="short-lived-token",
            expected_runner_version="0.0.0",
            session_factory=lambda *_args, **_kwargs: session,
        )
    assert session.writes == [b"short-lived-token\n"]
    assert session.killed and session.closed
    assert "short-lived-token" not in str(caught.value)


def prompt_argv():
    return [
        "/tmp/runner/config.sh",
        "--url",
        "https://github.com/owner/repo",
        "--name",
        "runner-" + "a" * 32,
        "--no-default-labels",
        "--labels",
        "chatcore-p09-ephemeral",
        "--work",
        "/tmp/runner/_work",
        "--ephemeral",
        "--disableupdate",
    ]


def prompt_env():
    return {
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "HOME": "/tmp/runner/home",
        "RUNNER_TEMP": "/tmp/runner/temp",
    }


def test_prompt_wrapper_rejects_noncanonical_runner_command_and_environment():
    for argv, env in (
        (prompt_argv() + ["--token", "accidental"], prompt_env()),
        (prompt_argv(), {**prompt_env(), "EXTRA_SECRET": "ambient"}),
    ):
        with pytest.raises(BootstrapError):
            configure_runner_with_prompt(
                argv,
                cwd="/tmp/runner",
                env=env,
                timeout_seconds=5,
                registration_token="short-lived-token",
                expected_runner_version="0.0.0",
                session_factory=lambda *_args, **_kwargs: pytest.fail(
                    "invalid command must be rejected before process creation"
                ),
            )


def test_runner_job_fails_before_run_on_configure_failure_and_cleans_tmpfs(tmp_path):
    manifest, root, _ = manifest_for(tmp_path)
    calls = []

    sessions = []

    def make_session(_argv, *, cwd, env):
        session = FakePromptSession(
            [b"Runner version: '0.0.0'\nEnter the runner registration token:\n"],
            returncode=1,
        )
        sessions.append(session)
        return session

    def execute(argv, *, cwd, env, timeout, input_data=None):
        calls.append(argv)
        return RunnerProcessResult(1, b"secret", b"token")

    with pytest.raises(BootstrapError):
        run_runner_job(
            manifest,
            expected_closure=manifest.runner_closure_sha256,
            source_root=root,
            bootstrap_path=Path(__file__).parents[1]
            / "src/chatcore_release_control/runner_bootstrap.py",
            scratch_parent=tmp_path,
            runner_name="runner-" + "2" * 32,
            repository_url="https://github.com/owner/repo",
            registration_token="short-lived-token",
            execute=execute,
            prompt_session_factory=make_session,
            timeout_seconds=300,
            expected_owner_uid=os.getuid(),
        )
    assert calls == []
    assert sessions[0].writes == [b"short-lived-token\n"]


@pytest.mark.parametrize(
    "runner_name,repository_url",
    [
        ("../../host", "https://github.com/owner/repo"),
        ("runner-" + "3" * 32, "https://evil.invalid/owner/repo"),
    ],
)
def test_runner_job_rejects_untrusted_identity_and_repository(
    runner_name, repository_url, tmp_path
):
    manifest, root, _ = manifest_for(tmp_path)
    with pytest.raises(BootstrapError):
        run_runner_job(
            manifest,
            expected_closure=manifest.runner_closure_sha256,
            source_root=root,
            bootstrap_path=Path(__file__).parents[1]
            / "src/chatcore_release_control/runner_bootstrap.py",
            scratch_parent=tmp_path,
            runner_name=runner_name,
            repository_url=repository_url,
            registration_token="short-lived-token",
            execute=lambda *args, **kwargs: pytest.fail("runner must not launch"),
            timeout_seconds=300,
            expected_owner_uid=os.getuid(),
        )


def test_image_recipe_requires_pinned_inputs_and_verifies_full_file_closure(tmp_path):
    manifest, _, _ = manifest_for(tmp_path)
    image_manifest = replace(manifest, runner_root="/opt/p09/runner-files")

    recipe = render_runner_image_recipe(image_manifest, python_binary="/usr/bin/python3")

    assert f"FROM {image_manifest.base_image}" in recipe
    assert "COPY runner-files/ /opt/p09/runner-files/" in recipe
    assert "COPY runner_relay.py /opt/p09/runner_relay.py" in recipe
    assert "verify-image" in recipe
    assert "io.chatcore.runner-closure-sha256=" + image_manifest.runner_closure_sha256 in recipe
    assert "io.chatcore.runner-version=" + image_manifest.runner_version in recipe
    assert "RUN apt" not in recipe
    with pytest.raises(SupervisorError):
        render_runner_image_recipe(image_manifest, python_binary="/bin/sh")


def test_image_recipe_handles_manifest_file_limit_without_expanding_checksum_args(tmp_path):
    manifest, _, fields = manifest_for(tmp_path)
    runner_files = dict(manifest.runner_files)
    runner_files.update({f"lib/Runner-{index:04d}.dll": "a" * 64 for index in range(300)})
    closure_fields = {
        "base_image": manifest.base_image,
        "runner_archive_sha256": manifest.runner_archive_sha256,
        "bootstrap_sha256": manifest.bootstrap_sha256,
        "relay_helper_sha256": manifest.relay_helper_sha256,
        "runner_binary": manifest.runner_binary,
        "runner_run_script": manifest.runner_run_script,
        "runner_version": manifest.runner_version,
        "runner_files": runner_files,
        "registration_protocol": manifest.registration_protocol,
    }
    large = RunnerImageManifest.from_mapping(
        {
            **fields,
            "runner_root": "/opt/p09/runner-files",
            "runner_files": runner_files,
            "runner_closure_sha256": compute_runner_closure_digest(closure_fields),
        }
    )

    recipe = render_runner_image_recipe(large, python_binary="/usr/bin/python3")

    assert "verify-image" in recipe
    assert "sha256sum -c -" not in recipe
    assert len(recipe) < 4096
