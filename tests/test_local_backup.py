import json
import os
import shlex
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from ops import backup
from ops.health import backup_fresh


def test_health_service_entrypoint_without_pythonpath_or_secrets(tmp_path):
    root = Path(__file__).resolve().parents[1]
    unit = (root / "ops/antiscam-health.service").read_text(encoding="utf-8")
    command = next(
        line.removeprefix("ExecStart=")
        for line in unit.splitlines()
        if line.startswith("ExecStart=")
    )
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR"}
    }
    environment["BACKUP_DIRECTORY"] = str(tmp_path)
    result = subprocess.run(
        [sys.executable, *shlex.split(command)[1:]],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert (
        result.returncode == 1
    )  # Missing configuration is unhealthy, not an import crash.
    report = json.loads(result.stdout)
    assert report["database"] is False and report["backup"] is False
    assert "Traceback" not in result.stderr


def pair(root, name, timestamp=None):
    (root / name).write_bytes(b"fixture")
    data = {
        "archive": name,
        "archive_bytes": 7,
        "snapshot_started_at": (timestamp or datetime.now(timezone.utc)).isoformat(),
    }
    (root / (name + ".json")).write_text(json.dumps(data), encoding="utf-8")
    return data


def test_local_rotation_preserves_seven_pairs_and_preupdate_unrelated_incomplete(
    tmp_path,
):
    names = [f"antiscam-202611{day:02d}T030000Z.dump" for day in range(1, 11)]
    for name in names:
        pair(tmp_path, name)
    protected = [
        "preupdate-20261101T020000Z.dump",
        "manual.dump",
        "antiscam-20261301T030000Z.dump",
    ]
    for name in protected:
        pair(tmp_path, name)
    orphan = tmp_path / "antiscam-20261001T030000Z.dump"
    orphan.write_bytes(b"incomplete")
    backup.prune_local(tmp_path, 7)
    for name in names[:3]:
        assert not (tmp_path / name).exists()
        assert not (tmp_path / (name + ".json")).exists()
    assert all((tmp_path / n).exists() for n in names[3:] + protected)
    assert orphan.exists()


@pytest.mark.parametrize("failure", ["pg_dump", "pg_restore", None])
def test_failed_dump_never_rotates_or_updates_success(tmp_path, monkeypatch, failure):
    previous = pair(tmp_path, "antiscam-20260101T030000Z.dump")
    marker = tmp_path / "last-success.json"
    marker.write_text(json.dumps(previous), encoding="utf-8")

    @contextmanager
    def connect(**kwargs):
        yield SimpleNamespace(
            execute=lambda _: SimpleNamespace(fetchone=lambda: ["snapshot"])
        )

    def run(args, **kwargs):
        assert args[0] in {"pg_dump", "pg_restore"}  # No age or rclone in local mode.
        if args[0] == failure:
            raise RuntimeError("simulated failure")
        if args[0] == "pg_dump":
            Path(args[-1]).write_bytes(b"new dump")

    monkeypatch.setattr(backup.psycopg, "connect", connect)
    monkeypatch.setattr(backup, "run", run)
    monkeypatch.setattr(
        backup,
        "database_manifest",
        lambda _: {"users": {"rows": 1, "sha256": "fixture"}},
    )
    if failure:
        with pytest.raises(RuntimeError):
            backup.local_backup(tmp_path, "fixture", keep=1)
        assert json.loads(marker.read_text()) == previous
        assert (tmp_path / previous["archive"]).exists()
    else:
        status = backup.local_backup(tmp_path, "fixture", keep=1)
        assert status["storage"] == "local"
        assert status["dump_sha256"] == backup.checksum(tmp_path / status["archive"])
        assert not (tmp_path / previous["archive"]).exists()
        pinned = backup.local_backup(tmp_path, "fixture", keep=1, before_update=True)
        assert pinned["archive"].startswith("preupdate-")
        assert (tmp_path / status["archive"]).exists()


def test_daily_backup_health_checks_age_and_actual_file(tmp_path):
    moment = datetime(2026, 11, 14, 12, tzinfo=timezone.utc)
    data = pair(
        tmp_path, "antiscam-20261113T120000Z.dump", moment - timedelta(hours=24)
    )
    marker = tmp_path / "last-success.json"
    marker.write_text(json.dumps(data), encoding="utf-8")
    assert backup_fresh(tmp_path, moment=moment)
    assert not backup_fresh(tmp_path, moment=moment + timedelta(hours=3))
    assert not backup_fresh(tmp_path, moment=moment - timedelta(days=2))
    (tmp_path / data["archive"]).unlink()
    assert not backup_fresh(tmp_path, moment=moment)
    data["archive"] = "../elsewhere.dump"
    marker.write_text(json.dumps(data), encoding="utf-8")
    assert not backup_fresh(tmp_path, moment=moment)
