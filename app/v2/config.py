from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_ignore_empty=True,
        extra="ignore",
        hide_input_in_errors=True,
    )
    db_url: SecretStr
    telegram_bot_token: SecretStr = SecretStr("")
    openai_api_key: SecretStr = SecretStr("")
    openai_model: str = "gpt-4o-mini"
    owner_user_ids: str = ""
    worker_count: int = Field(default=4, ge=2, le=32)
    llm_concurrency: int = Field(default=2, ge=1, le=16)
    ocr_concurrency: int = Field(default=1, ge=1, le=8)
    ocr_max_bytes: int = Field(default=10 * 1024 * 1024, ge=1024, le=20 * 1024 * 1024)
    ocr_max_pixels: int = Field(default=20_000_000, ge=1000, le=40_000_000)
    ocr_timeout_seconds: int = Field(default=12, ge=1, le=30)
    llm_timeout_seconds: int = Field(default=20, ge=1, le=45)
    global_daily_budget_usd: float | None = Field(default=None, gt=0)
    max_input_chars: int = Field(default=16000, ge=1000, le=100000)
    max_input_tokens: int = Field(default=8000, ge=1000, le=30000)
    completion_tokens: int = Field(default=512, ge=128, le=2048)
    recovery_mode: bool = False
    log_level: str = "INFO"

    @property
    def owners(self) -> frozenset[int]:
        return frozenset(
            int(x.strip()) for x in self.owner_user_ids.split(",") if x.strip()
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
