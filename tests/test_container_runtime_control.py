"""Receiver guards must fail closed before starting either deployment."""

import os
from unittest.mock import Mock

import pytest

from ops.container import runtime_control as c

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Linux host controller")


@pytest.mark.parametrize(
    "state,pid,allowed",
    [
        ("inactive", "0", True),
        ("failed", "0", True),
        ("active", "12412", False),
        ("activating", "0", False),
        ("deactivating", "12412", False),
        ("failed", "12412", False),
    ],
)
def test_legacy_state_guard(monkeypatch, state, pid, allowed):
    monkeypatch.setattr(c, "output", lambda args: f"MainPID={pid}\nActiveState={state}")
    if allowed:
        c.legacy_stopped()
    else:
        with pytest.raises(c.GuardError):
            c.legacy_stopped()


def test_gate_missing_denies_before_any_host_command(monkeypatch, tmp_path):
    check_gate = c.gate_ready

    def missing():
        check_gate(tmp_path / "absent")

    monkeypatch.setattr(c, "gate_ready", missing)
    command = Mock(side_effect=AssertionError("Host command before approval"))
    monkeypatch.setattr(c, "output", command)
    with pytest.raises(c.GuardError):
        c.start_guard()
    command.assert_not_called()


def test_activation_marker_contract(tmp_path):
    p = tmp_path / "gate"
    with pytest.raises(c.GuardError):
        c.gate_ready(p)
    p.write_bytes(b"production-v2-approved\n")
    p.chmod(0o600)
    if p.stat().st_uid == 0:
        c.gate_ready(p)
    p.chmod(0o644)
    with pytest.raises(c.GuardError):
        c.gate_ready(p)
    p.chmod(0o600)
    p.write_bytes(b"wrong")
    with pytest.raises(c.GuardError):
        c.gate_ready(p)
    link = tmp_path / "link"
    link.symlink_to(p)
    with pytest.raises(c.GuardError):
        c.gate_ready(link)


@pytest.mark.parametrize(
    "healthy,running", [(False, False), (True, True), (True, False)]
)
def test_v2_guards_database_and_duplicate(monkeypatch, healthy, running):
    monkeypatch.setattr(c, "gate_ready", lambda: None)
    monkeypatch.setattr(c, "legacy_stopped", lambda: None)
    monkeypatch.setattr(c, "output", lambda args: "healthy" if healthy else "starting")
    monkeypatch.setattr(c, "running_container", lambda name: running)
    if healthy and not running:
        c.start_guard()
    else:
        with pytest.raises(c.GuardError):
            c.start_guard()


def test_docker_failure_is_not_absent_container(monkeypatch):
    def failed(args):
        raise c.GuardError("Unavailable daemon")

    monkeypatch.setattr(c, "output", failed)
    with pytest.raises(c.GuardError):
        c.running_container(c.BOT)


def test_legacy_guard_respects_marker_and_v2(monkeypatch, tmp_path):
    gate = tmp_path / "gate"
    monkeypatch.setattr(c, "GATE", gate)
    monkeypatch.setattr(c, "running_container", lambda name: False)
    c.legacy_guard()
    gate.symlink_to(tmp_path / "missing")
    with pytest.raises(c.GuardError):
        c.legacy_guard()
    gate.unlink()
    monkeypatch.setattr(c, "running_container", lambda name: True)
    with pytest.raises(c.GuardError):
        c.legacy_guard()


def test_operations_require_gate_before_container_launch(monkeypatch):
    monkeypatch.setattr(c.os, "geteuid", lambda: 0)

    def denied():
        raise c.GuardError("Not activated")

    monkeypatch.setattr(c, "gate_ready", denied)
    call = Mock(side_effect=AssertionError("Unexpected container launch"))
    monkeypatch.setattr(c.subprocess, "call", call)
    for action in ("backup", "health"):
        with pytest.raises(c.GuardError):
            c.main(action)
    call.assert_not_called()
