"""Regression for the production receiver crash on aiogram Default sentinels."""

import json

import pytest
from aiogram.types import Update
from sqlalchemy import select, func

from app.v2 import tables as t
from app.v2.queue import persist_updates


def publication(preview):
    return {
        "message_id": 80,
        "date": 1794769800,
        "chat": {"id": -1001, "type": "supergroup", "title": "Test"},
        "from": {"id": 900, "is_bot": False, "first_name": "Person"},
        "text": "Offer https://example.org",
        "entities": [{"type": "url", "offset": 6, "length": 19}],
        "link_preview_options": preview,
    }


def event(kind, preview):
    message = publication(preview)
    if kind == "reply":
        message = {**publication({}), "reply_to_message": message}
        kind = "message"
    elif kind == "callback_query":
        return {
            "update_id": 80,
            "callback_query": {
                "id": "test-callback",
                "from": message["from"],
                "chat_instance": "test-chat",
                "data": "test-button",
                "message": message,
            },
        }
    return {"update_id": 80, kind: message}


class CaptureConnection:
    def __init__(self):
        self.payload = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def begin(self):
        return self

    async def execute(self, statement):
        params = statement.compile().params
        if getattr(statement, "table", None) is t.inbox:
            self.payload = params["payload"]
        return self

    def one(self):
        return 1, False

    def scalar_one_or_none(self):
        return None


@pytest.mark.parametrize(
    "kind", ["message", "edited_message", "reply", "callback_query"]
)
@pytest.mark.parametrize(
    "preview",
    [
        {},
        {"is_disabled": True},
        {
            "url": "https://example.org",
            "prefer_small_media": False,
            "prefer_large_media": True,
            "show_above_text": False,
        },
    ],
)
async def test_received_fields_survive_inbox_roundtrip(kind, preview):
    raw = event(kind, preview)
    conn = CaptureConnection()
    await persist_updates(conn, 999001, [Update.model_validate(raw)])
    payload = json.loads(json.dumps(conn.payload))
    restored = Update.model_validate(payload)
    # Compare all wire fields, including explicit False, entities and nested replies.
    assert restored.model_dump(mode="json", by_alias=True, exclude_unset=True) == raw


async def test_preview_batch_is_persisted_once_and_cursor_advances(db):
    updates = []
    for i, kind in enumerate(
        ["message", "edited_message", "reply", "callback_query"], 80
    ):
        raw = event(kind, {"is_disabled": True})
        raw["update_id"] = i
        updates.append(Update.model_validate(raw))
    await persist_updates(db, 999001, updates)
    await persist_updates(db, 999001, updates)
    async with db.connect() as conn:
        rows = (
            (await conn.execute(select(t.inbox).order_by(t.inbox.c.update_id)))
            .mappings()
            .all()
        )
        assert len(rows) == 4
        for row, original in zip(rows, updates):
            restored = Update.model_validate(row["payload"])
            assert restored == original
        assert (
            await conn.execute(select(func.count()).select_from(t.jobs))
        ).scalar_one() == 4
        assert (await conn.execute(select(t.cursors.c.next_offset))).scalar_one() == 84
