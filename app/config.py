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
    default_confidence_threshold: float = 0.8

    # Авто-добавление в whitelist после N сообщений "НЕ СКАМ"
    # (учитываются LLM=OK и ручные разметки NOT_SCAM)
    auto_whitelist_threshold: int = 5

    # TTL для уведомлений бота в рабочих группах (предупреждения/мут/бан).
    # 60 = удалить через 1 минуту. 0 или отрицательное = НЕ удалять.
    group_notice_ttl_seconds: int = 60

    # Режим отладки
    debug: bool = False


settings = Settings()