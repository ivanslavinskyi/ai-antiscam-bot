import json
import os
import stat
import sys
from urllib.parse import urlparse

import pytest

from ops.container import render_database_config as config
from ops.container.activate_credentials import ROLES, SetupError


@pytest.fixture
def private(tmp_path):
    if sys.platform != "linux" or os.geteuid() != 0:
        pytest.skip("Run in the Linux root test container")
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    document = {
        "version": 1,
        "database": "antiscam_v2",
        "passwords": {role: chr(65 + i) * 64 for i, role in enumerate(ROLES)},
    }
    path = directory / "database-credentials.json"
    path.write_text(json.dumps(document), encoding="ascii")
    path.chmod(0o600)
    return directory, document


def test_configs_separate_roles_and_preserve_source(private):
    directory, document = private
    before = (directory / "database-credentials.json").read_bytes()
    result = config.render(directory)
    target = directory / "service-config"
    assert result["result"] == "created"
    assert target.stat().st_uid == 0
    assert stat.S_IMODE(target.stat().st_mode) == 0o700
    assert {p.name for p in target.iterdir()} == {
        "runtime-db.env",
        "migration.env",
        "health.env",
        "backup.env",
        "pg_service.conf",
        "pgpass",
    }
    for name, role in [
        ("runtime-db.env", ROLES[1]),
        ("migration.env", ROLES[0]),
        ("health.env", ROLES[3]),
    ]:
        text = (target / name).read_text()
        url = urlparse(text.splitlines()[0].split("=", 1)[1])
        assert (url.hostname, url.port, url.path) == ("db", 5432, "/antiscam_v2")
        assert (url.username, url.password) == (role, document["passwords"][role])
        for other, secret in document["passwords"].items():
            assert (secret in text) == (other == role)
    backup = (target / "backup.env").read_text()
    assert "BACKUP_KEEP_COUNT=7\n" in backup
    assert "PGSERVICEFILE=/run/antiscam/pg_service.conf\n" in backup
    assert "PGPASSFILE=/run/antiscam/pgpass\n" in backup
    assert (target / "pgpass").read_text() == (
        "db:5432:antiscam_v2:antiscam_backup:" + document["passwords"][ROLES[2]] + "\n"
    )
    for path in target.iterdir():
        info = path.stat()
        owner = 10001 if path.name in ("pgpass", "pg_service.conf") else 0
        assert (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) == (
            owner,
            owner,
            0o600,
        )
        assert "TOKEN" not in path.read_text()
    for secret in document["passwords"].values():
        assert secret not in json.dumps(result)
    assert (directory / "database-credentials.json").read_bytes() == before
    saved = {p.name: p.read_bytes() for p in target.iterdir()}
    with pytest.raises(SetupError, match="already exists"):
        config.render(directory)
    assert saved == {p.name: p.read_bytes() for p in target.iterdir()}


def test_invalid_source_never_publishes(private):
    directory, _ = private
    (directory / "database-credentials.json").chmod(0o644)
    with pytest.raises(SetupError):
        config.render(directory)
    assert not (directory / "service-config").exists()


def test_refuses_dangling_target_symlink(private):
    directory, _ = private
    target = directory / "service-config"
    target.symlink_to(directory / "missing", target_is_directory=True)
    with pytest.raises(SetupError, match="already exists"):
        config.render(directory)
    assert target.is_symlink()
    assert not (directory / "missing").exists()


def test_partial_write_never_publishes(private, monkeypatch):
    directory, _ = private
    monkeypatch.setattr(
        config.os, "fchown", lambda *a: (_ for _ in ()).throw(OSError("failure"))
    )
    with pytest.raises(OSError):
        config.render(directory)
    assert not (directory / "service-config").exists()
    for stage in directory.glob(".service-config-*"):
        assert stat.S_IMODE(stage.stat().st_mode) == 0o700
        assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in stage.iterdir())


def test_unexpected_errors_do_not_leak_secrets(monkeypatch, capsys):
    def fail():
        raise ValueError("secret-value-that-must-not-be-printed")

    monkeypatch.setattr(config, "render", fail)
    assert config.main() == 1
    output = capsys.readouterr().out
    assert "secret-value" not in output
    assert json.loads(output) == {"result": "failed", "error_type": "ValueError"}
