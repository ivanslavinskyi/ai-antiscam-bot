"""Add group-isolated state without rewriting legacy records."""

from alembic import op
from pathlib import Path

revision = "0002_group_isolation"
down_revision = "0001_legacy_baseline"
branch_labels = depends_on = None


def upgrade():
    schema = (Path(__file__).parents[1] / "0002_schema.sql").read_text(encoding="utf-8")
    for statement in schema.split("\n-- statement boundary\n"):
        op.get_bind().exec_driver_sql(statement)


def downgrade():
    raise RuntimeError("V2 may contain live decisions. Use verified forward recovery.")
