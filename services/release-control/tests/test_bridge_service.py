from __future__ import annotations

import hashlib
import os
import stat
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from chatcore_release_control.bridge_service import (
    ServiceError,
    build_service,
    load_service_config,
    main,
)
from chatcore_release_control.telegram_bridge import BridgeError


def _write_config(tmp_path: Path) -> Path:
    files = {
        "bot-token": b"12345:synthetic-token-value\n",
        "ledger-dsn": b"postgresql://synthetic\n",
        "relay-key": b"r" * 32,
        "webhook-secret": b"w" * 32,
        "broker-credential": b"b" * 32,
        "client-credentials.toml": (
            b'[credentials]\nbot = "'
            + hashlib.sha256(b"client-secret").hexdigest().encode()
            + b'"\n'
        ),
    }
    for name, value in files.items():
        path = tmp_path / name
        path.write_bytes(value)
        path.chmod(0o600)
    config = tmp_path / "bridge.toml"
    config.write_text(
        f'''version = 1
socket_path = "{tmp_path}/bridge.sock"
ownership_store_path = "{tmp_path}/ownership.json"
telegram_owner_id = 7
ledger_dsn_file = "{tmp_path}/ledger-dsn"
telegram_token_file = "{tmp_path}/bot-token"
relay_key_file = "{tmp_path}/relay-key"
webhook_secret_file = "{tmp_path}/webhook-secret"
broker_credential_file = "{tmp_path}/broker-credential"
client_credentials_file = "{tmp_path}/client-credentials.toml"
menu_principal = "bot"
principal_chat_ids = {{ bot = ["7", "-100"] }}
global_event_principals = ["bot"]
reserved_commands = {{ private = [["release", "Review release status"]], groups = [] }}
'''
    )
    config.chmod(0o600)
    return config


def test_strict_config_reads_synthetic_private_secret_files(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)
    config = load_service_config(config_path, expected_uid=os.getuid())
    assert config.telegram_owner_id == 7
    assert config.allowed_chats == {"bot": frozenset({"7", "-100"})}
    assert config.credential_hashes == {"bot": hashlib.sha256(b"client-secret").hexdigest()}
    assert "synthetic-token-value" not in repr(config)


def test_config_rejects_unknown_fields_and_insecure_secret_modes(tmp_path: Path) -> None:
    config = _write_config(tmp_path)
    config.write_text(config.read_text() + "unexpected = true\n")
    with pytest.raises(ServiceError):
        load_service_config(config, expected_uid=os.getuid())
    config = _write_config(tmp_path)
    (tmp_path / "bot-token").chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP)
    with pytest.raises(ServiceError):
        load_service_config(config, expected_uid=os.getuid())


def test_cli_help_does_not_open_config_or_initialize_service(capsys) -> None:
    with pytest.raises(SystemExit) as result:
        main(["--help"])
    assert result.value.code == 0
    assert "--check-config" in capsys.readouterr().out


def test_cli_check_config_validates_only_local_synthetic_files(tmp_path: Path, capsys) -> None:
    config = _write_config(tmp_path)
    result = main(
        ["--check-config", "--config", str(config)],
        expected_uid=os.getuid(),
    )
    assert result == 0
    assert capsys.readouterr().out.strip() == "configuration valid"


class _Pool:
    def __init__(self, rows=None, fail=False) -> None:
        self.rows = rows or []
        self.fail = fail
        self.queried = False
        self.closed = False
        self.runtime_checked = False

    @asynccontextmanager
    async def acquire(self):
        yield _RuntimeConnection()

    async def fetchval(self, query):
        assert "bridge_broker_send" in query or "release_card_view" in query
        return True

    async def fetch(self, query: str):
        self.queried = True
        assert "release_card" in query
        if self.fail:
            raise RuntimeError("synthetic database diagnostics")
        return self.rows

    async def fetchrow(self, query: str, *args):
        if "release_card_view" in query:
            return getattr(self, "view_row", None)
        raise AssertionError("unexpected row query")

    async def close(self):
        self.closed = True


class _RuntimeConnection:
    async def fetchval(self, query):
        assert "broker_release_revocation" in query
        return True

    async def fetchrow(self, query, expected_group):
        assert "current_setting('fsync')" in query
        assert expected_group == "release_control_bridge"
        return {
            "fsync": "on",
            "full_page_writes": "on",
            "synchronous_commit": "on",
            "session_user": "bridge_login",
            "current_user": "bridge_login",
            "login": True,
            "superuser": False,
            "createdb": False,
            "createrole": False,
            "bypassrls": False,
            "inherit": True,
            "expected_group_exists": True,
            "expected_group_login": False,
            "expected_group_safe": True,
            "expected_membership": True,
            "other_group_membership": False,
            "maintenance_membership": False,
            "control_schema_exists": True,
            "owner_membership": False,
        }


class _Bot:
    def __init__(self) -> None:
        self.calls = []
        self.closed = False

    async def call(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return {"message_id": len(self.calls) + 100, "chat": {"id": 7}}

    async def download_file(self, *args, **kwargs):
        return b""

    async def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_runtime_loads_full_ledger_protection_before_returning_ready_service(
    tmp_path: Path,
) -> None:
    config_path = _write_config(tmp_path)
    pool = _Pool([{"chat_id": 7, "message_id": 22}])
    bot = _Bot()

    async def factory(*args, **kwargs):
        assert args == ("postgresql://synthetic",)
        assert "init" in kwargs
        await kwargs["init"](_RuntimeConnection())
        return pool

    service = await build_service(
        config_path,
        expected_uid=os.getuid(),
        pool_factory=factory,
        bot_transport=bot,
    )
    assert pool.queried
    assert service.socket_path == str(tmp_path / "bridge.sock")
    assert type(service.app._ingress._revocations).__name__ == "PostgresReleaseRevocationStore"
    with pytest.raises(BridgeError) as error:
        await service.bridge.handle(
            "client-secret",
            {
                "operation": "delete_message",
                "payload": {"chat_id": "7", "message_id": "22"},
            },
        )
    assert error.value.code == "forbidden"
    assert bot.calls == []
    await service.close()
    assert pool.closed and bot.closed


@pytest.mark.asyncio
async def test_service_details_and_diff_handlers_send_protected_reports_and_document(tmp_path):
    from types import SimpleNamespace
    from uuid import uuid4

    config_path = _write_config(tmp_path)
    pool, bot = _Pool([]), _Bot()

    async def factory(*args, **kwargs):
        await kwargs["init"](_RuntimeConnection())
        return pool

    service = await build_service(
        config_path, expected_uid=os.getuid(), pool_factory=factory, bot_transport=bot
    )
    callback = service.app._ingress._card_views
    binding = {
        "pr_number": 42,
        "target": "prod",
        "base_sha": "b" * 40,
        "head_sha": "h" * 40,
        "head_tree": "t" * 40,
        "policy_digest": "p" * 64,
        "deployed_identity": {
            "git_sha": "d" * 40,
            "image_digest": "sha256:" + "e" * 64,
            "config_generation": 1,
            "config_digest": "c" * 64,
            "target_sequence": 2,
        },
        "review_bundle": {
            "deployed_tree": "d" * 40,
            "base_tree": "b" * 40,
            "head_tree": "t" * 40,
            "check_evidence_digest": "x" * 64,
            "deployed_to_base": [],
            "base_to_head": [],
            "deployed_to_head": [],
        },
    }
    card = SimpleNamespace(owner_user_id=7, approval_id=uuid4(), binding=binding)
    await callback("details", card)
    assert bot.calls and all(call[0][0] == "sendMessage" for call in bot.calls)
    assert "Trusted check evidence digest" in "\n".join(call[0][1]["text"] for call in bot.calls)
    bot.calls.clear()
    pool.view_row = {
        "presentation": {"pr_url": "https://github.com/acme/repo/pull/42", "redacted_files": 1},
        "patch": b"already-redacted",
        "patch_state": "included",
    }
    await callback("diff", card)
    args, kwargs = bot.calls[0]
    assert args[0] == "sendDocument" and args[1]["document"] == b"already-redacted"
    assert args[1]["filename"] == "pr-42-hhhhhhh.patch"
    assert args[1]["caption"].endswith("(1 file(s) redacted by the secret scanner)")
    bot.calls.clear()
    pool.view_row = {
        "presentation": {"pr_url": "https://github.com/acme/repo/pull/42"},
        "patch": None,
        "patch_state": "too_large",
    }
    await callback("diff", card)
    assert "Diff too large" in bot.calls[0][0][1]["text"]
    bot.calls.clear()
    pool.view_row = None
    await callback("diff", card)
    assert "Diff not available" in bot.calls[0][0][1]["text"]
    assert "See PR #42 on GitHub." in bot.calls[0][0][1]["text"]
    await service.close()


@pytest.mark.asyncio
async def test_runtime_database_reconstruction_failure_never_returns_ready_service(
    tmp_path: Path,
) -> None:
    config_path = _write_config(tmp_path)
    pool = _Pool(fail=True)
    bot = _Bot()

    async def factory(*args, **kwargs):
        return pool

    with pytest.raises(ServiceError) as error:
        await build_service(
            config_path,
            expected_uid=os.getuid(),
            pool_factory=factory,
            bot_transport=bot,
        )
    assert error.value.code == "service_unavailable"
    assert "synthetic database diagnostics" not in str(error.value)
    assert pool.closed and not bot.closed
