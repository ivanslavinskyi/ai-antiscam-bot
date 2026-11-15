"""Publish separate container DB configs using the existing saved credentials."""

import json
import os
from pathlib import Path
import stat
import sys
import tempfile

from ops.container.activate_credentials import SetupError, read_credentials


def config_files(passwords):
    def url(role):
        return f"postgresql+psycopg://{role}:{passwords[role]}@db:5432/antiscam_v2"

    return {
        "runtime-db.env": (f"DB_URL={url('antiscam_runtime')}\n", 0),
        "migration.env": (
            f"ANTISCAM_MIGRATION_DATABASE_URL={url('antiscam_migrator')}\n",
            0,
        ),
        "health.env": (
            f"DB_URL={url('antiscam_health')}\n"
            "BACKUP_DIRECTORY=/var/lib/antiscam/backups\n"
            "BACKUP_MAX_AGE_SECONDS=93600\n",
            0,
        ),
        "backup.env": (
            "PGSERVICE=antiscam_backup\n"
            "PGSERVICEFILE=/run/antiscam/pg_service.conf\n"
            "PGPASSFILE=/run/antiscam/pgpass\n"
            "BACKUP_DIRECTORY=/var/lib/antiscam/backups\n"
            "BACKUP_KEEP_COUNT=7\n",
            0,
        ),
        "pg_service.conf": (
            "[antiscam_backup]\nhost=db\nport=5432\n"
            "dbname=antiscam_v2\nuser=antiscam_backup\n",
            10001,
        ),
        "pgpass": (
            f"db:5432:antiscam_v2:antiscam_backup:{passwords['antiscam_backup']}\n",
            10001,
        ),
    }


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def render(config_dir=Path("/etc/antiscam-v2")):
    import fcntl

    if sys.platform != "linux" or os.geteuid() != 0:
        raise SetupError("Run on Linux as root")
    config_dir = Path(config_dir)
    passwords = read_credentials(config_dir / "database-credentials.json")
    lock = os.open(
        config_dir / ".service-config.lock",
        os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
        0o600,
    )
    with os.fdopen(lock, "r+") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != 0
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise SetupError("Unsafe config lock file")
        fcntl.flock(stream, fcntl.LOCK_EX)
        target = config_dir / "service-config"
        if os.path.lexists(target):
            raise SetupError("Service config already exists; no files changed")
        # A failed staging directory is retained under the protected parent.
        # Publication is atomic; an incomplete set is never used by services.
        stage = Path(tempfile.mkdtemp(prefix=".service-config-", dir=config_dir))
        records = []
        for name, (content, owner) in config_files(passwords).items():
            fd = os.open(stage / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="ascii", newline="\n") as output:
                os.fchmod(output.fileno(), 0o600)
                output.write(content)
                output.flush()
                os.fchown(output.fileno(), owner, owner)
                os.fsync(output.fileno())
            records.append({"file": name, "uid": owner, "gid": owner, "mode": "0o600"})
        sync_directory(stage)
        stage.rename(target)
        sync_directory(config_dir)
        return {"result": "created", "directory": str(target), "files": records}


def main():
    try:
        print(json.dumps(render()))
    except SetupError as exc:
        print(json.dumps({"result": "failed", "reason": str(exc)}))
        return 1
    except Exception as exc:
        # Filesystem errors can contain sensitive paths/content; report type only.
        print(json.dumps({"result": "failed", "error_type": type(exc).__name__}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
