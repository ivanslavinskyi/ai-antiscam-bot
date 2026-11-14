"""
Одноразовый скрипт миграции: добавляет поля is_active, deactivated_at,
deactivation_reason в таблицу chats.

Запуск:
    python migrate_add_chat_active.py

Безопасен для повторного запуска (IF NOT EXISTS / IF EXISTS).
"""

import sys
import os

# Чтобы импортировать app.config, нужно, чтобы корень проекта был в sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.config import settings
from sqlalchemy import create_engine, text

DATABASE_URL = str(settings.db_url)
engine = create_engine(DATABASE_URL)

STATEMENTS = [
    "ALTER TABLE chats ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT TRUE;",
    "ALTER TABLE chats ADD COLUMN IF NOT EXISTS deactivated_at TIMESTAMPTZ;",
    "ALTER TABLE chats ADD COLUMN IF NOT EXISTS deactivation_reason VARCHAR(255);",
]


def main():
    print(f"Connecting to: {DATABASE_URL.split('@')[-1]}")  # без credentials
    with engine.begin() as conn:
        for stmt in STATEMENTS:
            print(f"  → {stmt}")
            conn.execute(text(stmt))
    print("Migration complete.")


if __name__ == "__main__":
    main()
