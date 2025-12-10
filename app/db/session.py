from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import settings

# Превращаем pydantic AnyUrl в строку
DATABASE_URL = str(settings.db_url)

# sync-движок SQLAlchemy (для альфы этого достаточно)
engine = create_engine(
    DATABASE_URL,
    echo=settings.debug,
)

SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


@contextmanager
def get_session():
    """Простой контекстный менеджер для получения сессии."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
