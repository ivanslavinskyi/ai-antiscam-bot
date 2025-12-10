from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import AnyUrl


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Telegram
    telegram_bot_token: str

    # OpenAI
    openai_api_key: str
    openai_model: str = "gpt-4o-mini"

    # Postgres
    db_url: AnyUrl  # например: postgresql+psycopg2://user:pass@localhost:5432/antiscam

    # Админ-чаты (через запятую id телеграм-чатов)
    admin_chat_ids: str | None = None

    # Порог уверенности по умолчанию
    default_confidence_threshold: float = 0.7

    # Режим отладки
    debug: bool = False


settings = Settings()
