"""Restore a local dump or age archive to an EMPTY antiscam_restore_* database."""

import argparse
import json
import os
from pathlib import Path
import tempfile
import time

import psycopg
from sqlalchemy.engine import make_url
from ops.db_manifest import database_manifest, sequence_issues
from ops.backup import checksum, run


def restore_environment(url):
    # A backup service can override PGHOST/PGPORT/PGUSER inherited by libpq.
    # Restore must use the same explicit target as the empty-database check.
    environment = dict(os.environ)
    environment.pop("PGSERVICE", None)
    environment.pop("PGSERVICEFILE", None)
    environment.update(
        PGHOST=url.host or "localhost",
        PGPORT=str(url.port or 5432),
        PGUSER=url.username or "",
        PGPASSWORD=url.password or "",
        PGDATABASE=url.database,
    )
    return environment


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("manifest", type=Path)
    parser.add_argument(
        "--identity", type=Path, help="Only needed for age-encrypted archives"
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    started = time.monotonic()
    url = make_url(os.environ["ANTISCAM_RESTORE_DATABASE_URL"])
    if url.get_backend_name() != "postgresql" or not url.database.startswith(
        "antiscam_restore_"
    ):
        raise RuntimeError(
            "Target must be an explicitly created antiscam_restore_* database"
        )
    expected = json.loads(args.manifest.read_text(encoding="utf-8"))
    encrypted = "encrypted_sha256" in expected
    if encrypted and not args.identity:
        raise RuntimeError("An age identity is required for this encrypted backup")
    if (
        checksum(args.archive)
        != expected["encrypted_sha256" if encrypted else "dump_sha256"]
    ):
        raise RuntimeError("Backup checksum mismatch")
    connection = dict(
        host=url.host,
        port=url.port or 5432,
        user=url.username,
        password=url.password,
        dbname=url.database,
    )
    with psycopg.connect(**connection) as conn:
        if conn.execute(
            "SELECT count(*) FROM pg_tables WHERE schemaname='public'"
        ).fetchone()[0]:
            raise RuntimeError(
                "Restore target is not empty; no existing database is overwritten"
            )
    environment = restore_environment(url)
    os.umask(0o077)
    with tempfile.TemporaryDirectory(prefix="antiscam-restore-") as temporary:
        dump = Path(temporary) / "verified.dump" if encrypted else args.archive
        if encrypted:
            run(
                [
                    "age",
                    "--decrypt",
                    "--identity",
                    str(args.identity),
                    "--output",
                    str(dump),
                    str(args.archive),
                ],
                timeout=180,
            )
        if checksum(dump) != expected["dump_sha256"]:
            raise RuntimeError("Decrypted dump checksum mismatch")
        run(
            [
                "pg_restore",
                "--no-password",
                "--dbname",
                url.database,
                "--no-owner",
                "--no-privileges",
                "--exit-on-error",
                "--single-transaction",
                str(dump),
            ],
            timeout=3300,
            env=environment,
        )
    with psycopg.connect(**connection) as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        conn.execute("SET LOCAL timezone='UTC'")
        actual = database_manifest(conn)
        invalid = conn.execute(
            "SELECT count(*) FROM pg_constraint WHERE NOT convalidated"
        ).fetchone()[0]
        sequences = sequence_issues(conn)
    report = {
        "tables_match": actual == expected.get("tables"),
        "invalid_constraints": invalid,
        "sequence_issues": sequences,
        "seconds": time.monotonic() - started,
        "database": url.database,
        "tables": actual,
    }
    report["passed"] = (
        report["tables_match"]
        and not invalid
        and not sequences
        and report["seconds"] <= 3600
    )
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "tables"}))
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
