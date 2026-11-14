"""Verify the exact legacy schema before adopting Alembic."""

from alembic import op
from sqlalchemy import inspect
from app.db.models import Base

revision = "0001_legacy_baseline"
down_revision = None
branch_labels = depends_on = None


def upgrade():
    conn = op.get_bind()
    inspector = inspect(conn)
    names = set(inspector.get_table_names())
    expected = {"chats", "users", "chat_members", "messages"}
    if not (names & expected):
        if names - {"alembic_version"}:
            raise RuntimeError("Unrelated database: baseline refused.")
        for table in Base.metadata.sorted_tables:
            if table.name in expected:
                table.create(conn)
        return
    if not expected <= names:
        raise RuntimeError("Partial legacy schema: baseline refused.")
    for name in expected:
        cols = {c["name"]: c for c in inspector.get_columns(name)}
        table = Base.metadata.tables[name]
        if set(cols) != set(table.c.keys()):
            raise RuntimeError(f"Unexpected legacy columns: {name}")
        for col in table.c:
            actual = cols[col.name]
            actual_type = str(actual["type"].compile(dialect=conn.dialect))
            expected_type = str(col.type.compile(dialect=conn.dialect))
            equivalent_float = (
                actual_type == "DOUBLE PRECISION" and expected_type == "FLOAT"
            )
            if actual_type != expected_type and not equivalent_float:
                raise RuntimeError(f"Unexpected column type: {name}.{col.name}")
            if actual["nullable"] != col.nullable:
                raise RuntimeError(f"Unexpected nullability: {name}.{col.name}")
        if inspector.get_pk_constraint(name)["constrained_columns"] != ["id"]:
            raise RuntimeError(f"Unexpected primary key: {name}")
    constraints = inspector.get_unique_constraints("chat_members")
    if not any(c["column_names"] == ["chat_id", "user_id"] for c in constraints):
        raise RuntimeError("Legacy membership uniqueness is missing.")


def downgrade():
    raise RuntimeError("Legacy data must never be removed by downgrade.")
