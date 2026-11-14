from sqlalchemy import create_engine, select, func

from app.v2 import tables as t
from app.v2.migration import Manifest, import_data


def test_empty_membership_import_is_verified_and_repeatable(test_url, clean_db):
    engine = create_engine(test_url, hide_parameters=True)
    try:
        with engine.begin() as conn:
            # Keep the fixture's two legacy chats, but remove its prepared V2 state.
            for table in (t.bindings, t.roles, t.aliases, t.group_settings):
                conn.execute(table.delete())
            manifest = Manifest(
                bot_id=999001,
                primary_chat_id=1,
                primary_telegram_id=-1001,
                primary_admin_id=-2001,
                source_group_ids=[1, 2],
                archived_group_ids=[2],
                owner_ids=[111],
                primary_policy={},
                archive_sha256="0" * 64,
            )
            report = import_data(conn, manifest)
            assert report["legacy_balance"] == 0
            assert report["memberships_after"] == 0
            assert report["preserved_all_source_fields"]
            assert report["actions_created"] == 0
        with engine.begin() as conn:
            assert import_data(conn, manifest)["already_applied"]
            assert conn.execute(select(t.migrations.c.state)).scalar_one() == "verified"
            assert (
                conn.execute(select(func.count()).select_from(t.roles)).scalar_one()
                == 1
            )
            assert (
                conn.execute(
                    select(func.count()).select_from(t.member_states)
                ).scalar_one()
                == 0
            )
    finally:
        engine.dispose()
