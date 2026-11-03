"""Fail-closed PostgreSQL durability and runtime-role checks."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

_ALLOWED_GROUPS = frozenset({"release_control_bridge", "release_control_broker"})


class DatabaseRuntimeError(RuntimeError):
    """Safe startup failure; never includes DSNs or database diagnostics."""

    def __init__(self) -> None:
        super().__init__("database_runtime_denied")


_QUERY = """
WITH login_role AS (
    SELECT oid, rolcanlogin, rolsuper, rolcreatedb, rolcreaterole,
           rolbypassrls, rolinherit
    FROM pg_roles
    WHERE rolname = session_user
),
expected_role AS (
    SELECT oid, rolcanlogin, rolsuper, rolcreatedb, rolcreaterole,
           rolbypassrls, rolinherit
    FROM pg_roles
    WHERE rolname = $1
),
owned_objects AS (
    SELECT nspowner AS owner_oid
    FROM pg_namespace
    WHERE nspname = 'release_control'
    UNION ALL
    SELECT relation.relowner
    FROM pg_class AS relation
    JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
    WHERE namespace.nspname = 'release_control'
    UNION ALL
    SELECT procedure.proowner
    FROM pg_proc AS procedure
    JOIN pg_namespace AS namespace ON namespace.oid = procedure.pronamespace
    WHERE namespace.nspname = 'release_control'
)
SELECT current_setting('fsync') AS fsync,
       current_setting('full_page_writes') AS full_page_writes,
       current_setting('synchronous_commit') AS synchronous_commit,
       session_user::text AS session_user,
       current_user::text AS current_user,
       login_role.rolcanlogin AS login,
       login_role.rolsuper AS superuser,
       login_role.rolcreatedb AS createdb,
       login_role.rolcreaterole AS createrole,
       login_role.rolbypassrls AS bypassrls,
       login_role.rolinherit AS inherit,
       expected_role.oid IS NOT NULL AS expected_group_exists,
       expected_role.rolcanlogin AS expected_group_login,
       (expected_role.oid IS NOT NULL AND NOT expected_role.rolsuper
        AND NOT expected_role.rolcreatedb AND NOT expected_role.rolcreaterole
        AND NOT expected_role.rolbypassrls AND expected_role.rolinherit)
           AS expected_group_safe,
       COALESCE(pg_has_role(login_role.oid, expected_role.oid, 'MEMBER'), false)
           AS expected_membership,
       COALESCE(pg_has_role(login_role.oid,
           CASE WHEN $1 = 'release_control_bridge'
                THEN 'release_control_broker'::regrole::oid
                ELSE 'release_control_bridge'::regrole::oid END, 'MEMBER'), false)
           AS other_group_membership,
       COALESCE(pg_has_role(login_role.oid,
           'release_control_maintenance'::regrole::oid, 'MEMBER'), false)
           AS maintenance_membership,
       EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'release_control')
           AS control_schema_exists,
       COALESCE((SELECT bool_or(owner_oid = login_role.oid
                           OR pg_has_role(login_role.oid, owner_oid, 'MEMBER'))
                 FROM owned_objects), false) AS owner_membership
FROM login_role
LEFT JOIN expected_role ON true
"""


def _valid_group(expected_group: object) -> bool:
    return isinstance(expected_group, str) and expected_group in _ALLOWED_GROUPS


async def verify_database_connection(connection: Any, expected_group: str) -> None:
    """Verify effective durability and a restricted LOGIN with one intended group."""
    if not _valid_group(expected_group) or not callable(getattr(connection, "fetchrow", None)):
        raise DatabaseRuntimeError()
    try:
        row = await connection.fetchrow(_QUERY, expected_group)
        if row is None:
            raise ValueError
        if (
            row["fsync"] != "on"
            or row["full_page_writes"] != "on"
            or row["synchronous_commit"] not in {"on", "remote_apply"}
            or row["session_user"] != row["current_user"]
            or row["login"] is not True
            or row["superuser"] is not False
            or row["createdb"] is not False
            or row["createrole"] is not False
            or row["bypassrls"] is not False
            or row["inherit"] is not True
            or row["expected_group_exists"] is not True
            or row["expected_group_login"] is not False
            or row["expected_group_safe"] is not True
            or row["expected_membership"] is not True
            or row["other_group_membership"] is not False
            or row["maintenance_membership"] is not False
            or row["control_schema_exists"] is not True
            or row["owner_membership"] is not False
        ):
            raise ValueError
    except Exception:
        raise DatabaseRuntimeError() from None


def connection_initializer(expected_group: str) -> Callable[[Any], Any]:
    """Create asyncpg's per-connection initializer for the selected service role."""
    if not _valid_group(expected_group):
        raise DatabaseRuntimeError()

    async def initialize(connection: Any) -> None:
        await verify_database_connection(connection, expected_group)

    return initialize


async def verify_database_pool(pool: Any, expected_group: str) -> None:
    """Check one active pooled connection before the service is declared ready."""
    if not _valid_group(expected_group) or not callable(getattr(pool, "acquire", None)):
        raise DatabaseRuntimeError()
    try:
        async with pool.acquire() as connection:
            await verify_database_connection(connection, expected_group)
    except DatabaseRuntimeError:
        raise
    except Exception:
        raise DatabaseRuntimeError() from None
