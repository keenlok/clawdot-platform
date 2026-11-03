"""Per-package SQL migrations (§7a).

Each package ships ``NNNN_name.sql`` files. They are applied in order core → plugins and recorded as
``(package, version)``, so packages never collide on numbering. Migrations must be additive (§6a),
so the previous release keeps working against the new schema.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from importlib.resources.abc import Traversable

from chatcore.store.pg import PgDatabase

log = logging.getLogger(__name__)

_FILE = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")
_LOCK_ID = 0x6368_6174  # pg advisory lock key: one migrator at a time

_BOOTSTRAP = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    package     text        NOT NULL,
    version     int         NOT NULL,
    name        text        NOT NULL,
    checksum    text        NOT NULL,
    applied_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (package, version)
)
"""


@dataclass(frozen=True, slots=True)
class Migration:
    package: str
    version: int
    name: str
    sql: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode()).hexdigest()


class MigrationError(Exception):
    pass


def discover(package: str, directory: Traversable | None) -> list[Migration]:
    if directory is None:
        return []
    found: list[Migration] = []
    for entry in directory.iterdir():
        match = _FILE.match(entry.name)
        if match:
            found.append(Migration(package, int(match[1]), entry.name, entry.read_text("utf-8")))
        elif entry.name.endswith(".sql"):
            raise MigrationError(f"{package}: bad migration filename {entry.name!r}")
    found.sort(key=lambda m: m.version)
    versions = [m.version for m in found]
    if len(set(versions)) != len(versions):
        raise MigrationError(f"{package}: duplicate migration versions")
    return found


async def apply(db: PgDatabase, packages: Sequence[tuple[str, Traversable | None]]) -> list[str]:
    """Apply every pending migration in one transaction (all or nothing); returns the names applied.

    An already-applied file whose content changed is an error: fix forward with a new file.
    """
    applied_names: list[str] = []
    async with db.transaction() as tx:
        await tx.execute("SELECT pg_advisory_xact_lock($1)", _LOCK_ID)
        await tx.execute(_BOOTSTRAP)
        rows = await tx.fetch("SELECT package, version, checksum FROM schema_migrations")
        done = {(r["package"], r["version"]): r["checksum"] for r in rows}
        for package, directory in packages:
            for migration in discover(package, directory):
                key = (package, migration.version)
                if key in done:
                    if done[key] != migration.checksum:
                        raise MigrationError(
                            f"{package}/{migration.name} changed after it was applied"
                        )
                    continue
                await tx.execute(migration.sql)
                await tx.execute(
                    "INSERT INTO schema_migrations (package, version, name, checksum) "
                    "VALUES ($1, $2, $3, $4)",
                    package,
                    migration.version,
                    migration.name,
                    migration.checksum,
                )
                applied_names.append(f"{package}/{migration.name}")
                log.info("applied migration %s/%s", package, migration.name)
    return applied_names
