import uuid
import shutil
import subprocess

import psycopg
import pytest
from sqlalchemy.engine import make_url

from ops.db_manifest import sequence_issues
from ops.restore_drill import restore_environment


def connect(test_url):
    url = make_url(test_url)
    return psycopg.connect(
        host=url.host,
        port=url.port,
        user=url.username,
        password=url.password,
        dbname=url.database,
    )


@pytest.mark.skipif(not shutil.which("psql"), reason="Requires PostgreSQL CLI client")
def test_restore_ignores_inherited_backup_service(test_url, monkeypatch, tmp_path):
    service = tmp_path / "pg_service.conf"
    service.write_text(
        "[backup]\nhost=127.0.0.1\nport=1\nuser=wrong_backup_user\ndbname=wrong_backup_db\n"
    )
    monkeypatch.setenv("PGSERVICE", "backup")
    monkeypatch.setenv("PGSERVICEFILE", str(service))
    monkeypatch.setenv("PGCONNECT_TIMEOUT", "2")
    url = make_url(test_url)
    result = subprocess.run(
        [
            "psql",
            "-XAt",
            "--no-password",
            "--dbname",
            url.database,
            "-c",
            "SELECT current_database(), current_user",
        ],
        env=restore_environment(url),
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == f"{url.database}|{url.username}"


def test_explicit_telegram_and_picker_ids_do_not_fail_restore(test_url, clean_db):
    with connect(test_url) as conn, conn.transaction(force_rollback=True):
        conn.execute(
            "INSERT INTO polling_cursors(bot_id,next_offset) VALUES (9990009991,42)"
        )
        request = uuid.uuid4()
        conn.execute(
            "INSERT INTO onboarding_requests(id,owner_id,manager_id,work_actor_id,admin_actor_id,expires_at) VALUES (%s,111,111,111,111,now()+interval '10 minutes')",
            (request,),
        )
        conn.execute(
            "INSERT INTO onboarding_selections(request_id,request,actor_id,side,expires_at) VALUES (2003307360,%s,111,'work',now()+interval '10 minutes')",
            (request,),
        )
        assert sequence_issues(conn) == []


def test_restore_still_detects_real_sequence_collisions(test_url, clean_db):
    with connect(test_url) as conn, conn.transaction(force_rollback=True):
        conn.execute("CREATE TABLE restore_sequence_probe (id SERIAL PRIMARY KEY)")
        conn.execute("INSERT INTO restore_sequence_probe(id) VALUES (100)")
        issue = sequence_issues(conn)
        assert issue == [
            {
                "table": "restore_sequence_probe",
                "column": "id",
                "last_value": 1,
                "maximum": 100,
            }
        ]
        conn.execute(
            "SELECT setval(pg_get_serial_sequence('restore_sequence_probe','id'),100,false)"
        )
        assert sequence_issues(conn)[0]["last_value"] == 100
        conn.execute(
            "SELECT setval(pg_get_serial_sequence('restore_sequence_probe','id'),100,true)"
        )
        assert sequence_issues(conn) == []
