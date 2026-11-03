"""Read-only protected host watchdog with durable, deduplicated bridge alerts."""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import os
import re
import resource
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from .broker_http import ProtectedHostClient
from .host_journal import HostJournal

CONFIG_PATH = Path("/etc/chatcore-release-control/watchdog.json")
STATE_PATH = Path("/var/lib/chatcore-release-control/watchdog-state.json")
JOURNAL_PATH = Path("/var/lib/chatcore-release-control/host-journal.json")
HOST_SOCKET_PATH = Path("/run/chatcore-release-control/host.sock")
BRIDGE_SOCKET_PATH = Path("/run/chatcore-release-control/bridge.sock")
HOST_CREDENTIAL_PATH = Path("/etc/chatcore-release-control/host-client.token")
BRIDGE_CREDENTIAL_PATH = Path("/etc/chatcore-release-control/bridge-broker.token")
FIXED_UNITS = (
    ("bridge", "chatcore-release-bridge.service"),
    ("broker", "chatcore-release-broker.service"),
    ("host", "chatcore-release-host.service"),
    ("worker", "chatcore-release-worker.service"),
)
_FIXED_UNIT_MAP = dict(FIXED_UNITS)
_FIXED_DISK_PATHS = ("/", "/var/lib/chatcore-release-control", "/var/lib/docker")
FIXED_DISK_PATHS = _FIXED_DISK_PATHS
_MAX_CONFIG_BYTES = 32 * 1024
_MAX_STATE_BYTES = 64 * 1024
_MAX_SECRET_BYTES = 4096
_MAX_SYSTEMD_OUTPUT = 4096
_MAX_JOURNAL_SECONDS = 60
_TARGET = re.compile(r"[A-Za-z0-9._-]{1,64}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z")
_STATE_KEY = re.compile(r"[a-z_]+:[a-z_]+\Z")


class WatchdogError(RuntimeError):
    """Fixed, safe watchdog error; no command, host, token or log content."""

    def __init__(self, code: str = "watchdog_unavailable") -> None:
        if code not in {"watchdog_unavailable", "watchdog_invalid_configuration"}:
            code = "watchdog_unavailable"
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class MetricsSnapshot:
    cpu_busy_percent: int
    load_per_cpu_milli: int
    available_memory_bytes: int
    disk_free_bytes: Mapping[str, int]


@dataclass(frozen=True)
class MonitorFinding:
    kind: str
    service: str

    @property
    def key(self) -> str:
        return f"{self.service}:{self.kind}"


@dataclass(frozen=True)
class WatchdogReport:
    healthy: bool
    findings: tuple[MonitorFinding, ...]
    notifications_pending: bool


@dataclass(frozen=True)
class WatchdogConfig:
    target: str
    state_path: Path
    journal_path: Path
    host_socket: Path
    host_credential_file: Path
    bridge_socket: Path
    bridge_credential_file: Path
    units: tuple[tuple[str, str], ...]
    max_cpu_busy_percent: int
    max_load_per_cpu_milli: int
    min_available_memory_bytes: int
    disk_free_bytes: tuple[tuple[str, int], ...]
    sample_interval_seconds: int
    poll_interval_seconds: int

    def __post_init__(self) -> None:
        try:
            if (
                not isinstance(self.target, str)
                or not _TARGET.fullmatch(self.target)
                or self.units != FIXED_UNITS
                or any(
                    not isinstance(path, Path) or not path.is_absolute()
                    for path in (
                        self.state_path,
                        self.journal_path,
                        self.host_socket,
                        self.host_credential_file,
                        self.bridge_socket,
                        self.bridge_credential_file,
                    )
                )
                or type(self.max_cpu_busy_percent) is not int
                or not 1 <= self.max_cpu_busy_percent <= 100
                or type(self.max_load_per_cpu_milli) is not int
                or not 1 <= self.max_load_per_cpu_milli <= 1_000_000
                or type(self.min_available_memory_bytes) is not int
                or not 1 <= self.min_available_memory_bytes <= 2**63 - 1
                or not isinstance(self.disk_free_bytes, tuple)
                or tuple(path for path, _ in self.disk_free_bytes) != _FIXED_DISK_PATHS
                or any(
                    type(threshold) is not int or not 1 <= threshold <= 2**63 - 1
                    for _, threshold in self.disk_free_bytes
                )
                or type(self.sample_interval_seconds) is not int
                or not 1 <= self.sample_interval_seconds <= 30
                or type(self.poll_interval_seconds) is not int
                or not 5 <= self.poll_interval_seconds <= 3600
            ):
                raise ValueError
        except (TypeError, ValueError):
            raise WatchdogError("watchdog_invalid_configuration") from None

    def __repr__(self) -> str:
        return "WatchdogConfig([protected])"


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _root_file(path: Path, *, expected_uid: int, maximum: int, mode: int = 0o600) -> bytes:
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != expected_uid
            or stat.S_IMODE(info.st_mode) != mode
            or not 1 <= info.st_size <= maximum
        ):
            raise ValueError
        data = bytearray()
        while chunk := os.read(descriptor, min(8192, maximum + 1 - len(data))):
            data.extend(chunk)
            if len(data) > maximum:
                raise ValueError
        return bytes(data)
    except Exception:
        raise WatchdogError("watchdog_invalid_configuration") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _check_root_file(path: Path, *, expected_uid: int, maximum: int) -> None:
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != expected_uid
            or stat.S_IMODE(info.st_mode) != 0o600
            or not 1 <= info.st_size <= maximum
        ):
            raise ValueError
    except Exception:
        raise WatchdogError("watchdog_invalid_configuration") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _safe_directory(path: Path, expected_uid: int) -> None:
    try:
        info = path.lstat()
        if (
            not path.is_absolute()
            or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != expected_uid
            or stat.S_IMODE(info.st_mode) & 0o022
            or path.resolve(strict=True) != path
        ):
            raise ValueError
    except Exception:
        raise WatchdogError("watchdog_invalid_configuration") from None


def load_watchdog_config(
    path: str | os.PathLike[str] = CONFIG_PATH, *, expected_uid: int = 0
) -> WatchdogConfig:
    config_path = Path(path)
    if config_path != CONFIG_PATH or type(expected_uid) is not int or expected_uid < 0:
        raise WatchdogError("watchdog_invalid_configuration")
    _safe_directory(config_path.parent, expected_uid)
    raw = _root_file(config_path, expected_uid=expected_uid, maximum=_MAX_CONFIG_BYTES)
    try:
        value = json.loads(raw, object_pairs_hook=_unique_pairs)
        required = {
            "version",
            "target",
            "state_path",
            "journal_path",
            "host_socket",
            "host_credential_file",
            "bridge_socket",
            "bridge_credential_file",
            "units",
            "max_cpu_busy_percent",
            "max_load_per_cpu_milli",
            "min_available_memory_bytes",
            "disk_free_bytes",
            "sample_interval_seconds",
            "poll_interval_seconds",
        }
        if (
            not isinstance(value, dict)
            or set(value) != required
            or type(value["version"]) is not int
            or value["version"] != 1
            or value["units"] != _FIXED_UNIT_MAP
            or value["state_path"] != str(STATE_PATH)
            or value["journal_path"] != str(JOURNAL_PATH)
            or value["host_socket"] != str(HOST_SOCKET_PATH)
            or value["bridge_socket"] != str(BRIDGE_SOCKET_PATH)
            or value["host_credential_file"] != str(HOST_CREDENTIAL_PATH)
            or value["bridge_credential_file"] != str(BRIDGE_CREDENTIAL_PATH)
            or not isinstance(value["disk_free_bytes"], dict)
            or tuple(value["disk_free_bytes"]) != _FIXED_DISK_PATHS
        ):
            raise ValueError
        for disk_path in _FIXED_DISK_PATHS:
            path_value = Path(disk_path)
            _safe_directory(path_value, expected_uid)
        for credential_path in (HOST_CREDENTIAL_PATH, BRIDGE_CREDENTIAL_PATH):
            _check_root_file(credential_path, expected_uid=expected_uid, maximum=_MAX_SECRET_BYTES)
        state_parent = STATE_PATH.parent
        _safe_directory(state_parent, expected_uid)
        units = tuple((service, _FIXED_UNIT_MAP[service]) for service, _ in FIXED_UNITS)
        thresholds = tuple((key, value["disk_free_bytes"][key]) for key in _FIXED_DISK_PATHS)
        return WatchdogConfig(
            target=value["target"],
            state_path=STATE_PATH,
            journal_path=JOURNAL_PATH,
            host_socket=HOST_SOCKET_PATH,
            host_credential_file=HOST_CREDENTIAL_PATH,
            bridge_socket=BRIDGE_SOCKET_PATH,
            bridge_credential_file=BRIDGE_CREDENTIAL_PATH,
            units=units,
            max_cpu_busy_percent=value["max_cpu_busy_percent"],
            max_load_per_cpu_milli=value["max_load_per_cpu_milli"],
            min_available_memory_bytes=value["min_available_memory_bytes"],
            disk_free_bytes=thresholds,
            sample_interval_seconds=value["sample_interval_seconds"],
            poll_interval_seconds=value["poll_interval_seconds"],
        )
    except WatchdogError:
        raise
    except Exception:
        raise WatchdogError("watchdog_invalid_configuration") from None


class SystemdProbe:
    """Read exactly ActiveState for four installed service units."""

    def __init__(self, runner: Any = None) -> None:
        self._runner = runner or _run_systemctl

    def active_state(self, unit: str) -> str:
        if unit not in _FIXED_UNIT_MAP.values():
            raise WatchdogError()
        raw = self._runner(
            ("/usr/bin/systemctl", "show", "--property=ActiveState", "--value", unit),
            timeout=5,
            output_limit=_MAX_SYSTEMD_OUTPUT,
        )
        if not isinstance(raw, bytes) or len(raw) > _MAX_SYSTEMD_OUTPUT:
            raise WatchdogError()
        try:
            value = raw.decode("ascii", errors="strict").strip()
        except UnicodeError:
            raise WatchdogError() from None
        if value not in {"active", "inactive", "failed", "activating", "deactivating"}:
            raise WatchdogError()
        return value


def _run_systemctl(args: Sequence[str], *, timeout: int, output_limit: int) -> bytes:
    expected = {"PATH": "/usr/bin:/bin", "HOME": "/root", "LC_ALL": "C", "LANG": "C"}
    if (
        not isinstance(args, (tuple, list))
        or len(args) != 5
        or args[:4] != ("/usr/bin/systemctl", "show", "--property=ActiveState", "--value")
        or args[4] not in _FIXED_UNIT_MAP.values()
        or type(timeout) is not int
        or not 1 <= timeout <= 5
        or type(output_limit) is not int
        or not 1 <= output_limit <= _MAX_SYSTEMD_OUTPUT
    ):
        raise WatchdogError()
    try:
        with tempfile.TemporaryFile() as stdout:
            process = subprocess.Popen(  # noqa: S603 - arguments are fixed to status-only systemctl.
                list(args),
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=subprocess.DEVNULL,
                env=expected,
                close_fds=True,
                preexec_fn=lambda: resource.setrlimit(
                    resource.RLIMIT_FSIZE, (output_limit, output_limit)
                ),
            )
            try:
                status = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                raise WatchdogError() from None
            stdout.seek(0, os.SEEK_END)
            size = stdout.tell()
            if status != 0 or size > output_limit:
                raise WatchdogError()
            stdout.seek(0)
            result = stdout.read(output_limit + 1)
            if len(result) > output_limit:
                raise WatchdogError()
            return result
    except WatchdogError:
        raise
    except Exception:
        raise WatchdogError() from None


class ProcMetrics:
    """Read bounded `/proc` counters and statvfs for exact configured paths."""

    def __init__(
        self,
        *,
        reader: Callable[[Path, int], bytes] | None = None,
        sleep=time.sleep,
        statvfs=os.statvfs,
    ):
        self._reader = reader or _read_file
        self._sleep = sleep
        self._statvfs = statvfs

    def read(self, config: WatchdogConfig) -> MetricsSnapshot:
        try:
            first = _parse_cpu_stat(self._reader(Path("/proc/stat"), 64 * 1024))
            self._sleep(config.sample_interval_seconds)
            second = _parse_cpu_stat(self._reader(Path("/proc/stat"), 64 * 1024))
            delta = tuple(after - before for before, after in zip(first[0], second[0], strict=True))
            total = sum(delta)
            idle = delta[3] + (delta[4] if len(delta) > 4 else 0)
            if total <= 0 or idle < 0 or idle > total:
                raise ValueError
            cpu_busy = round((total - idle) * 100 / total)
            cores = second[1]
            load = float(self._reader(Path("/proc/loadavg"), 4096).split()[0])
            if load < 0:
                raise ValueError
            load_per_cpu = round(load * 1000 / cores)
            available = _parse_available_memory(self._reader(Path("/proc/meminfo"), 64 * 1024))
            disk_free = {}
            for path, _ in config.disk_free_bytes:
                stats = self._statvfs(path)
                disk_free[path] = stats.f_bavail * stats.f_frsize
            if not 0 <= cpu_busy <= 100 or load_per_cpu < 0:
                raise ValueError
            return MetricsSnapshot(cpu_busy, load_per_cpu, available, disk_free)
        except Exception:
            raise WatchdogError() from None


def _read_file(path: Path, maximum: int) -> bytes:
    with path.open("rb") as stream:
        data = stream.read(maximum + 1)
    if len(data) > maximum:
        raise ValueError
    return data


def _parse_cpu_stat(raw: bytes) -> tuple[tuple[int, ...], int]:
    if not isinstance(raw, bytes) or len(raw) > 64 * 1024:
        raise ValueError
    lines = raw.decode("ascii", errors="strict").splitlines()
    aggregate = None
    cores = 0
    for line in lines:
        fields = line.split()
        if fields and fields[0] == "cpu":
            if aggregate is not None or len(fields) < 5:
                raise ValueError
            aggregate = tuple(int(value) for value in fields[1:])
        elif fields and re.fullmatch(r"cpu[0-9]+", fields[0]):
            cores += 1
    if aggregate is None or not 1 <= cores <= 4096 or len(aggregate) < 5:
        raise ValueError
    return aggregate, cores


def _parse_available_memory(raw: bytes) -> int:
    if not isinstance(raw, bytes) or len(raw) > 64 * 1024:
        raise ValueError
    found = []
    for line in raw.decode("ascii", errors="strict").splitlines():
        fields = line.split()
        if fields and fields[0] == "MemAvailable:":
            if len(fields) != 3 or fields[2] != "kB":
                raise ValueError
            found.append(int(fields[1]) * 1024)
    if len(found) != 1 or found[0] < 0:
        raise ValueError
    return found[0]


class WatchdogStateStore:
    """Private append-by-replacement incident state with fsync and process lock."""

    def __init__(self, path: Path, *, owner_uid: int) -> None:
        if not isinstance(path, Path) or not path.is_absolute() or type(owner_uid) is not int:
            raise WatchdogError("watchdog_invalid_configuration")
        self.path = path
        self.owner_uid = owner_uid
        self.lock_path = path.with_name(path.name + ".lock")

    @contextmanager
    def _locked(self):
        descriptor = -1
        try:
            parent = self.path.parent.lstat()
            if (
                not stat.S_ISDIR(parent.st_mode)
                or parent.st_uid != self.owner_uid
                or stat.S_IMODE(parent.st_mode) & 0o022
                or self.path.parent.resolve(strict=True) != self.path.parent
            ):
                raise ValueError
            descriptor = os.open(
                self.lock_path,
                os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
                0o600,
            )
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != self.owner_uid
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                raise ValueError
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        except WatchdogError:
            raise
        except Exception:
            raise WatchdogError() from None
        finally:
            if descriptor >= 0:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                except OSError:
                    pass
                os.close(descriptor)

    def _read_locked(self) -> dict[str, Any]:
        try:
            descriptor = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return {"version": 1, "incidents": {}}
        except OSError:
            raise WatchdogError() from None
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != self.owner_uid
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_size > _MAX_STATE_BYTES
            ):
                raise ValueError
            raw = bytearray()
            while chunk := os.read(descriptor, min(8192, _MAX_STATE_BYTES + 1 - len(raw))):
                raw.extend(chunk)
                if len(raw) > _MAX_STATE_BYTES:
                    raise ValueError
            state = json.loads(bytes(raw), object_pairs_hook=_unique_pairs)
            _validate_state(state)
            return state
        except WatchdogError:
            raise
        except Exception:
            raise WatchdogError() from None
        finally:
            os.close(descriptor)

    def _write_locked(self, state: dict[str, Any]) -> None:
        _validate_state(state)
        raw = json.dumps(state, sort_keys=True, separators=(",", ":")).encode("ascii")
        if len(raw) > _MAX_STATE_BYTES:
            raise WatchdogError()
        descriptor = -1
        temporary: str | None = None
        try:
            descriptor, temporary = tempfile.mkstemp(
                prefix=f".{self.path.name}.", dir=self.path.parent
            )
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb", closefd=True) as stream:
                descriptor = -1
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            temporary = None
            parent_fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
        except Exception:
            raise WatchdogError() from None
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass

    def _mutate(self, operation: Callable[[dict[str, Any]], Any]) -> Any:
        with self._locked():
            state = self._read_locked()
            before = json.dumps(state, sort_keys=True, separators=(",", ":"))
            result = operation(state)
            after = json.dumps(state, sort_keys=True, separators=(",", ":"))
            if after != before:
                self._write_locked(state)
            return result

    def get(self, key: str) -> dict[str, Any] | None:
        if not _STATE_KEY.fullmatch(key):
            raise WatchdogError()
        with self._locked():
            value = self._read_locked()["incidents"].get(key)
            return None if value is None else dict(value)

    def active_incidents(self) -> dict[str, dict[str, Any]]:
        with self._locked():
            state = self._read_locked()
            return {
                key: dict(value)
                for key, value in state["incidents"].items()
                if value["state"] == "firing"
            }

    def prepare_firing(self, key: str) -> dict[str, Any]:
        if not _STATE_KEY.fullmatch(key):
            raise WatchdogError()

        def update(state):
            value = state["incidents"].get(key)
            if value is None or value["state"] == "resolved":
                value = {
                    "incident_id": str(uuid4()),
                    "state": "firing",
                    "firing_request_id": str(uuid4()),
                    "firing_sent": False,
                    "resolved_request_id": None,
                    "resolved_sent": False,
                }
                state["incidents"][key] = value
            return dict(value)

        return self._mutate(update)

    def prepare_resolved(self, key: str, incident_id: str) -> dict[str, Any]:
        if not _STATE_KEY.fullmatch(key) or not _is_uuid(incident_id):
            raise WatchdogError()

        def update(state):
            value = state["incidents"].get(key)
            if (
                value is None
                or value["incident_id"] != incident_id
                or (value["state"] == "firing" and value["firing_sent"] is not True)
            ):
                raise ValueError
            if value["state"] == "firing":
                value["state"] = "resolved"
                value["resolved_request_id"] = str(uuid4())
                value["resolved_sent"] = False
            return dict(value)

        return self._mutate(update)

    def mark_sent(self, key: str, incident_id: str, transition: str) -> None:
        if (
            not _STATE_KEY.fullmatch(key)
            or not _is_uuid(incident_id)
            or transition not in {"firing", "resolved"}
        ):
            raise WatchdogError()

        def update(state):
            value = state["incidents"].get(key)
            if value is None or value["incident_id"] != incident_id:
                raise ValueError
            if transition == "firing":
                value["firing_sent"] = True
            else:
                if value["state"] != "resolved":
                    raise ValueError
                value["resolved_sent"] = True
            return None

        self._mutate(update)


def _is_uuid(value: object) -> bool:
    return isinstance(value, str) and _UUID.fullmatch(value) is not None


def _validate_state(value: object) -> None:
    if (
        not isinstance(value, dict)
        or set(value) != {"version", "incidents"}
        or value["version"] != 1
        or not isinstance(value["incidents"], dict)
        or len(value["incidents"]) > 256
    ):
        raise WatchdogError()
    fields = {
        "incident_id",
        "state",
        "firing_request_id",
        "firing_sent",
        "resolved_request_id",
        "resolved_sent",
    }
    for key, row in value["incidents"].items():
        if (
            not isinstance(key, str)
            or not _STATE_KEY.fullmatch(key)
            or not isinstance(row, dict)
            or set(row) != fields
            or not _is_uuid(row["incident_id"])
            or row["state"] not in {"firing", "resolved"}
            or not _is_uuid(row["firing_request_id"])
            or type(row["firing_sent"]) is not bool
            or (row["state"] == "firing" and row["resolved_request_id"] is not None)
            or (row["state"] == "firing" and row["resolved_sent"] is not False)
            or (row["state"] == "resolved" and not _is_uuid(row["resolved_request_id"]))
            or (row["state"] == "resolved" and row["firing_sent"] is not True)
            or type(row["resolved_sent"]) is not bool
        ):
            raise WatchdogError()


class WatchdogBridgeClient:
    """Fixed authenticated bridge UDS client; sends no caller-provided text."""

    def __init__(
        self,
        *,
        socket_path: Path,
        credential: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if (
            not socket_path.is_absolute()
            or not isinstance(credential, str)
            or re.fullmatch(r"[A-Za-z0-9_.-]{32,4096}", credential) is None
        ):
            raise WatchdogError("watchdog_invalid_configuration")
        self._credential = credential
        self._http = httpx.AsyncClient(
            transport=(
                transport
                if transport is not None
                else httpx.AsyncHTTPTransport(uds=str(socket_path))
            ),
            base_url="http://protected-bridge",
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(5.0, connect=2.0),
            headers={"Authorization": "Bearer " + credential},
        )

    async def send_alert(self, *, request_id: str, kind: str, service: str, state: str) -> None:
        if (
            not _is_uuid(request_id)
            or not isinstance(kind, str)
            or kind not in _ALLOWED_KINDS
            or not isinstance(service, str)
            or service not in _ALLOWED_SERVICES
            or not isinstance(state, str)
            or state not in {"firing", "resolved"}
        ):
            raise WatchdogError()
        try:
            async with self._http.stream(
                "POST",
                "/v1/broker/alerts",
                json={"request_id": request_id, "kind": kind, "service": service, "state": state},
            ) as response:
                if response.status_code != 200:
                    raise ValueError
                raw = bytearray()
                async for chunk in response.aiter_bytes(2048):
                    if len(raw) + len(chunk) > 8192:
                        raise ValueError
                    raw.extend(chunk)
            value = json.loads(bytes(raw), object_pairs_hook=_unique_pairs)
            if (
                not isinstance(value, dict)
                or set(value) != {"request_id", "state", "message_id"}
                or value["request_id"] != request_id
                or value["state"] != state
                or type(value["message_id"]) is not int
                or value["message_id"] <= 0
            ):
                raise ValueError
        except Exception:
            raise WatchdogError() from None

    async def close(self) -> None:
        await self._http.aclose()


_ALLOWED_KINDS = frozenset(
    {
        "service_inactive",
        "systemd_unavailable",
        "cpu_pressure",
        "load_pressure",
        "memory_pressure",
        "disk_pressure",
        "metrics_unavailable",
        "host_state_unavailable",
        "host_unhealthy",
        "journal_unavailable",
        "journal_deadline",
        "journal_recovery_failed",
    }
)
_ALLOWED_SERVICES = frozenset({"bridge", "broker", "host", "worker", "metrics", "journal"})


class WatchdogMonitor:
    def __init__(
        self,
        config: WatchdogConfig,
        *,
        systemd: Any,
        metrics: Any,
        host: Any,
        journal: Any,
        store: WatchdogStateStore,
        bridge: Any,
        now: Callable[[], int] = lambda: int(time.time()),
    ) -> None:
        if (
            not isinstance(config, WatchdogConfig)
            or not callable(getattr(systemd, "active_state", None))
            or not callable(getattr(metrics, "read", None))
            or not callable(getattr(host, "current_state", None))
            or not callable(getattr(journal, "current_record", None))
            or not isinstance(store, WatchdogStateStore)
            or not callable(getattr(bridge, "send_alert", None))
            or not callable(now)
        ):
            raise WatchdogError("watchdog_invalid_configuration")
        self.config = config
        self.systemd, self.metrics, self.host = systemd, metrics, host
        self.journal, self.store, self.bridge, self._now = journal, store, bridge, now

    async def run_once(self, *, dry_run: bool = False) -> WatchdogReport:
        findings = await self.collect_findings()
        if dry_run:
            return WatchdogReport(not findings, findings, False)
        pending = False
        active = {finding.key: finding for finding in findings}
        for key, finding in active.items():
            previous = self.store.get(key)
            if (
                previous is not None
                and previous["state"] == "resolved"
                and not previous["resolved_sent"]
            ):
                try:
                    await self._send(finding, previous["resolved_request_id"], "resolved")
                    self.store.mark_sent(key, previous["incident_id"], "resolved")
                except WatchdogError:
                    pending = True
                    continue
            row = self.store.prepare_firing(key)
            if not row["firing_sent"]:
                try:
                    await self._send(finding, row["firing_request_id"], "firing")
                    self.store.mark_sent(key, row["incident_id"], "firing")
                except WatchdogError:
                    pending = True
        for key, row in self.store.active_incidents().items():
            if key in active:
                continue
            finding = _finding_from_key(key)
            if not row["firing_sent"]:
                try:
                    await self._send(finding, row["firing_request_id"], "firing")
                    self.store.mark_sent(key, row["incident_id"], "firing")
                    row = self.store.get(key) or row
                except WatchdogError:
                    pending = True
                    continue
            row = self.store.prepare_resolved(key, row["incident_id"])
            if not row["resolved_sent"]:
                try:
                    await self._send(finding, row["resolved_request_id"], "resolved")
                    self.store.mark_sent(key, row["incident_id"], "resolved")
                except WatchdogError:
                    pending = True
        return WatchdogReport(not findings and not pending, findings, pending)

    async def collect_findings(self) -> tuple[MonitorFinding, ...]:
        found: set[MonitorFinding] = set()
        for service, unit in self.config.units:
            try:
                state = self.systemd.active_state(unit)
                if state != "active":
                    found.add(MonitorFinding("service_inactive", service))
            except Exception:
                found.add(MonitorFinding("systemd_unavailable", service))
        try:
            metrics = self.metrics.read(self.config)
            if not isinstance(metrics, MetricsSnapshot):
                raise ValueError
            if metrics.cpu_busy_percent > self.config.max_cpu_busy_percent:
                found.add(MonitorFinding("cpu_pressure", "metrics"))
            if metrics.load_per_cpu_milli > self.config.max_load_per_cpu_milli:
                found.add(MonitorFinding("load_pressure", "metrics"))
            if metrics.available_memory_bytes < self.config.min_available_memory_bytes:
                found.add(MonitorFinding("memory_pressure", "metrics"))
            for path, threshold in self.config.disk_free_bytes:
                free = metrics.disk_free_bytes.get(path)
                if type(free) is not int or free < threshold:
                    found.add(MonitorFinding("disk_pressure", "metrics"))
        except Exception:
            found.add(MonitorFinding("metrics_unavailable", "metrics"))
        try:
            observation = await self.host.current_state(self.config.target)
            if getattr(observation, "healthy", None) is not True:
                found.add(MonitorFinding("host_unhealthy", "host"))
        except Exception:
            found.add(MonitorFinding("host_state_unavailable", "host"))
        try:
            record = self.journal.current_record()
            if record is not None:
                phase = getattr(record.phase, "value", record.phase)
                now = self._now()
                if phase == "recovery_failed":
                    found.add(MonitorFinding("journal_recovery_failed", "journal"))
                elif (phase == "prepared" and now >= record.start_deadline) or (
                    phase not in {"healthy", "restored_healthy", "recovery_failed"}
                    and now >= record.recovery_deadline
                ):
                    found.add(MonitorFinding("journal_deadline", "journal"))
        except Exception:
            found.add(MonitorFinding("journal_unavailable", "journal"))
        return tuple(sorted(found, key=lambda item: item.key))

    async def _send(self, finding: MonitorFinding, request_id: str, state: str) -> None:
        try:
            await self.bridge.send_alert(
                request_id=request_id,
                kind=finding.kind,
                service=finding.service,
                state=state,
            )
        except Exception:
            raise WatchdogError() from None


def _finding_from_key(key: str) -> MonitorFinding:
    try:
        service, kind = key.split(":", 1)
        if service not in _ALLOWED_SERVICES or kind not in _ALLOWED_KINDS:
            raise ValueError
        return MonitorFinding(kind, service)
    except Exception:
        raise WatchdogError() from None


async def _read_credential(path: Path) -> str:
    raw = _root_file(path, expected_uid=0, maximum=_MAX_SECRET_BYTES)
    try:
        value = raw.decode("ascii", errors="strict").strip()
        if re.fullmatch(r"[A-Za-z0-9_.-]{32,4096}", value) is None:
            raise ValueError
        return value
    except Exception:
        raise WatchdogError("watchdog_invalid_configuration") from None


async def build_watchdog(
    config: WatchdogConfig,
    *,
    systemd_runner=None,
    metrics=None,
    bridge_transport=None,
    host_transport=None,
) -> tuple[WatchdogMonitor, WatchdogBridgeClient, ProtectedHostClient]:
    host_token = await _read_credential(config.host_credential_file)
    bridge_token = await _read_credential(config.bridge_credential_file)
    host = ProtectedHostClient(
        socket_path=config.host_socket,
        credential=host_token,
        transport=host_transport,
    )
    bridge = WatchdogBridgeClient(
        socket_path=config.bridge_socket,
        credential=bridge_token,
        transport=bridge_transport,
    )
    monitor = WatchdogMonitor(
        config,
        systemd=SystemdProbe(systemd_runner),
        metrics=metrics or ProcMetrics(),
        host=host,
        journal=HostJournal(config.journal_path),
        store=WatchdogStateStore(config.state_path, owner_uid=0),
        bridge=bridge,
    )
    return monitor, bridge, host


async def serve(monitor: WatchdogMonitor, *, interval: int) -> None:
    while True:
        await monitor.run_once()
        await asyncio.sleep(interval)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="chatcore-release-watchdog")
    parser.add_argument("--config", default=str(CONFIG_PATH))
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check-config", action="store_true")
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--serve", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        config = load_watchdog_config(args.config)
        if args.check_config:
            print("configuration valid")
            return 0
        if os.geteuid() != 0:
            raise WatchdogError("watchdog_invalid_configuration")

        async def run() -> int:
            monitor, bridge, host = await build_watchdog(config)
            try:
                if args.once:
                    report = await monitor.run_once(dry_run=True)
                    print(
                        json.dumps(
                            {
                                "healthy": report.healthy,
                                "findings": [finding.key for finding in report.findings],
                                "notifications_pending": False,
                            },
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                    )
                    return 0 if report.healthy else 1
                await serve(monitor, interval=config.poll_interval_seconds)
                return 0
            finally:
                await bridge.close()

        return asyncio.run(run())
    except WatchdogError as exc:
        print(exc.code, file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 0
    except Exception:
        print("watchdog_unavailable", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
