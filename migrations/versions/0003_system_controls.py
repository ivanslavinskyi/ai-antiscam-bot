"""Persistent emergency controls and operational heartbeats."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0003_system_controls"
down_revision = "0002_group_isolation"
branch_labels = depends_on = None


def upgrade():
    op.create_table(
        "system_state",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column(
            "value", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.execute(
        "INSERT INTO system_state (key,value) VALUES ('controls',jsonb_build_object('automatic_paused',false))"
    )


def downgrade():
    raise RuntimeError(
        "Emergency controls must not be removed by an unattended downgrade."
    )
