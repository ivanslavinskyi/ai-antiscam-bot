from __future__ import annotations

import datetime as dt
from typing import Optional, List

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    JSON,
    func,
)
from sqlalchemy.orm import declarative_base, Mapped, mapped_column, relationship

Base = declarative_base()

from sqlalchemy import BigInteger

class Chat(Base):
    __tablename__ = "chats"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    telegram_chat_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    title: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    type: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)

    # НОВОЕ ПОЛЕ
    admin_chat_telegram_id: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
    )

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    messages: Mapped[list["Message"]] = relationship("Message", back_populates="chat")
    members: Mapped[list["ChatMember"]] = relationship("ChatMember", back_populates="chat")


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    username: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    first_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    last_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    is_global_whitelisted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_seen_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Участие пользователя в чатах
    memberships: Mapped[List["ChatMember"]] = relationship(
        "ChatMember",
        back_populates="user",
        cascade="all, delete-orphan",
    )

    # Сообщения, которые пользователь написал
    messages: Mapped[List["Message"]] = relationship(
        "Message",
        back_populates="user",
        foreign_keys="Message.user_id",
        cascade="all, delete-orphan",
    )

    # Сообщения, которые пользователь размечал руками (view-only, для аналитики)
    labeled_messages: Mapped[List["Message"]] = relationship(
        "Message",
        foreign_keys="Message.human_labeled_by",
        viewonly=True,
    )


class ChatMember(Base):
    __tablename__ = "chat_members"
    __table_args__ = (
        UniqueConstraint("chat_id", "user_id", name="uq_chat_members_chat_user"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(
        ForeignKey("chats.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )

    is_whitelisted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_blacklisted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    strike_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_strike_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))

    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    chat: Mapped["Chat"] = relationship("Chat", back_populates="members")
    user: Mapped["User"] = relationship("User", back_populates="memberships")


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    chat_id: Mapped[int] = mapped_column(
        ForeignKey("chats.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )

    telegram_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)

    text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Результат модели
    model_label: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    model_category: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    model_confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    model_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    model_raw_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    model_version: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    # Ручная разметка
    human_label: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    human_labeled_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    human_labeled_by: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    is_scam_effective: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    skipped_reason: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    chat: Mapped["Chat"] = relationship("Chat", back_populates="messages")

    # Автор сообщения (user_id)
    user: Mapped["User"] = relationship(
        "User",
        back_populates="messages",
        foreign_keys=[user_id],
    )

    # Кто размечал сообщение (human_labeled_by)
    human_labeled_by_user: Mapped[Optional["User"]] = relationship(
        "User",
        foreign_keys=[human_labeled_by],
        overlaps="labeled_messages",
    )
