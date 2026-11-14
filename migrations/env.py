import os
from alembic import context
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from app.v2.tables import metadata

config = context.config
url = config.attributes.get("database_url") or os.environ.get(
    "ANTISCAM_MIGRATION_DATABASE_URL"
)
if not url:
    raise RuntimeError(
        "Set ANTISCAM_MIGRATION_DATABASE_URL explicitly; .env is not loaded."
    )
parsed = make_url(url)
if parsed.get_backend_name() != "postgresql":
    raise RuntimeError("PostgreSQL is required.")
if context.is_offline_mode():
    raise RuntimeError("Offline baseline cannot verify the existing schema.")
engine = create_engine(
    parsed.set(drivername="postgresql+psycopg"), hide_parameters=True
)
with engine.connect() as connection:
    context.configure(
        connection=connection, target_metadata=metadata, transaction_per_migration=True
    )
    with context.begin_transaction():
        context.run_migrations()
engine.dispose()
