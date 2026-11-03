"""Strict local configuration and Unix-socket service entrypoint for P06."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import os
import re
import socket
import stat
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import asyncpg
import uvicorn

from .bot_api_http import BotApiHTTPTransport
from .bridge_http import TelegramBridgeASGI
from .database_runtime import connection_initializer, verify_database_pool
from .durable_relay import DurableRelay
from .ledger import ReleaseLedger
from .protected_bridge_http import BridgeSendStore, ProtectedBridgeApplication, report_observation
from .release_card_view import ReleaseCardViewStore, render_details
from .revocation import PostgresReleaseRevocationStore
from .telegram_bridge import MessageOwnershipStore, TelegramBridge
from .telegram_cards import ReleaseCardIssuer
from .telegram_ingress import TelegramIngress

_HASH = re.compile(r"^[0-9a-f]{64}$")
_CHAT = re.compile(r"^-?[0-9]{1,20}$")
_COMMAND = re.compile(r"^[a-z0-9_]{1,32}$")
_PRINCIPAL = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_CONFIG_KEYS = frozenset(
    {
        "version",
        "socket_path",
        "ownership_store_path",
        "telegram_owner_id",
        "ledger_dsn_file",
        "telegram_token_file",
        "relay_key_file",
        "webhook_secret_file",
        "broker_credential_file",
        "client_credentials_file",
        "menu_principal",
        "principal_chat_ids",
        "global_event_principals",
        "reserved_commands",
    }
)


class ServiceError(RuntimeError):
    def __init__(self, code: str = "invalid_configuration") -> None:
        allowed = {"invalid_configuration", "service_unavailable"}
        self.code = code if code in allowed else "invalid_configuration"
        super().__init__(self.code)


@dataclass(frozen=True, repr=False)
class ServiceConfig:
    socket_path: str
    ownership_store_path: str
    telegram_owner_id: int
    ledger_dsn: str = field(repr=False)
    telegram_token: str = field(repr=False)
    relay_key: bytes = field(repr=False)
    webhook_secret: str = field(repr=False)
    broker_credential: str = field(repr=False)
    credential_hashes: dict[str, str] = field(repr=False)
    allowed_chats: dict[str, frozenset[str]]
    global_event_principals: frozenset[str]
    menu_principal: str
    reserved_commands: dict[str, list[list[str]]]

    def __repr__(self) -> str:
        return "ServiceConfig(secrets=[REDACTED])"


@dataclass
class ProtectedBridgeService:
    app: ProtectedBridgeApplication
    bridge: TelegramBridge
    cards: ReleaseCardIssuer
    socket_path: str
    pool: Any = field(repr=False)
    bot_transport: BotApiHTTPTransport = field(repr=False)

    async def close(self) -> None:
        await self.bot_transport.close()
        await self.pool.close()


def _secure_read(path_value: object, expected_uid: int) -> bytes:
    if not isinstance(path_value, str) or not Path(path_value).is_absolute():
        raise ServiceError()
    path = Path(path_value)
    try:
        parent = path.parent.lstat()
        if (
            not stat.S_ISDIR(parent.st_mode)
            or parent.st_uid != expected_uid
            or stat.S_IMODE(parent.st_mode) & 0o022
        ):
            raise ServiceError()
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != expected_uid
                or stat.S_IMODE(info.st_mode) & 0o077
                or not 1 <= info.st_size <= 1024 * 1024
            ):
                raise ServiceError()
            raw = bytearray()
            while chunk := os.read(descriptor, min(65536, 1024 * 1024 - len(raw) + 1)):
                raw.extend(chunk)
                if len(raw) > 1024 * 1024:
                    raise ServiceError()
        finally:
            os.close(descriptor)
    except ServiceError:
        raise
    except OSError:
        raise ServiceError() from None
    if not raw or len(raw) > 1024 * 1024:
        raise ServiceError()
    return bytes(raw)


def load_service_config(path: str | os.PathLike[str], *, expected_uid: int = 0) -> ServiceConfig:
    try:
        raw = _secure_read(os.fspath(path), expected_uid)
        value = tomllib.loads(raw.decode("utf-8"))
        if (
            not isinstance(value, dict)
            or set(value) != _CONFIG_KEYS
            or type(value["version"]) is not int
            or value["version"] != 1
        ):
            raise ServiceError()
        socket_path = value["socket_path"]
        ownership_path = value["ownership_store_path"]
        if (
            not isinstance(socket_path, str)
            or not Path(socket_path).is_absolute()
            or not isinstance(ownership_path, str)
            or not Path(ownership_path).is_absolute()
            or type(value["telegram_owner_id"]) is not int
            or value["telegram_owner_id"] <= 0
        ):
            raise ServiceError()
        _secure_parent(ownership_path, expected_uid)
        token = _secure_read(value["telegram_token_file"], expected_uid).decode("ascii").strip()
        dsn = _secure_read(value["ledger_dsn_file"], expected_uid).decode("utf-8").strip()
        relay_key = _secure_read(value["relay_key_file"], expected_uid)
        webhook_secret = (
            _secure_read(value["webhook_secret_file"], expected_uid).decode("ascii").strip()
        )
        broker_credential = (
            _secure_read(value["broker_credential_file"], expected_uid).decode("ascii").strip()
        )
        if (
            not token
            or any(char in token for char in "\x00\r\n/?#")
            or not dsn
            or len(relay_key) < 32
            or re.fullmatch(r"[A-Za-z0-9_-]{32,256}", webhook_secret) is None
            or re.fullmatch(r"[A-Za-z0-9_.-]{32,4096}", broker_credential) is None
            or len({token.encode(), relay_key, webhook_secret.encode(), broker_credential.encode()})
            != 4
        ):
            raise ServiceError()
        credentials_doc = tomllib.loads(
            _secure_read(value["client_credentials_file"], expected_uid).decode("utf-8")
        )
        if set(credentials_doc) != {"credentials"} or not isinstance(
            credentials_doc["credentials"], dict
        ):
            raise ServiceError()
        credentials = credentials_doc["credentials"]
        if (
            not credentials
            or len(set(credentials.values())) != len(credentials)
            or any(
                not isinstance(key, str)
                or not _PRINCIPAL.fullmatch(key)
                or not isinstance(item, str)
                or not _HASH.fullmatch(item)
                for key, item in credentials.items()
            )
        ):
            raise ServiceError()
        if hashlib.sha256(broker_credential.encode()).hexdigest() in credentials.values():
            raise ServiceError()
        raw_chats = value["principal_chat_ids"]
        if not isinstance(raw_chats, dict) or set(raw_chats) != set(credentials):
            raise ServiceError()
        allowed_chats: dict[str, frozenset[str]] = {}
        for principal, chats in raw_chats.items():
            if (
                not isinstance(chats, list)
                or not chats
                or any(not isinstance(chat, str) or not _CHAT.fullmatch(chat) for chat in chats)
            ):
                raise ServiceError()
            allowed_chats[principal] = frozenset(chats)
        menu = value["menu_principal"]
        global_principals = value["global_event_principals"]
        if (
            not isinstance(menu, str)
            or menu not in credentials
            or not isinstance(global_principals, list)
            or any(item not in credentials for item in global_principals)
        ):
            raise ServiceError()
        raw_reserved = value["reserved_commands"]
        if not isinstance(raw_reserved, dict) or set(raw_reserved) != {"private", "groups"}:
            raise ServiceError()
        reserved: dict[str, list[list[str]]] = {}
        for scope in ("private", "groups"):
            rows = raw_reserved[scope]
            if not isinstance(rows, list) or len(rows) > 100:
                raise ServiceError()
            normalized: list[list[str]] = []
            for row in rows:
                if (
                    not isinstance(row, list)
                    or len(row) != 2
                    or not isinstance(row[0], str)
                    or not _COMMAND.fullmatch(row[0])
                    or not isinstance(row[1], str)
                    or not 1 <= len(row[1]) <= 256
                ):
                    raise ServiceError()
                normalized.append([row[0], row[1]])
            reserved[scope] = normalized
        return ServiceConfig(
            socket_path=socket_path,
            ownership_store_path=ownership_path,
            telegram_owner_id=value["telegram_owner_id"],
            ledger_dsn=dsn,
            telegram_token=token,
            relay_key=relay_key,
            webhook_secret=webhook_secret,
            broker_credential=broker_credential,
            credential_hashes=dict(credentials),
            allowed_chats=allowed_chats,
            global_event_principals=frozenset(global_principals),
            menu_principal=menu,
            reserved_commands=reserved,
        )
    except ServiceError:
        raise
    except Exception:
        raise ServiceError() from None


def _secure_parent(path_value: str, expected_uid: int) -> None:
    try:
        parent = Path(path_value).parent
        info = parent.lstat()
        if (
            not Path(path_value).is_absolute()
            or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != expected_uid
            or stat.S_IMODE(info.st_mode) & 0o022
        ):
            raise ServiceError()
    except ServiceError:
        raise
    except OSError:
        raise ServiceError() from None


def _authenticator(config: ServiceConfig):
    def authenticate(credential: str) -> str | None:
        if not isinstance(credential, str) or not credential or len(credential) > 4096:
            return None
        candidate = hashlib.sha256(credential.encode("utf-8")).hexdigest()
        matched = None
        for principal, digest in config.credential_hashes.items():
            if hmac.compare_digest(candidate, digest):
                matched = principal
        return matched

    return authenticate


async def build_service(
    config_path: str | os.PathLike[str],
    *,
    expected_uid: int = 0,
    pool_factory=asyncpg.create_pool,
    bot_transport: BotApiHTTPTransport | None = None,
) -> ProtectedBridgeService:
    config = load_service_config(config_path, expected_uid=expected_uid)
    pool = None
    transport = bot_transport if bot_transport is not None else BotApiHTTPTransport()
    try:
        pool = await pool_factory(
            config.ledger_dsn,
            min_size=1,
            max_size=8,
            timeout=5,
            command_timeout=10,
            init=connection_initializer("release_control_bridge"),
        )
        await verify_database_pool(pool, "release_control_bridge")
        # Complete ledger reconstruction happens before the service can be returned/served.
        protected = await pool.fetch(
            "SELECT chat_id, message_id FROM release_control.release_card "
            "ORDER BY chat_id, message_id"
        )
        if len(protected) > 100_000:
            raise ServiceError()
        ownership = MessageOwnershipStore(config.ownership_store_path)
        bridge = TelegramBridge(
            token=config.telegram_token,
            authenticate=_authenticator(config),
            allowed_chats=config.allowed_chats,
            transport=transport,
            ownership=ownership,
            load_protected_messages=lambda: tuple(
                (str(row["chat_id"]), str(row["message_id"])) for row in protected
            ),
            menu_principal=config.menu_principal,
            reserved_commands=config.reserved_commands,
        )
        app = TelegramBridgeASGI(
            bridge,
            relay_store=DurableRelay(pool),
            relay_key=config.relay_key,
            audiences=config.allowed_chats,
            global_event_principals=config.global_event_principals,
            allow_private_uds=True,
        )
        cards = ReleaseCardIssuer(
            ledger=ReleaseLedger(pool),
            transport=transport,
            token=config.telegram_token,
            current_telegram_owner=lambda: config.telegram_owner_id,
            protect_message=bridge.protect_release_message,
        )
        send_store = BridgeSendStore(pool)
        if not await send_store.ready():
            raise ServiceError("service_unavailable")
        revocations = PostgresReleaseRevocationStore(pool)
        if not await revocations.ready():
            raise ServiceError("service_unavailable")
        card_views = ReleaseCardViewStore(pool)
        if not await card_views.ready():
            raise ServiceError("service_unavailable")

        async def acknowledge(query_id: str) -> None:
            await transport.call(
                "answerCallbackQuery", {"callback_query_id": query_id}, token=config.telegram_token
            )

        async def send_report(owner: int, text: str) -> int:
            result = await transport.call(
                "sendMessage",
                {
                    "chat_id": owner,
                    "text": text,
                    "link_preview_options": {"is_disabled": True},
                },
                token=config.telegram_token,
            )
            if (
                not isinstance(result, dict)
                or type(result.get("message_id")) is not int
                or result["message_id"] <= 0
                or not isinstance(result.get("chat"), dict)
                or result["chat"].get("id") != owner
            ):
                raise ServiceError("service_unavailable")
            bridge.protect_release_message(str(owner), str(result["message_id"]))
            return result["message_id"]

        async def report_revocation(owner: int, approval_id: str, receipt_id: str) -> None:
            await send_report(
                owner,
                f"Release revocation recorded · approval {approval_id} · receipt {receipt_id}",
            )

        async def card_view(kind, card):
            owner = card.owner_user_id
            if kind == "details":
                for text in render_details(card.binding):
                    await send_report(owner, text)
                return
            row = await card_views.get(card.approval_id)
            binding = card.binding
            url = row.get("presentation", {}).get("pr_url") if row else None
            number = binding.get("pr_number")
            if row and row["patch_state"] == "included":
                result = await transport.call(
                    "sendDocument",
                    {
                        "chat_id": owner,
                        "document": row["patch"],
                        "filename": f"pr-{number}-{binding['head_sha'][:7]}.patch",
                        "caption": f"D → H diff for PR #{number}"
                        + (
                            f" ({row['presentation'].get('redacted_files', 0)} file(s) redacted "
                            "by the secret scanner)"
                            if row["presentation"].get("redacted_files", 0)
                            else ""
                        ),
                    },
                    token=config.telegram_token,
                )
                if (
                    not isinstance(result, dict)
                    or type(result.get("message_id")) is not int
                    or result["message_id"] <= 0
                    or not isinstance(result.get("chat"), dict)
                    or result["chat"].get("id") != owner
                ):
                    raise ServiceError("service_unavailable")
                bridge.protect_release_message(str(owner), str(result["message_id"]))
                return
            suffix = f"See the PR on GitHub: {url}" if url else f"See PR #{number} on GitHub."
            await send_report(
                owner,
                (
                    "Diff too large to attach. "
                    if row and row["patch_state"] == "too_large"
                    else "Diff not available. "
                )
                + suffix,
            )

        ingress = TelegramIngress(
            webhook_secret=config.webhook_secret,
            relay_key=config.relay_key,
            current_owner=lambda: config.telegram_owner_id,
            ledger=ReleaseLedger(pool),
            acknowledge=acknowledge,
            relay=app.accept_relay,
            revocations=revocations,
            report_revocation=report_revocation,
            card_views=card_view,
        )
        protected_app = ProtectedBridgeApplication(
            ordinary=app,
            ingress=ingress,
            cards=cards,
            ledger=ReleaseLedger(pool),
            send_store=send_store,
            broker_credential=config.broker_credential,
            current_owner=lambda: config.telegram_owner_id,
            send_report=send_report,
            allow_private_uds=True,
            report_summary=lambda card: report_observation(pool, card),
            card_views=card_views,
        )
        return ProtectedBridgeService(
            app=protected_app,
            bridge=bridge,
            cards=cards,
            socket_path=config.socket_path,
            pool=pool,
            bot_transport=transport,
        )
    except ServiceError:
        if pool is not None:
            await pool.close()
        if bot_transport is None:
            await transport.close()
        raise
    except Exception:
        if pool is not None:
            await pool.close()
        if bot_transport is None:
            await transport.close()
        raise ServiceError("service_unavailable") from None


def _check_socket_parent(socket_path: str) -> None:
    path = Path(socket_path)
    parent = path.parent
    try:
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o022:
            raise ServiceError()
        if path.exists() or path.is_symlink():
            raise ServiceError()
    except ServiceError:
        raise
    except OSError:
        raise ServiceError() from None


async def _serve(config_path: str) -> None:
    if os.geteuid() != 0:
        raise ServiceError()
    service = await build_service(config_path, expected_uid=0)
    sock: socket.socket | None = None
    try:
        path = service.socket_path
        _check_socket_parent(path)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(path)
        os.chmod(path, 0o600)
        sock.listen(128)
        sock.setblocking(False)
        config = uvicorn.Config(
            service.app,
            log_config=None,
            access_log=False,
            server_header=False,
            date_header=False,
            proxy_headers=False,
            lifespan="off",
        )
        await uvicorn.Server(config).serve(sockets=[sock])
    finally:
        if sock is not None:
            sock.close()
        try:
            os.unlink(path)
        except (UnboundLocalError, FileNotFoundError):
            pass
        await service.close()


def main(argv: list[str] | None = None, *, expected_uid: int = 0) -> int:
    parser = argparse.ArgumentParser(prog="chatcore-release-bridge")
    parser.add_argument("--config", default="/etc/chatcore/release-bridge.toml")
    parser.add_argument("--check-config", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.check_config:
            load_service_config(args.config, expected_uid=expected_uid)
            print("configuration valid")
            return 0
        asyncio.run(_serve(args.config))
    except ServiceError as exc:
        print(exc.code, file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ProtectedBridgeService",
    "ServiceConfig",
    "ServiceError",
    "build_service",
    "load_service_config",
    "main",
]
