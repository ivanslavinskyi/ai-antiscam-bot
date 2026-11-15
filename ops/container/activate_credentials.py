"""Enable the four new V2 database logins using a previously saved root-only file.

Run once on the VPS as root. No passwords are accepted on the command line,
printed, or regenerated. Application tables must not exist yet.
"""

import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys

ROLES = ("antiscam_migrator", "antiscam_runtime", "antiscam_backup", "antiscam_health")
ROLE_LIST = ",".join("'" + role + "'" for role in ROLES)


class SetupError(RuntimeError):
    """Only static, non-secret messages may be placed in this exception."""


def validate_credentials(document):
    if not isinstance(document, dict) or set(document) != {
        "version",
        "database",
        "passwords",
    }:
        raise SetupError("Unexpected credentials document")
    if (
        type(document["version"]) is not int
        or document["version"] != 1
        or document["database"] != "antiscam_v2"
    ):
        raise SetupError("Unexpected credentials version or database")
    passwords = document["passwords"]
    if not isinstance(passwords, dict) or set(passwords) != set(ROLES):
        raise SetupError("Unexpected role names")
    if any(
        not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{64}", value)
        for value in passwords.values()
    ):
        raise SetupError("Unexpected password format")
    if len(set(passwords.values())) != len(ROLES):
        raise SetupError("Role passwords must differ")
    return passwords


def read_credentials(path):
    parent = path.parent.lstat()
    if (
        not stat.S_ISDIR(parent.st_mode)
        or parent.st_uid != 0
        or stat.S_IMODE(parent.st_mode) != 0o700
    ):
        raise SetupError("Credentials directory must be root-owned with mode 0700")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "r", encoding="ascii") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != 0
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_size > 8192
        ):
            raise SetupError("Credentials file must be root-owned with mode 0600")
        return validate_credentials(json.load(stream))


def activation_sql(passwords):
    # Restrict names and values before interpolation; none may contain quotes,
    # backslashes, newlines, psql metacommands, or SQL expressions.
    validate_credentials(
        {"version": 1, "database": "antiscam_v2", "passwords": passwords}
    )
    changes = "\n".join(
        f"ALTER ROLE {role} LOGIN PASSWORD '{passwords[role]}';" for role in ROLES
    )
    return f"""BEGIN;
SET LOCAL log_statement = 'none';
SET LOCAL log_duration = off;
SET LOCAL log_min_duration_statement = -1;
SET LOCAL log_min_duration_sample = -1;
SET LOCAL log_statement_sample_rate = 0;
SET LOCAL log_transaction_sample_rate = 0;
SET LOCAL log_min_error_statement = panic;
SET LOCAL log_parameter_max_length_on_error = 0;
SET LOCAL password_encryption = 'scram-sha-256';
DO $$
BEGIN
  IF current_database() <> 'antiscam_v2' OR current_user <> 'postgres'
     OR current_setting('server_version_num')::integer NOT BETWEEN 170012 AND 179999 THEN
    RAISE EXCEPTION 'Unexpected activation connection';
  END IF;
  IF (SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname=current_database()) <> 'antiscam_migrator'
     OR EXISTS (SELECT 1 FROM pg_tables WHERE schemaname='public') THEN
    RAISE EXCEPTION 'Expected a new empty V2 database';
  END IF;
  IF (SELECT count(*) FROM pg_authid WHERE rolname IN ({ROLE_LIST})) <> 4
     OR EXISTS (SELECT 1 FROM pg_authid WHERE rolname IN ({ROLE_LIST}) AND
       (rolcanlogin OR rolpassword IS NOT NULL OR rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls))
     OR EXISTS (SELECT 1 FROM pg_auth_members m JOIN pg_authid r ON r.oid=m.member OR r.oid=m.roleid WHERE r.rolname IN ({ROLE_LIST})) THEN
    RAISE EXCEPTION 'Expected four unused unprivileged NOLOGIN roles';
  END IF;
END $$;
{changes}
COMMIT;
SELECT json_agg(json_build_object(
  'role',rolname,'login',rolcanlogin,'scram',rolpassword LIKE 'SCRAM-SHA-256$%',
  'elevated',rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls
) ORDER BY rolname) FROM pg_authid WHERE rolname IN ({ROLE_LIST});
"""


def execute(args, sql=None):
    result = subprocess.run(args, input=sql, capture_output=True, text=True, timeout=60)
    if result.returncode:
        # PostgreSQL errors can include SQL containing secrets. Never forward
        # subprocess stdout/stderr or attach them to an exception.
        raise SetupError(
            "Command failed; inspect role state before retrying; do not regenerate credentials"
        )
    return result.stdout.strip()


def activate(passwords, container="antiscam-v2-db-1", project="antiscam-v2"):
    sql = activation_sql(passwords)
    identity = execute(
        [
            "docker",
            "inspect",
            container,
            "--format",
            '{{index .Config.Labels "com.docker.compose.project"}}/'
            '{{index .Config.Labels "com.docker.compose.service"}}/'
            "{{.State.Running}}/{{.State.Health.Status}}",
        ]
    )
    if identity != project + "/db/true/healthy":
        raise SetupError("Unexpected database container or health")
    rows = json.loads(
        execute(
            [
                "docker",
                "exec",
                "-i",
                "--user",
                "postgres",
                container,
                "psql",
                "-XqAt",
                "--no-password",
                "-v",
                "ON_ERROR_STOP=1",
                "-U",
                "postgres",
                "-d",
                "antiscam_v2",
            ],
            sql=sql,
        )
    )
    if (
        not isinstance(rows, list)
        or {r.get("role") for r in rows} != set(ROLES)
        or len(rows) != 4
    ):
        raise SetupError(
            "Unexpected role verification result; inspect state before retrying"
        )
    if not all(
        r.get("login") is True and r.get("scram") is True and r.get("elevated") is False
        for r in rows
    ):
        raise SetupError("Role verification failed; inspect state before retrying")
    return {
        "result": "activated",
        "database": "antiscam_v2",
        "roles": [
            {"role": r["role"], "login": True, "scram": True, "elevated": False}
            for r in rows
        ],
    }


def main():
    try:
        if not sys.platform.startswith("linux") or os.geteuid() != 0:
            raise SetupError("Run on the VPS as root")
        passwords = read_credentials(Path("/etc/antiscam-v2/database-credentials.json"))
        report = activate(passwords)
    except SetupError as exc:
        print(json.dumps({"result": "failed", "message": str(exc)}))
        return 1
    except Exception as exc:
        print(
            json.dumps(
                {
                    "result": "failed",
                    "error_type": type(exc).__name__,
                    "message": "Inspect state before retrying; do not regenerate credentials",
                }
            )
        )
        return 1
    print(json.dumps(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
