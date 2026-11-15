#!/usr/bin/python3
"""Host systemd controller. Never reads or prints credential contents."""

import json
import os
from pathlib import Path
import stat
import subprocess
import sys

GATE = Path("/etc/antiscam-v2/ALLOW_V2_START")
COMPOSE = [
    "/usr/bin/docker",
    "compose",
    "--project-name",
    "antiscam-v2-app",
    "--env-file",
    "/dev/null",
    "--file",
    "/opt/antiscam-bot/deploy/compose.runtime.yaml",
]
BOT = "antiscam-v2-app-bot"


class GuardError(Exception):
    pass


def output(args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=20)
    if result.returncode:
        raise GuardError("Host state check failed; refusing action")
    return result.stdout.strip()


def gate_ready(path=GATE):
    try:
        s = path.lstat()
        if not (
            stat.S_ISREG(s.st_mode)
            and s.st_uid == s.st_gid == 0
            and stat.S_IMODE(s.st_mode) == 0o600
            and path.read_bytes() == b"production-v2-approved\n"
        ):
            raise GuardError("Invalid V2 activation marker")
    except FileNotFoundError:
        raise GuardError("V2 activation is not approved yet") from None


def legacy_stopped():
    properties = output(
        [
            "/usr/bin/systemctl",
            "show",
            "ai-antiscam-bot.service",
            "--property=ActiveState,MainPID",
        ]
    )
    state = dict(line.split("=", 1) for line in properties.splitlines())
    if (
        state.get("ActiveState") not in {"inactive", "failed"}
        or state.get("MainPID") != "0"
    ):
        raise GuardError("Legacy bot must be stopped before V2")


def running_container(name):
    # A daemon error is not evidence that a competing container is absent.
    names = output(["/usr/bin/docker", "ps", "--format", "{{.Names}}"])
    return name in names.splitlines()


def start_guard():
    gate_ready()
    legacy_stopped()
    if (
        output(
            [
                "/usr/bin/docker",
                "inspect",
                "antiscam-v2-db-1",
                "--format",
                "{{.State.Health.Status}}",
            ]
        )
        != "healthy"
    ):
        raise GuardError("V2 database is not healthy")
    if running_container(BOT):
        raise GuardError("V2 bot container is already running")


def legacy_guard():
    if os.path.lexists(GATE):
        raise GuardError("V2 activation marker blocks legacy startup")
    if running_container(BOT):
        raise GuardError("V2 bot must be stopped before legacy startup")


def stop_task(task):
    name = "antiscam-v2-" + task
    if running_container(name):
        project = output(
            [
                "/usr/bin/docker",
                "inspect",
                name,
                "--format",
                '{{index .Config.Labels "com.docker.compose.project"}}',
            ]
        )
        if project != "antiscam-v2-app":
            raise GuardError("Unexpected one-off container owner")
        return subprocess.call(["/usr/bin/docker", "stop", "--time", "30", name])
    return 0


def main(action):
    if os.geteuid() != 0:
        raise GuardError("Controller requires root; application runs as UID10001")
    # Do not let shell/manager environment override the installed deployment.
    for key in list(os.environ):
        if key.startswith(("ANTISCAM_V2_", "COMPOSE_", "DOCKER_")):
            os.environ.pop(key)
    os.environ["DOCKER_HOST"] = "unix:///var/run/docker.sock"
    if action == "guard":
        start_guard()
        return 0
    if action == "legacy-guard":
        legacy_guard()
        return 0
    if action == "stop":
        return subprocess.call(COMPOSE + ["stop", "--timeout", "45", "bot"])
    if action in {"stop-backup", "stop-health"}:
        return stop_task(action.removeprefix("stop-"))
    if action in {"backup", "health"}:
        gate_ready()
        return subprocess.call(
            COMPOSE
            + [
                "--profile",
                "operations",
                "run",
                "--rm",
                "--no-deps",
                "-T",
                "--name",
                "antiscam-v2-" + action,
                action,
            ]
        )
    if action != "start":
        raise GuardError("Unknown controller action")
    import fcntl

    fd = os.open(
        "/run/lock/antiscam-v2-receiver.lock",
        os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
        0o600,
    )
    with os.fdopen(fd, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        start_guard()
        return subprocess.call(
            COMPOSE
            + [
                "up",
                "--no-build",
                "--pull",
                "never",
                "--abort-on-container-exit",
                "--exit-code-from",
                "bot",
                "bot",
            ]
        )


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1] if len(sys.argv) == 2 else ""))
    except Exception as exc:
        print(
            json.dumps(
                {
                    "result": "refused",
                    "reason": str(exc)
                    if isinstance(exc, GuardError)
                    else type(exc).__name__,
                }
            )
        )
        raise SystemExit(1)
