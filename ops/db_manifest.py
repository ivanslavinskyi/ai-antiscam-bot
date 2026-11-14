"""Canonical row manifests within an explicit PostgreSQL read snapshot."""

import hashlib
from psycopg import sql


# Schema 0002 created serial defaults for these externally assigned keys.
# Every application insert supplies the Telegram ID or random selection ID;
# their unused sequences are not allocators and must not be advanced to max(ID).
# Keep this list explicit: all actual and unknown sequence allocators are checked.
EXPLICIT_ID_COLUMNS = frozenset(
    {
        ("polling_cursors", "bot_id"),
        ("onboarding_selections", "request_id"),
        ("group_id_aliases", "telegram_chat_id"),
        ("onboarding_candidates", "telegram_chat_id"),
    }
)


def database_manifest(conn):
    tables = conn.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename"
    ).fetchall()
    report = {}
    for (name,) in tables:
        digest = hashlib.sha256()
        count = 0
        with conn.cursor(name="manifest_rows") as cursor:
            cursor.execute(
                sql.SQL(
                    'SELECT to_jsonb(r)::text FROM {} r ORDER BY to_jsonb(r)::text COLLATE "C"'
                ).format(sql.Identifier("public", name))
            )
            for (row,) in cursor:
                data = row.encode("utf-8")
                digest.update(len(data).to_bytes(8, "big"))
                digest.update(data)
                count += 1
        report[name] = {"rows": count, "sha256": digest.hexdigest()}
    return report


def sequence_issues(conn):
    issues = []
    rows = conn.execute(
        "SELECT table_name, column_name, pg_get_serial_sequence(format('%I.%I', table_schema, table_name), column_name) FROM information_schema.columns WHERE table_schema='public'"
    ).fetchall()
    for table, column, sequence in rows:
        if not sequence or (table, column) in EXPLICIT_ID_COLUMNS:
            continue
        # pg_get_serial_sequence returns a server-generated identifier, parsed by regclass.
        seq_parts = conn.execute(
            "SELECT n.nspname,c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.oid=%s::regclass",
            (sequence,),
        ).fetchone()
        last, called = conn.execute(
            sql.SQL("SELECT last_value,is_called FROM {}").format(
                sql.Identifier(*seq_parts)
            )
        ).fetchone()
        maximum = conn.execute(
            sql.SQL("SELECT max({}) FROM {}").format(
                sql.Identifier(column), sql.Identifier("public", table)
            )
        ).fetchone()[0]
        if maximum is not None and (last < maximum or last == maximum and not called):
            issues.append(
                {
                    "table": table,
                    "column": column,
                    "last_value": last,
                    "maximum": maximum,
                }
            )
    return issues
