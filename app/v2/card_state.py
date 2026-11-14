"""Read current local trust when rendering a card; never infer a Telegram ban."""

from sqlalchemy import select

from app.v2 import tables as t
from app.v2.domain import now


async def member_card_state(conn, row):
    if not row.get("user_id"):
        return {}
    grant = (
        await conn.execute(
            select(t.trust.c.id).where(
                t.trust.c.chat_id == row["chat_id"],
                t.trust.c.user_id == row["user_id"],
                t.trust.c.revoked_at.is_(None),
                t.trust.c.expires_at.is_(None) | (t.trust.c.expires_at > now()),
            )
        )
    ).scalar_one_or_none()
    return {"trust_grant": grant}
