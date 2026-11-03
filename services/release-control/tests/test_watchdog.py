from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import chatcore_release_control.watchdog as watchdog
import httpx
import pytest
from chatcore_release_control.watchdog import (
    FIXED_DISK_PATHS,
    FIXED_UNITS,
    MetricsSnapshot,
    ProcMetrics,
    SystemdProbe,
    WatchdogConfig,
    WatchdogError,
    WatchdogMonitor,
    WatchdogStateStore,
    load_watchdog_config,
)


def _config(tmp_path: Path) -> WatchdogConfig:
    return WatchdogConfig(
        target="production",
        state_path=tmp_path / "watchdog-state.json",
        journal_path=tmp_path / "journal.json",
        host_socket=tmp_path / "host.sock",
        host_credential_file=tmp_path / "host-token",
        bridge_socket=tmp_path / "bridge.sock",
        bridge_credential_file=tmp_path / "bridge-token",
        units=FIXED_UNITS,
        max_cpu_busy_percent=85,
        max_load_per_cpu_milli=1500,
        min_available_memory_bytes=2_000_000,
        disk_free_bytes=tuple((path, 1_000_000) for path in FIXED_DISK_PATHS),
        sample_interval_seconds=1,
        poll_interval_seconds=30,
    )


class _Systemd:
    def __init__(self, values=None, fail=()):
        self.values = values or {}
        self.fail = set(fail)
        self.calls = []

    def active_state(self, unit):
        self.calls.append(unit)
        if unit in self.fail:
            raise RuntimeError("synthetic systemctl output")
        return self.values.get(unit, "active")


class _Metrics:
    def __init__(self, result=None, fail=False):
        self.result = result or MetricsSnapshot(10, 100, 8_000_000, {})
        self.fail = fail
        self.calls = 0

    def read(self, config):
        self.calls += 1
        if self.fail:
            raise OSError("synthetic proc read failure")
        return replace(self.result, disk_free_bytes=dict(config.disk_free_bytes))


class _Host:
    def __init__(self, *, healthy=True, fail=False):
        self.healthy, self.fail = healthy, fail
        self.calls = []

    async def current_state(self, target):
        self.calls.append(target)
        if self.fail:
            raise RuntimeError("synthetic host socket failure")
        return SimpleNamespace(healthy=self.healthy)


class _Journal:
    def __init__(self, record=None, fail=False):
        self.record, self.fail = record, fail
        self.calls = 0

    def current_record(self):
        self.calls += 1
        if self.fail:
            raise OSError("synthetic journal failure")
        return self.record


class _Bridge:
    def __init__(self):
        self.calls = []
        self.fail = False

    async def send_alert(self, *, request_id, kind, service, state):
        self.calls.append((request_id, kind, service, state))
        if self.fail:
            raise RuntimeError("synthetic lost bridge reply")


def test_watchdog_config_requires_fixed_units_explicit_thresholds_and_root_paths(
    tmp_path, monkeypatch
):
    config = _config(tmp_path)
    assert config.units == FIXED_UNITS
    with pytest.raises(WatchdogError):
        replace(config, max_cpu_busy_percent=0)
    with pytest.raises(WatchdogError):
        replace(config, units=(("bridge", "arbitrary.service"),))

    config_dir = tmp_path / "etc"
    config_dir.mkdir(mode=0o700)
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o700)
    run_dir = tmp_path / "run"
    run_dir.mkdir(mode=0o700)
    host_token = config_dir / "host.token"
    bridge_token = config_dir / "bridge.token"
    host_token.write_text("h" * 32)
    bridge_token.write_text("b" * 32)
    host_token.chmod(0o600)
    bridge_token.chmod(0o600)
    fixed_disk_paths = (str(tmp_path), str(state_dir), str(run_dir))
    monkeypatch.setattr(watchdog, "CONFIG_PATH", config_dir / "watchdog.json")
    monkeypatch.setattr(watchdog, "STATE_PATH", state_dir / "watchdog-state.json")
    monkeypatch.setattr(watchdog, "JOURNAL_PATH", state_dir / "host-journal.json")
    monkeypatch.setattr(watchdog, "HOST_SOCKET_PATH", run_dir / "host.sock")
    monkeypatch.setattr(watchdog, "BRIDGE_SOCKET_PATH", run_dir / "bridge.sock")
    monkeypatch.setattr(watchdog, "HOST_CREDENTIAL_PATH", host_token)
    monkeypatch.setattr(watchdog, "BRIDGE_CREDENTIAL_PATH", bridge_token)
    monkeypatch.setattr(watchdog, "_FIXED_DISK_PATHS", fixed_disk_paths)
    path = watchdog.CONFIG_PATH
    value = {
        "version": 1,
        "target": "production",
        "state_path": str(watchdog.STATE_PATH),
        "journal_path": str(watchdog.JOURNAL_PATH),
        "host_socket": str(watchdog.HOST_SOCKET_PATH),
        "host_credential_file": str(watchdog.HOST_CREDENTIAL_PATH),
        "bridge_socket": str(watchdog.BRIDGE_SOCKET_PATH),
        "bridge_credential_file": str(watchdog.BRIDGE_CREDENTIAL_PATH),
        "units": dict(FIXED_UNITS),
        "max_cpu_busy_percent": 85,
        "max_load_per_cpu_milli": 1500,
        "min_available_memory_bytes": 2_000_000,
        "disk_free_bytes": {item: 1_000_000 for item in fixed_disk_paths},
        "sample_interval_seconds": 1,
        "poll_interval_seconds": 30,
    }
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    loaded = load_watchdog_config(path, expected_uid=os.getuid())
    assert loaded.disk_free_bytes == tuple((item, 1_000_000) for item in fixed_disk_paths)
    del value["max_load_per_cpu_milli"]
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    with pytest.raises(WatchdogError):
        load_watchdog_config(path, expected_uid=os.getuid())


def test_watchdog_state_store_persists_durable_incident_ids_and_transition_ids(tmp_path):
    state_path = tmp_path / "state.json"
    store = WatchdogStateStore(state_path, owner_uid=os.getuid())
    first = store.prepare_firing("metrics:cpu_pressure")
    reopened = WatchdogStateStore(state_path, owner_uid=os.getuid())
    assert reopened.prepare_firing("metrics:cpu_pressure") == first
    assert state_path.stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "state.json.lock").stat().st_mode & 0o777 == 0o600
    reopened.mark_sent("metrics:cpu_pressure", first["incident_id"], "firing")
    resolved = reopened.prepare_resolved("metrics:cpu_pressure", first["incident_id"])
    assert resolved["incident_id"] == first["incident_id"]
    assert resolved["resolved_request_id"]
    assert reopened.prepare_resolved("metrics:cpu_pressure", first["incident_id"]) == resolved


def test_watchdog_concurrent_starts_share_one_durable_incident_and_request_id(tmp_path):
    state_path = tmp_path / "state.json"

    def prepare(_):
        store = WatchdogStateStore(state_path, owner_uid=os.getuid())
        return store.prepare_firing("metrics:disk_pressure")

    with ThreadPoolExecutor(max_workers=6) as workers:
        rows = tuple(workers.map(prepare, range(12)))
    assert len({row["incident_id"] for row in rows}) == 1
    assert len({row["firing_request_id"] for row in rows}) == 1


def test_watchdog_state_store_rejects_symlink_and_malformed_state(tmp_path):
    target = tmp_path / "actual.json"
    target.write_text("{}")
    target.chmod(0o600)
    link = tmp_path / "state.json"
    link.symlink_to(target)
    with pytest.raises(WatchdogError):
        WatchdogStateStore(link, owner_uid=os.getuid()).prepare_firing("metrics:cpu_pressure")


async def test_watchdog_observes_fixed_units_metrics_host_and_journal_without_mutation(tmp_path):
    config = _config(tmp_path)
    systemd = _Systemd()
    metrics = _Metrics()
    host = _Host()
    journal = _Journal()
    bridge = _Bridge()
    store = WatchdogStateStore(config.state_path, owner_uid=os.getuid())
    monitor = WatchdogMonitor(
        config,
        systemd=systemd,
        metrics=metrics,
        host=host,
        journal=journal,
        store=store,
        bridge=bridge,
        now=lambda: 100,
    )

    report = await monitor.run_once(dry_run=True)

    assert report.healthy and report.findings == ()
    assert systemd.calls == [unit for _, unit in FIXED_UNITS]
    assert metrics.calls == 1 and host.calls == ["production"] and journal.calls == 1
    assert bridge.calls == [] and not config.state_path.exists()


async def test_watchdog_alerts_only_changes_and_reuses_request_id_after_lost_reply(tmp_path):
    config = _config(tmp_path)
    systemd = _Systemd({unit: "inactive" for _, unit in FIXED_UNITS})
    metrics = _Metrics(MetricsSnapshot(95, 2500, 1000, {}))
    host = _Host(fail=True)
    journal = _Journal(fail=True)
    bridge = _Bridge()
    bridge.fail = True
    monitor = WatchdogMonitor(
        config,
        systemd=systemd,
        metrics=metrics,
        host=host,
        journal=journal,
        store=WatchdogStateStore(config.state_path, owner_uid=os.getuid()),
        bridge=bridge,
        now=lambda: 100,
    )

    first = await monitor.run_once()
    assert not first.healthy
    assert {finding.kind for finding in first.findings} >= {
        "service_inactive",
        "cpu_pressure",
        "load_pressure",
        "memory_pressure",
        "host_state_unavailable",
        "journal_unavailable",
    }
    first_calls = tuple(bridge.calls)
    second = await monitor.run_once()
    assert second.notifications_pending
    assert tuple(call[0] for call in bridge.calls[: len(first_calls)]) == tuple(
        call[0] for call in first_calls
    )
    firing_keys = {
        f"{service}:{kind}" for _, kind, service, state in bridge.calls if state == "firing"
    }
    assert firing_keys == {finding.key for finding in first.findings}


async def test_watchdog_resolves_cleared_incident_once_and_new_firing_gets_new_id(tmp_path):
    config = _config(tmp_path)
    systemd = _Systemd({unit: "inactive" for _, unit in FIXED_UNITS})
    metrics = _Metrics()
    host, journal, bridge = _Host(), _Journal(), _Bridge()
    monitor = WatchdogMonitor(
        config,
        systemd=systemd,
        metrics=metrics,
        host=host,
        journal=journal,
        store=WatchdogStateStore(config.state_path, owner_uid=os.getuid()),
        bridge=bridge,
        now=lambda: 100,
    )
    await monitor.run_once()
    systemd.values = {unit: "active" for _, unit in FIXED_UNITS}
    await monitor.run_once()
    resolved = [call for call in bridge.calls if call[3] == "resolved"]
    assert len(resolved) == len(FIXED_UNITS)
    await monitor.run_once()
    assert len([call for call in bridge.calls if call[3] == "resolved"]) == len(FIXED_UNITS)
    systemd.values[dict(FIXED_UNITS)["bridge"]] = "inactive"
    await monitor.run_once()
    firings = [
        call
        for call in bridge.calls
        if call[1:3] == ("service_inactive", "bridge") and call[3] == "firing"
    ]
    assert len(firings) == 2 and firings[0][0] != firings[1][0]


async def test_watchdog_reports_failed_host_health_and_expired_journal_as_typed_findings(tmp_path):
    config = _config(tmp_path)
    expired = SimpleNamespace(phase="prepared", start_deadline=99, recovery_deadline=200)
    monitor = WatchdogMonitor(
        config,
        systemd=_Systemd(),
        metrics=_Metrics(),
        host=_Host(healthy=False),
        journal=_Journal(expired),
        store=WatchdogStateStore(config.state_path, owner_uid=os.getuid()),
        bridge=_Bridge(),
        now=lambda: 100,
    )
    report = await monitor.run_once(dry_run=True)
    assert {finding.kind for finding in report.findings} == {
        "host_unhealthy",
        "journal_deadline",
    }


def test_systemd_probe_runs_only_fixed_read_command_and_bounds_output():
    calls = []

    def runner(args, *, timeout, output_limit):
        calls.append((args, timeout, output_limit))
        return b"active\n"

    probe = SystemdProbe(runner)
    assert probe.active_state(dict(FIXED_UNITS)["bridge"]) == "active"
    assert calls[0][0] == (
        "/usr/bin/systemctl",
        "show",
        "--property=ActiveState",
        "--value",
        dict(FIXED_UNITS)["bridge"],
    )
    with pytest.raises(WatchdogError):
        probe.active_state("arbitrary.service")


def test_proc_metrics_parses_bounded_cpu_load_memory_and_fixed_disk_paths(tmp_path):
    config = _config(tmp_path)
    cpu = iter(
        (
            b"cpu 100 0 50 500 50\ncpu0 1 0 1 1 0\ncpu1 1 0 1 1 0\n",
            b"cpu 110 0 55 580 55\ncpu0 2 0 2 2 0\ncpu1 2 0 2 2 0\n",
        )
    )

    def reader(path, maximum):
        assert len(path.as_posix()) <= maximum
        if path == Path("/proc/stat"):
            return next(cpu)
        if path == Path("/proc/loadavg"):
            return b"2.00 1.00 0.50 1/100 42\n"
        if path == Path("/proc/meminfo"):
            return b"MemTotal: 8192 kB\nMemAvailable: 4096 kB\n"
        raise AssertionError(path)

    metrics = ProcMetrics(
        reader=reader,
        sleep=lambda _seconds: None,
        statvfs=lambda _path: SimpleNamespace(f_bavail=200, f_frsize=4096),
    ).read(config)
    assert metrics == MetricsSnapshot(
        15,
        1000,
        4_194_304,
        {path: 819_200 for path, _ in config.disk_free_bytes},
    )


async def test_watchdog_bridge_client_uses_fixed_uds_route_and_bounded_schema():
    from chatcore_release_control.watchdog import WatchdogBridgeClient

    incident = "e2222222-2222-4222-8222-222222222222"
    calls = []

    def send(request):
        calls.append(request)
        assert request.url.path == "/v1/broker/alerts"
        assert request.headers["authorization"] == "Bearer " + "b" * 32
        assert json.loads(request.content) == {
            "request_id": incident,
            "kind": "disk_pressure",
            "service": "metrics",
            "state": "firing",
        }
        return httpx.Response(
            200,
            json={"request_id": incident, "state": "firing", "message_id": 901},
        )

    client = WatchdogBridgeClient(
        socket_path=Path("/private/bridge.sock"),
        credential="b" * 32,
        transport=httpx.MockTransport(send),
    )
    await client.send_alert(
        request_id=incident,
        kind="disk_pressure",
        service="metrics",
        state="firing",
    )
    assert len(calls) == 1 and "b" * 32 not in repr(client)
    await client.close()


def test_inactive_watchdog_units_are_fixed_private_and_nonremediating():
    service = (
        Path("services/release-control/src/chatcore_release_control")
        / "templates"
        / "p10-watchdog.service.template"
    ).read_text()
    timer = (
        Path("services/release-control/src/chatcore_release_control")
        / "templates"
        / "p10-watchdog.timer.template"
    ).read_text()
    assert "--serve" in service
    assert "/etc/chatcore-release-control/watchdog.json" in service
    assert "User=root" in service and "RestrictAddressFamilies=AF_UNIX" in service
    assert "systemctl restart" not in service and "docker" not in service.lower()
    assert "OnBootSec=60s" in timer
    assert "chatcore-release-watchdog.service" in timer
    assert not Path(".github/workflows/p10-watchdog.service").exists()
