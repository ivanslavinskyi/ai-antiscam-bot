import asyncio
import os
import sys
from types import SimpleNamespace
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from aiogram.types import Message, ChatPermissions

from app.v2 import tables as t
from app.v2.db import engine_for
from app.v2.domain import Policy, GroupScope, Role
from app.v2.auth import Authorization


@pytest.fixture(scope="session")
def event_loop_policy():
    return (
        asyncio.WindowsSelectorEventLoopPolicy()
        if sys.platform == "win32"
        else asyncio.DefaultEventLoopPolicy()
    )


@pytest.fixture(scope="session")
def test_url():
    value = os.environ.get("ANTISCAM_TEST_DATABASE_URL")
    if not value:
        pytest.skip(
            "Set ANTISCAM_TEST_DATABASE_URL to an isolated local test database."
        )
    url = make_url(value)
    if url.host not in {"localhost", "127.0.0.1", "::1"} or not url.database.startswith(
        "antiscam_test_"
    ):
        raise RuntimeError("Tests refuse any database except local antiscam_test_*.")
    cfg = Config("alembic.ini")
    cfg.attributes["database_url"] = value
    command.upgrade(cfg, "head")
    return value


@pytest.fixture
def clean_db(test_url):
    engine = create_engine(
        make_url(test_url).set(drivername="postgresql+psycopg"), hide_parameters=True
    )
    with engine.begin() as conn:
        # The explicit test_url allowlist above is required before destructive fixture cleanup.
        names = [table.name for table in t.metadata.sorted_tables]
        conn.execute(
            text(
                "TRUNCATE "
                + ",".join('"' + n + '"' for n in names)
                + " RESTART IDENTITY CASCADE"
            )
        )
        conn.execute(
            t.chats.insert(),
            [
                dict(
                    id=i,
                    telegram_chat_id=-1000 - i,
                    title=f"Group {i}",
                    type="supergroup",
                    is_active=True,
                )
                for i in [1, 2]
            ],
        )
        conn.execute(
            t.aliases.insert(),
            [dict(chat_id=i, telegram_chat_id=-1000 - i) for i in [1, 2]],
        )
        conn.execute(
            t.group_settings.insert(),
            [
                dict(
                    chat_id=i,
                    lifecycle="connected",
                    mode="auto",
                    policy=Policy().model_dump(),
                )
                for i in [1, 2]
            ],
        )
        conn.execute(
            t.bindings.insert(),
            [
                dict(chat_id=i, admin_chat_id=-2000 - i, version=1, confirmed_by=111)
                for i in [1, 2]
            ],
        )
        conn.execute(
            t.roles.insert(),
            [
                dict(chat_id=None, actor_id=111, role="owner", granted_by=111),
                dict(chat_id=1, actor_id=222, role="manager", granted_by=111),
                dict(chat_id=2, actor_id=333, role="manager", granted_by=111),
            ],
        )
        # Explicit fixture IDs require sequence alignment for later onboarding inserts.
        conn.execute(text("SELECT setval(pg_get_serial_sequence('chats','id'),2,true)"))
    engine.dispose()


@pytest_asyncio.fixture
async def db(test_url, clean_db):
    engine = engine_for(test_url)
    yield engine
    await engine.dispose()


def message(mid=1, gid=1, uid=900, text="Hello", **extra):
    return Message.model_validate(
        dict(
            message_id=mid,
            date=datetime.now(timezone.utc),
            chat={"id": -1000 - gid, "type": "supergroup", "title": f"Group {gid}"},
            from_user={"id": uid, "is_bot": False, "first_name": "Person"},
            text=text,
            **extra,
        )
    )


class FakeBot:
    id = 999001

    def __init__(self):
        self.calls, self.members, self.fail_send, self.fail_action = (
            [],
            {},
            False,
            False,
        )
        self.sequence = 100

    async def get_chat_member(self, chat, user):
        self.calls.append(("get_chat_member", chat, user))
        state = self.members.get(
            (chat, user), "administrator" if user in {self.id, 111} else "member"
        )
        data = {
            "status": state,
            "is_member": True,
            "can_delete_messages": True,
            "can_restrict_members": True,
            "can_promote_members": True,
            "can_invite_users": True,
        }
        return SimpleNamespace(**data, model_dump=lambda **_: data)

    async def get_chat(self, chat):
        self.calls.append(("get_chat", chat))
        return SimpleNamespace(
            id=chat,
            type="supergroup",
            title="Selected group",
            permissions=ChatPermissions(can_send_messages=True),
        )

    async def get_me(self):
        return SimpleNamespace(id=self.id, username="test_antiscam_bot")

    async def send_message(self, chat, text, **kwargs):
        self.validate_buttons(kwargs.get("reply_markup"))
        self.calls.append(("send_message", chat, text, kwargs))
        if self.fail_send:
            raise TimeoutError()
        self.sequence += 1
        return SimpleNamespace(message_id=self.sequence)

    async def answer_callback_query(self, *args, **kwargs):
        self.calls.append(("answer", args, kwargs))

    async def edit_message_text(self, text, **kwargs):
        self.validate_buttons(kwargs.get("reply_markup"))
        self.calls.append(("edit_message_text", text, kwargs))
        return True

    @staticmethod
    def validate_buttons(markup):
        for row in getattr(markup, "inline_keyboard", []) or []:
            for button in row:
                if button.callback_data is not None:
                    assert 1 <= len(button.callback_data.encode()) <= 64, (
                        button.callback_data
                    )

    async def delete_message(self, chat_id, message_id):
        self.calls.append(("delete_message", chat_id, message_id))
        if self.fail_action:
            raise TimeoutError()
        return True

    async def ban_chat_member(self, chat_id, user_id, **kwargs):
        self.calls.append(("ban_chat_member", chat_id, user_id, kwargs))
        return True

    async def unban_chat_member(self, chat_id, user_id, **kwargs):
        self.calls.append(("unban_chat_member", chat_id, user_id, kwargs))
        return True

    async def restrict_chat_member(self, *args, **kwargs):
        self.calls.append(("restrict", args, kwargs))
        return True


@pytest.fixture
def bot():
    return FakeBot()


@pytest.fixture
def scope():
    return GroupScope(1, 222, -2001, 1, Role.MANAGER)


@pytest.fixture
def auth(db, bot):
    return Authorization(db, bot)
