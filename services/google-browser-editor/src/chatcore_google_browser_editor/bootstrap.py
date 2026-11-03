"""Operator-only visible sign-in helper for the dedicated editor account."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import stat
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import async_playwright

from .docs_ui import DocsUiAdapter
from .engine import clean_browser_environment
from .resource_hosts import RESOURCE_HOSTS, allowed_cookie_domain


def _has_checkout_ancestor(path: Path) -> bool:
    current = path.resolve()
    while True:
        if os.path.lexists(current / ".git"):
            return True
        if current.parent == current:
            return False
        current = current.parent


def _require_private_directory(path: Path) -> None:
    metadata = path.stat()
    mode = stat.S_IMODE(metadata.st_mode)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or mode & 0o077
        or mode & 0o700 != 0o700
    ):
        raise ValueError("session seed output requires a private directory owned by this user")


def _create_private_parents(parent: Path) -> None:
    missing = []
    current = parent
    while not current.exists():
        missing.append(current)
        if current.parent == current:
            raise ValueError("session seed parent directory is unavailable")
        current = current.parent
    if not current.is_dir():
        raise ValueError("session seed parent must be a directory")
    for directory in reversed(missing):
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError as exc:
            raise ValueError("session seed parent changed during setup") from exc
        directory.chmod(0o700)
    _require_private_directory(parent)


def validate_output_path(output: Path) -> Path:
    requested = output.expanduser().absolute()
    parent = requested.parent.resolve()
    path = parent / requested.name
    if _has_checkout_ancestor(parent):
        raise ValueError("session seed must be outside the repository")
    if os.path.lexists(path):
        raise ValueError("session seed output already exists")
    _create_private_parents(parent)
    return path


def validate_seed_state(state: object, expected_email: str) -> None:
    if not isinstance(state, dict) or not isinstance(state.get("cookies"), list):
        raise RuntimeError("session seed has an unsupported format")
    for cookie in state["cookies"]:
        if not isinstance(cookie, dict) or not allowed_cookie_domain(str(cookie.get("domain", ""))):
            raise RuntimeError("session seed contains a non-Google cookie")
    origins = state.get("origins", [])
    if not isinstance(origins, list):
        raise RuntimeError("session seed has an unsupported origin")
    for origin in origins:
        if not isinstance(origin, dict) or not isinstance(origin.get("origin"), str):
            raise RuntimeError("session seed has an unsupported origin")
        parsed = urlsplit(origin["origin"])
        if parsed.scheme != "https" or parsed.hostname not in RESOURCE_HOSTS:
            raise RuntimeError("session seed contains a non-allowlisted origin")


def validate_seed_file(path: Path, expected_email: str) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise RuntimeError("session seed is invalid") from exc
    if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
        raise RuntimeError("session seed must be a regular mode-0600 file")
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("session seed is invalid") from exc
    validate_seed_state(state, expected_email)


def write_seed_state(path: Path, state: object, expected_email: str) -> None:
    """Exclusively create and write session state without exposing a permissive inode."""
    validate_seed_state(state, expected_email)
    encoded = json.dumps(state, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    directory_fd = os.open(
        path.parent,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    output_fd: int | None = None
    created_info: os.stat_result | None = None
    try:
        _require_private_directory(path.parent)
        output_fd = os.open(
            path.name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=directory_fd,
        )
        created_info = os.fstat(output_fd)
        os.fchmod(output_fd, 0o600)
        offset = 0
        while offset < len(encoded):
            offset += os.write(output_fd, encoded[offset:])
        os.fsync(output_fd)
        os.close(output_fd)
        output_fd = None
        validate_seed_file(path, expected_email)
        os.fsync(directory_fd)
    except BaseException:
        if output_fd is not None:
            try:
                created_info = os.fstat(output_fd)
            except OSError:
                pass
        if created_info is not None:
            try:
                current = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
                if (current.st_dev, current.st_ino) == (created_info.st_dev, created_info.st_ino):
                    os.unlink(path.name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
        raise
    finally:
        if output_fd is not None:
            os.close(output_fd)
        os.close(directory_fd)


async def bootstrap_session(output: Path, editor_email: str) -> None:
    output = validate_output_path(output)
    with tempfile.TemporaryDirectory(prefix="google-browser-bootstrap-") as home:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                headless=False,
                chromium_sandbox=True,
                args=["--disable-quic"],
                env=clean_browser_environment(home),
            )
            try:
                context = await browser.new_context(accept_downloads=False, service_workers="block")
                page = await context.new_page()
                await page.goto("https://docs.google.com", wait_until="domcontentloaded")
                await asyncio.to_thread(
                    input,
                    "Sign in visibly as the dedicated editor account, complete MFA, "
                    "then press Enter here. ",
                )
                await DocsUiAdapter(page, editor_email).check_identity()
                state = await context.storage_state()
                write_seed_state(output, state, editor_email)
            finally:
                await browser.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Bootstrap the dedicated Google Docs editor session"
    )
    parser.add_argument("--editor-email", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.editor_email != args.editor_email.casefold():
        parser.error("--editor-email must be lowercase")
    try:
        asyncio.run(bootstrap_session(args.output, args.editor_email))
    except (ValueError, RuntimeError) as exc:
        parser.error(str(exc))
