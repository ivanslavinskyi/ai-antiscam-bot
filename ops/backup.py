"""Daily local custom-format backups; optional legacy offsite delivery.

Run on Linux under a dedicated account. Configuration is read from EnvironmentFile.
No bot process or Telegram/OpenAI credential is required.
"""

import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
import psycopg
from ops.db_manifest import database_manifest

NAME = re.compile(r"^antiscam-(\d{8}T\d{6}Z)\.dump\.age$")
LOCAL_NAME = re.compile(r"^antiscam-(\d{8}T\d{6}Z)\.dump$")
PREUPDATE_NAME = re.compile(r"^preupdate-(\d{8}T\d{6}Z)\.dump$")


def checksum(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def run(args, timeout=180, **kwargs):
    result = subprocess.run(
        args, check=False, timeout=timeout, capture_output=True, **kwargs
    )
    if result.returncode:
        # Tool stderr may contain credentials from a misconfigured connection string.
        raise RuntimeError(
            Path(args[0]).name + " failed with status " + str(result.returncode)
        )
    return result.stdout


def retention_keep(names, moment=None, daily=14, weekly=8):
    moment = moment or datetime.now(timezone.utc)
    dated = []
    for name in names:
        match = NAME.fullmatch(name)
        if match:
            dated.append(
                (
                    datetime.strptime(match[1], "%Y%m%dT%H%M%SZ").replace(
                        tzinfo=timezone.utc
                    ),
                    name,
                )
            )
    dated.sort(reverse=True)
    keep = set()
    days, weeks = set(), set()
    for stamp, name in dated:
        if stamp >= moment - timedelta(hours=24):
            keep.add(name)
        if stamp.date() not in days and len(days) < daily:
            days.add(stamp.date())
            keep.add(name)
        week = stamp.isocalendar()[:2]
        if week not in weeks and len(weeks) < weekly:
            weeks.add(week)
            keep.add(name)
    return keep


def prune_local(root, keep):
    """Only completed scheduled pairs are rotated; pre-update copies stay put."""
    if keep < 1:
        raise ValueError("Keep at least one scheduled backup")
    completed = []
    for path in root.iterdir():
        match = LOCAL_NAME.fullmatch(path.name)
        manifest = path.with_name(path.name + ".json")
        if (
            not match
            or path.is_symlink()
            or not path.is_file()
            or manifest.is_symlink()
        ):
            continue
        try:
            datetime.strptime(match[1], "%Y%m%dT%H%M%SZ")
            details = json.loads(manifest.read_text(encoding="utf-8"))
            if details.get("archive") == path.name:
                completed.append(path)
        except (OSError, ValueError, AttributeError):
            continue
    for path in sorted(completed, key=lambda p: p.name, reverse=True)[keep:]:
        path.unlink()
        path.with_name(path.name + ".json").unlink()


def local_backup(root, service, keep=7, before_update=False):
    """Caller holds the process lock and sets restrictive file permissions."""
    if keep < 1 or not re.fullmatch(r"[A-Za-z0-9_-]+", service):
        raise ValueError("Configure a PostgreSQL service and positive retention count")
    started = datetime.now(timezone.utc)
    prefix = "preupdate-" if before_update else "antiscam-"
    filename = prefix + started.strftime("%Y%m%dT%H%M%SZ") + ".dump"
    if (root / filename).exists() or (root / (filename + ".json")).exists():
        raise RuntimeError("Backup with this timestamp already exists; retry later")
    with tempfile.TemporaryDirectory(prefix="pending-", dir=root) as temporary:
        dump = Path(temporary) / filename
        with psycopg.connect(service=service) as source:
            source.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            source.execute("SET LOCAL timezone='UTC'")
            snapshot = source.execute("SELECT pg_export_snapshot()").fetchone()[0]
            run(
                [
                    "pg_dump",
                    "--dbname=service=" + service,
                    "--format=custom",
                    "--no-password",
                    "--snapshot",
                    snapshot,
                    "--file",
                    str(dump),
                ]
            )
            tables = database_manifest(source)
        run(["pg_restore", "--list", str(dump)], timeout=30)
        manifest = {
            "storage": "local",
            "snapshot_started_at": started.isoformat(),
            "archive": filename,
            "dump_sha256": checksum(dump),
            "archive_bytes": dump.stat().st_size,
            "tables": tables,
        }
        manifest_path = Path(temporary) / (filename + ".json")
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        dump.replace(root / filename)
        manifest_path.replace(root / manifest_path.name)
    # Never rotate old copies after a failed dump or validation.
    if not before_update:
        prune_local(root, keep)
    finished = datetime.now(timezone.utc)
    status = {
        **manifest,
        "local_verified_at": finished.isoformat(),
        "seconds": (finished - started).total_seconds(),
    }
    pending = root / "last-success.pending"
    pending.write_text(json.dumps(status, indent=2), encoding="utf-8")
    pending.replace(root / "last-success.json")
    return status


def main():
    import argparse
    import fcntl

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--before-update",
        action="store_true",
        help="Keep a separate pre-update copy outside daily rotation",
    )
    args = parser.parse_args()
    if os.environ.get("BACKUP_REMOTE", "").strip():
        if args.before_update:
            raise RuntimeError("Pre-update copies use local mode: unset BACKUP_REMOTE")
        return offsite_main()
    os.umask(0o077)
    configured = Path(os.environ.get("BACKUP_DIRECTORY", "/var/lib/antiscam/backups"))
    root = configured.resolve()
    if not configured.is_absolute() or len(root.parts) < 3:
        raise RuntimeError("Use a dedicated absolute backup directory")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / "backup.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        status = local_backup(
            root,
            os.environ.get("PGSERVICE", "antiscam_backup"),
            keep=int(os.environ.get("BACKUP_KEEP_COUNT", "7")),
            before_update=args.before_update,
        )
    print(
        json.dumps(
            {k: status[k] for k in ("storage", "archive", "archive_bytes", "seconds")}
        )
    )


def offsite_main():
    import fcntl

    os.umask(0o077)
    root = Path(
        os.environ.get("BACKUP_DIRECTORY", "/var/lib/antiscam/backups")
    ).resolve()
    if not root.is_absolute() or len(root.parts) < 3:
        raise RuntimeError("Use a dedicated absolute backup directory.")
    root.mkdir(parents=True, exist_ok=True)
    remote = os.environ["BACKUP_REMOTE"].rstrip("/")
    recipient = os.environ["AGE_RECIPIENT"]
    service = os.environ.get("PGSERVICE", "antiscam_backup")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", service) or ":" not in remote:
        raise RuntimeError(
            "Configure a PostgreSQL service and a dedicated rclone remote prefix."
        )
    with (root / "backup.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        started = datetime.now(timezone.utc)
        stem = "antiscam-" + started.strftime("%Y%m%dT%H%M%SZ")
        filename = stem + ".dump.age"
        with tempfile.TemporaryDirectory(prefix="pending-", dir=root) as temporary:
            dump = Path(temporary) / "database.dump"
            with psycopg.connect(service=service) as source:
                source.execute(
                    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
                )
                source.execute("SET LOCAL timezone='UTC'")
                snapshot = source.execute("SELECT pg_export_snapshot()").fetchone()[0]
                run(
                    [
                        "pg_dump",
                        "--dbname=service=" + service,
                        "--format=custom",
                        "--no-password",
                        "--snapshot",
                        snapshot,
                        "--file",
                        str(dump),
                    ]
                )
                row_manifest = database_manifest(source)
            run(["pg_restore", "--list", str(dump)], timeout=30)
            encrypted = Path(temporary) / filename
            run(
                [
                    "age",
                    "--recipient",
                    recipient,
                    "--output",
                    str(encrypted),
                    str(dump),
                ],
                timeout=60,
            )
            cipher_sha = checksum(encrypted)
            manifest = {
                "snapshot_started_at": started.isoformat(),
                "archive": filename,
                "dump_sha256": checksum(dump),
                "encrypted_sha256": cipher_sha,
                "encrypted_bytes": encrypted.stat().st_size,
                "tables": row_manifest,
            }
            manifest_path = Path(temporary) / (filename + ".json")
            manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            run(["rclone", "copyto", str(encrypted), remote + "/" + filename])
            run(
                [
                    "rclone",
                    "copyto",
                    str(manifest_path),
                    remote + "/" + manifest_path.name,
                ]
            )
            # Read back ciphertext to verify the offsite object, not only upload exit status.
            downloaded = Path(temporary) / "offsite-verified.age"
            run(["rclone", "copyto", remote + "/" + filename, str(downloaded)])
            if checksum(downloaded) != cipher_sha:
                raise RuntimeError("Offsite checksum mismatch")
            downloaded_manifest = Path(temporary) / "offsite-manifest.json"
            run(
                [
                    "rclone",
                    "copyto",
                    remote + "/" + manifest_path.name,
                    str(downloaded_manifest),
                ]
            )
            if checksum(downloaded_manifest) != checksum(manifest_path):
                raise RuntimeError("Offsite manifest checksum mismatch")
            encrypted.replace(root / filename)
            manifest_path.replace(root / manifest_path.name)
            finished = datetime.now(timezone.utc)
            status = {
                **manifest,
                "offsite_verified_at": finished.isoformat(),
                "seconds": (finished - started).total_seconds(),
            }
            status_path = root / "last-success.pending"
            status_path.write_text(json.dumps(status), encoding="utf-8")
            status_path.replace(root / "last-success.json")
        local_names = [p.name for p in root.iterdir() if NAME.fullmatch(p.name)]
        local_keep = retention_keep(local_names, daily=7, weekly=0)
        for name in set(local_names) - local_keep:
            for suffix in ("", ".json"):
                with contextlib.suppress(FileNotFoundError):
                    (root / (name + suffix)).unlink()
        remote_names = (
            run(["rclone", "lsf", remote, "--files-only", "--max-depth", "1"])
            .decode()
            .splitlines()
        )
        remote_names = [n for n in remote_names if NAME.fullmatch(n)]
        remote_keep = retention_keep(remote_names, daily=14, weekly=8)
        for name in set(remote_names) - remote_keep:
            run(["rclone", "deletefile", remote + "/" + name])
            run(["rclone", "deletefile", remote + "/" + name + ".json"])
        if status["seconds"] > 300:
            raise RuntimeError(
                "Backup verified but delivery exceeded the 5-minute objective"
            )
        print(
            json.dumps(
                {
                    "offsite_verified": True,
                    "seconds": status["seconds"],
                    "bytes": status["encrypted_bytes"],
                }
            )
        )


if __name__ == "__main__":
    main()
