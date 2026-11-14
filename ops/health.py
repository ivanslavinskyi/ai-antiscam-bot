"""Independent process check: receiver, queue backlog, DB and backup freshness."""

import json
from pathlib import Path
import os
import shutil
from datetime import datetime, timezone
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from ops.backup import NAME, LOCAL_NAME, PREUPDATE_NAME


def backup_fresh(root, max_age_seconds=26 * 3600, moment=None):
    try:
        status = json.loads((root / "last-success.json").read_text(encoding="utf-8"))
        stamp = datetime.fromisoformat(status["snapshot_started_at"])
        age = ((moment or datetime.now(timezone.utc)) - stamp).total_seconds()
        name = status["archive"]
        if not isinstance(name, str) or not any(
            pattern.fullmatch(name) for pattern in (NAME, LOCAL_NAME, PREUPDATE_NAME)
        ):
            return False
        archive = root / name
        size = status.get("archive_bytes", status.get("encrypted_bytes", 0))
        return (
            max_age_seconds > 0
            and 0 <= age < max_age_seconds
            and size > 0
            and archive.is_file()
            and not archive.is_symlink()
            and archive.stat().st_size == size
            and archive.with_name(name + ".json").is_file()
        )
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False


def main():
    checks = {}
    try:
        space = shutil.disk_usage(
            os.environ.get("BACKUP_DIRECTORY", "/var/lib/antiscam/backups")
        )
        checks["disk_space"] = space.free >= 1024**3 and space.free / space.total >= 0.1
    except OSError:
        checks["disk_space"] = False
    try:
        url = make_url(os.environ["DB_URL"]).set(drivername="postgresql+psycopg")
        engine = create_engine(
            url, hide_parameters=True, connect_args={"connect_timeout": 5}
        )
        with engine.connect() as conn:
            age = conn.execute(
                text(
                    "SELECT extract(epoch from now()-updated_at) FROM system_state WHERE key='heartbeat:receiver'"
                )
            ).scalar()
            checks["receiver"] = age is not None and age < 120
            backlog = conn.execute(
                text(
                    "SELECT extract(epoch from now()-min(due_at)) FROM jobs WHERE state='pending' AND due_at<=now()"
                )
            ).scalar()
            checks["queue"] = backlog is None or backlog < 300
            checks["database"] = True
        engine.dispose()
    except Exception:
        checks["database"] = False
    try:
        checks["backup"] = backup_fresh(
            Path(os.environ.get("BACKUP_DIRECTORY", "/var/lib/antiscam/backups")),
            max_age_seconds=int(os.environ.get("BACKUP_MAX_AGE_SECONDS", "93600")),
        )
    except Exception:
        checks["backup"] = False
    success_url = os.environ.get("HEALTHCHECK_SUCCESS_URL")
    if all(checks.values()) and success_url:
        import httpx

        try:
            response = httpx.get(success_url, timeout=5, follow_redirects=False)
            checks["external_heartbeat"] = response.is_success
        except Exception:
            checks["external_heartbeat"] = False
    print(json.dumps(checks))
    raise SystemExit(0 if all(checks.values()) else 1)


if __name__ == "__main__":
    main()
