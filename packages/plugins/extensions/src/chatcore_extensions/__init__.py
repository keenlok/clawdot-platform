"""Scoped sandboxed extensions for chatcore.

Public names load lazily. The extension worker starts as ``chatcore_extensions.worker``,
which imports this package first; eager re-exports would load the controller, generator
and Postgres code into that privileged process.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

_EXPORTS = {
    "ExtensionAuthority": "chatcore_extensions.authority",
    "ExtensionGenerator": "chatcore_extensions.generator",
    "ExtensionService": "chatcore_extensions.service",
    "ExtensionsPlugin": "chatcore_extensions.plugin",
    "ExtensionsSettings": "chatcore_extensions.settings",
    "HARNESS_DIGEST": "chatcore_extensions.sandbox",
    "MemoryExtensionRepo": "chatcore_extensions.repository",
    "PostgresExtensionRepo": "chatcore_extensions.pg_repository",
    "SandboxPolicy": "chatcore_extensions.sandbox",
    "WorkerClient": "chatcore_extensions.worker",
    "read_token_file": "chatcore_extensions.worker",
}

__all__ = [
    "HARNESS_DIGEST",
    "ExtensionAuthority",
    "ExtensionGenerator",
    "ExtensionService",
    "ExtensionsPlugin",
    "ExtensionsSettings",
    "MemoryExtensionRepo",
    "PostgresExtensionRepo",
    "SandboxPolicy",
    "WorkerClient",
    "read_token_file",
]

if TYPE_CHECKING:
    from chatcore_extensions.authority import ExtensionAuthority
    from chatcore_extensions.generator import ExtensionGenerator
    from chatcore_extensions.pg_repository import PostgresExtensionRepo
    from chatcore_extensions.plugin import ExtensionsPlugin
    from chatcore_extensions.repository import MemoryExtensionRepo
    from chatcore_extensions.sandbox import HARNESS_DIGEST, SandboxPolicy
    from chatcore_extensions.service import ExtensionService
    from chatcore_extensions.settings import ExtensionsSettings
    from chatcore_extensions.worker import WorkerClient, read_token_file


def __getattr__(name: str) -> Any:
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
