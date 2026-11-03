"""Global group-access administration, separate from per-chat content storage."""

from chatcore.administration.store import (
    AccessSnapshot,
    AdminChange,
    AdministrationStore,
    AuditEntry,
    MemoryAdministrationStore,
    Restriction,
    StaleChangeError,
    global_scope,
    is_global_scope,
)

__all__ = [
    "AccessSnapshot",
    "AdminChange",
    "AdministrationStore",
    "AuditEntry",
    "MemoryAdministrationStore",
    "Restriction",
    "StaleChangeError",
    "global_scope",
    "is_global_scope",
]
