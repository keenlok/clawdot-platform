"""Per-conversation rendered-fetch settings."""

from __future__ import annotations

from typing import Any

from chatcore.store.base import Database

from chatcore_browse.settings import BrowseSettings


class BrowseConflict(ValueError):
    pass


class BrowseRepo:
    def __init__(self, db: Database, settings: BrowseSettings) -> None:
        self.db = db
        self.settings_config = settings

    async def settings(self, scope) -> dict[str, Any]:
        row = await self.db.fetchrow(
            "SELECT * FROM browse_settings WHERE chat_key=$1 AND is_dm=$2",
            scope.chat_key,
            scope.is_dm,
        )
        if row is None:
            return {
                "rendered_enabled": self.settings_config.dm_enabled if scope.is_dm else True,
                "revision": 0,
            }
        return dict(row)

    async def configure(
        self, scope, *, enabled: bool, actor: str, expected_revision: int
    ) -> dict[str, Any]:
        if scope.is_dm:
            raise BrowseConflict("DM settings are controlled globally")
        async with self.db.transaction() as tx:
            await tx.execute(
                "INSERT INTO browse_settings (chat_key, is_dm) VALUES ($1, false) "
                "ON CONFLICT DO NOTHING",
                scope.chat_key,
            )
            current = await tx.fetchrow(
                "SELECT * FROM browse_settings WHERE chat_key=$1 AND is_dm=false FOR UPDATE",
                scope.chat_key,
            )
            if current["revision"] != expected_revision:
                raise BrowseConflict("settings changed")
            row = await tx.fetchrow(
                """UPDATE browse_settings SET rendered_enabled=$2, revision=revision+1,
                   updated_by=$3, updated_at=now() WHERE chat_key=$1 AND is_dm=false
                   RETURNING *""",
                scope.chat_key,
                enabled,
                actor,
            )
            return dict(row)

    async def purge_chat(self, chat_key: str) -> None:
        await self.db.execute("DELETE FROM browse_settings WHERE chat_key=$1", chat_key)

    async def purge_user(self, chat_key: str, user_key: str) -> None:
        await self.db.execute(
            "UPDATE browse_settings SET updated_by=NULL WHERE chat_key=$1 AND updated_by=$2",
            chat_key,
            user_key,
        )
