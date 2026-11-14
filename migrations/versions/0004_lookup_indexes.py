"""Bound publication/card lookups as retained history grows."""

from alembic import op
import sqlalchemy as sa

revision = "0004_lookup_indexes"
down_revision = "0003_system_controls"
branch_labels = depends_on = None


def upgrade():
    op.create_index(
        "ix_message_items_chat_id_incident_key_telegram_message_id",
        "message_items",
        ["chat_id", "incident_key", "telegram_message_id"],
    )
    op.create_index(
        "ix_telegram_actions_incident_id_created_at",
        "telegram_actions",
        ["incident_id", "created_at"],
    )
    op.create_index(
        "ix_admin_cards_chat_id_incident_id",
        "admin_cards",
        ["chat_id", "incident_id"],
    )
    op.create_index(
        "ix_active_admin_chat",
        "admin_bindings",
        ["admin_chat_id"],
        postgresql_where=sa.text("closed_at IS NULL"),
    )


def downgrade():
    op.drop_index("ix_active_admin_chat", table_name="admin_bindings")
    op.drop_index("ix_admin_cards_chat_id_incident_id", table_name="admin_cards")
    op.drop_index(
        "ix_telegram_actions_incident_id_created_at", table_name="telegram_actions"
    )
    op.drop_index(
        "ix_message_items_chat_id_incident_key_telegram_message_id",
        table_name="message_items",
    )
