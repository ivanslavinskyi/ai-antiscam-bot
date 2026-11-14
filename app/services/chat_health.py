"""
Chat health utilities: auto-deactivation of chats where the bot can no longer operate.

Centralised logic so that both admin.py and messages.py can share it
without circular imports.
"""

import datetime as dt
import logging

from sqlalchemy import update

from app.db.session import get_session
from app.db import models as db

logger = logging.getLogger(__name__)

# Подстроки ошибок Telegram API, при которых чат считается «мёртвым».
# Формат: (marker_substring, deactivation_reason).
_DEACTIVATION_MARKERS: list[tuple[str, str]] = [
    ("chat not found", "chat_not_found"),
    ("chat was deleted", "chat_not_found"),
    ("bot was kicked", "bot_kicked"),
    ("bot is not a member", "bot_not_member"),
    ("not enough rights", "bot_not_admin"),
    ("CHAT_NOT_FOUND", "chat_not_found"),
    ("have no rights", "bot_not_admin"),
    ("the group chat was deleted", "chat_not_found"),
    ("bot was blocked by the user", "bot_blocked"),
    ("chat was deactivated", "chat_not_found"),
]


def should_deactivate_chat(error: Exception) -> str | None:
    """Return a reason string if the error indicates the chat is dead, else None."""
    err_str = str(error)
    for marker, reason in _DEACTIVATION_MARKERS:
        if marker.lower() in err_str.lower():
            return reason
    return None


def deactivate_chat(telegram_chat_id: int, reason: str) -> None:
    """Mark a chat as inactive in the database."""
    with get_session() as session:
        session.execute(
            update(db.Chat)
            .where(db.Chat.telegram_chat_id == telegram_chat_id)
            .values(
                is_active=False,
                deactivated_at=dt.datetime.now(dt.timezone.utc),
                deactivation_reason=reason,
            )
        )
        session.commit()
    logger.warning(
        "Chat %s deactivated: reason=%s", telegram_chat_id, reason
    )


def reactivate_chat(telegram_chat_id: int) -> bool:
    """Re-activate a previously deactivated chat.

    Returns True if the chat was found and reactivated, False otherwise.
    """
    with get_session() as session:
        result = session.execute(
            update(db.Chat)
            .where(
                db.Chat.telegram_chat_id == telegram_chat_id,
                db.Chat.is_active == False,  # noqa: E712
            )
            .values(
                is_active=True,
                deactivated_at=None,
                deactivation_reason=None,
            )
        )
        session.commit()
        changed = int(getattr(result, "rowcount", 0) or 0)
    if changed:
        logger.info("Chat %s reactivated", telegram_chat_id)
    return changed > 0
