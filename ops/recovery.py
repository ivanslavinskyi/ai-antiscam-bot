"""Quarantine stale external work after backup restore, without contacting Telegram."""

import argparse
import os
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--quarantine-restored-work", action="store_true", required=True
    )
    parser.add_argument("--expected-database", required=True)
    args = parser.parse_args()
    url = make_url(os.environ["ANTISCAM_MIGRATION_DATABASE_URL"]).set(
        drivername="postgresql+psycopg"
    )
    if url.database != args.expected_database:
        raise RuntimeError("Database does not match the explicit recovery target")
    engine = create_engine(url, hide_parameters=True)
    with engine.begin() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(71392146)"))
        conn.execute(
            text(
                "INSERT INTO system_state (key,value) VALUES ('controls',jsonb_build_object('automatic_paused',true)) "
                "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value,updated_at=now()"
            )
        )
        conn.execute(
            text(
                "UPDATE telegram_actions SET state='cancelled',error='restore_quarantine' WHERE state='planned'"
            )
        )
        conn.execute(
            text(
                "UPDATE telegram_actions SET state='unknown',error='restore_reconcile' WHERE state='in_flight'"
            )
        )
        conn.execute(text("UPDATE outbox SET state='cancelled' WHERE state='pending'"))
        conn.execute(text("UPDATE outbox SET state='unknown' WHERE state='in_flight'"))
        conn.execute(
            text(
                "UPDATE jobs SET state='dead',last_error='restore_quarantine',lease_token=NULL,lease_until=NULL "
                "WHERE state IN ('pending','running') AND kind IN ('outbox','action','notice_cleanup')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO audit_events (id,kind,details) VALUES (gen_random_uuid(),'recovery.quarantine','{}'::jsonb)"
            )
        )
    engine.dispose()
    print(
        "Restored external work quarantined. Start with RECOVERY_MODE=true; reconcile before release."
    )


if __name__ == "__main__":
    main()
