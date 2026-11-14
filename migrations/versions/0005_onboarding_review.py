"""Durable applications with owner review; existing group data is untouched."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0005_onboarding_review"
down_revision = "0004_lookup_indexes"
branch_labels = depends_on = None


def upgrade():
    op.add_column(
        "onboarding_requests",
        sa.Column("durable", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "onboarding_requests",
        sa.Column(
            "approval_required", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    op.add_column("onboarding_requests", sa.Column("approved_by", sa.BigInteger()))
    op.add_column("onboarding_requests", sa.Column("approved_version", sa.Integer()))
    op.add_column("onboarding_requests", sa.Column("review_note", sa.Text()))
    op.add_column(
        "onboarding_requests",
        sa.Column(
            "initial_policy",
            JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.create_index(
        "ix_onboarding_owner_state", "onboarding_requests", ["owner_id", "state"]
    )


def downgrade():
    # Removing pending approvals could grant access under older code; require an explicit migration plan.
    raise RuntimeError(
        "Downgrade requires archiving pending applications and an explicit rollback plan."
    )
