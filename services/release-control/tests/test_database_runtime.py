from __future__ import annotations

import pytest
from chatcore_release_control.database_runtime import (
    DatabaseRuntimeError,
    verify_database_connection,
)


class _Connection:
    def __init__(self, **overrides):
        self.row = {
            "fsync": "on",
            "full_page_writes": "on",
            "synchronous_commit": "on",
            "session_user": "runtime_login",
            "current_user": "runtime_login",
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
        self.row.update(overrides)
        self.query = None
        self.args = None

    async def fetchrow(self, query, *args):
        self.query = query
        self.args = args
        return self.row


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "override",
    [
        {"fsync": "off"},
        {"full_page_writes": "off"},
        {"synchronous_commit": "off"},
        {"synchronous_commit": "local"},
        {"current_user": "assumed_group"},
        {"login": False},
        {"superuser": True},
        {"createdb": True},
        {"createrole": True},
        {"bypassrls": True},
        {"inherit": False},
        {"expected_group_exists": False},
        {"expected_group_login": True},
        {"expected_group_safe": False},
        {"expected_membership": False},
        {"other_group_membership": True},
        {"maintenance_membership": True},
        {"control_schema_exists": False},
        {"owner_membership": True},
    ],
)
async def test_database_runtime_rejects_unsafe_settings_or_identity(override):
    with pytest.raises(DatabaseRuntimeError, match="database_runtime_denied"):
        await verify_database_connection(_Connection(**override), "release_control_bridge")


@pytest.mark.asyncio
async def test_database_runtime_accepts_durable_session_and_expected_group_membership():
    connection = _Connection(synchronous_commit="remote_apply")
    await verify_database_connection(connection, "release_control_bridge")
    assert connection.args == ("release_control_bridge",)
    assert "pg_has_role" in connection.query


@pytest.mark.asyncio
async def test_database_runtime_rejects_unknown_group_before_query():
    connection = _Connection()
    with pytest.raises(DatabaseRuntimeError, match="database_runtime_denied"):
        await verify_database_connection(connection, "release_control_maintenance")
    assert connection.query is None
