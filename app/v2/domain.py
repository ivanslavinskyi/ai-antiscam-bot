from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, model_validator


def now() -> datetime:
    return datetime.now(timezone.utc)


class Denied(Exception):
    """An operation is outside the authenticated scope."""


class Conflict(Exception):
    """An operation was superseded or is not valid in the current state."""


class Role(StrEnum):
    OBSERVER = "observer"
    MODERATOR = "moderator"
    MANAGER = "manager"
    OWNER = "owner"


PERMISSIONS = {
    Role.OBSERVER: frozenset({"read"}),
    Role.MODERATOR: frozenset({"read", "moderate", "trust"}),
    Role.MANAGER: frozenset(
        {"read", "moderate", "trust", "settings", "export", "roles"}
    ),
    Role.OWNER: frozenset(
        {"read", "moderate", "trust", "settings", "export", "roles", "system"}
    ),
}


@dataclass(frozen=True)
class GroupScope:
    chat_id: int
    actor_id: int
    admin_chat_id: int
    binding_version: int
    role: Role

    def require(self, permission: str) -> None:
        if permission not in PERMISSIONS[self.role]:
            raise Denied("Недостаточно прав для этого действия.")


class Policy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    auto_whitelist_enabled: bool = True
    auto_whitelist_threshold: int = Field(default=5, ge=1, le=1000)
    confidence_threshold: float = Field(default=0.8, ge=0, le=1)
    warn_at: int = Field(default=1, ge=1)
    mute_at: int = Field(default=4, ge=2)
    ban_at: int = Field(default=5, ge=3)
    mute_seconds: int = Field(default=86400, ge=60, le=366 * 86400)
    mute_enabled: bool = True
    ban_enabled: bool = True
    exempt_admins: bool = True
    notice_ttl_seconds: int = Field(default=60, ge=0, le=86400)
    review_enabled: bool = True
    delete_when_admin_unavailable: bool = False
    timezone: str = "Europe/Zurich"
    daily_summary_time: str | None = None
    quiet_start: str | None = None
    quiet_end: str | None = None
    daily_budget_usd: float | None = Field(default=None, gt=0)
    context: str = ""
    max_action_age_seconds: int = Field(default=900, ge=60, le=86400)
    categories: list[str] = Field(
        default_factory=lambda: [
            "job_scam",
            "crypto",
            "investment",
            "phishing",
            "other",
        ]
    )

    @model_validator(mode="after")
    def validate_policy(self):
        if not self.warn_at < self.mute_at < self.ban_at:
            raise ValueError("Пороги предупреждения, мута и бана должны возрастать.")
        ZoneInfo(self.timezone)
        for value in (self.daily_summary_time, self.quiet_start, self.quiet_end):
            if value is not None:
                datetime.strptime(value, "%H:%M")
        if bool(self.quiet_start) != bool(self.quiet_end):
            raise ValueError("Нужно указать обе границы тихих часов.")
        if len(self.context) > 2000:
            raise ValueError("Описание сообщества: максимум 2000 символов.")
        allowed = {"job_scam", "crypto", "investment", "phishing", "other"}
        if not set(self.categories) <= allowed:
            raise ValueError("Неизвестная категория.")
        return self


def credit_eligible(
    model_label: str | None, human_label: str | None, complete=True
) -> bool:
    if human_label is not None:
        return human_label == "NOT_SCAM"
    return complete and model_label == "OK"


def sanction_for(balance: int, policy: Policy) -> str | None:
    if balance >= policy.ban_at and policy.ban_enabled:
        return "ban"
    if balance == policy.mute_at and policy.mute_enabled:
        return "mute"
    if balance == policy.warn_at:
        return "warn"
    return None


def quiet_delay(policy: Policy, moment=None) -> float:
    """Seconds until quiet hours end in the group's timezone, including midnight."""
    if not policy.quiet_start:
        return 0
    from datetime import timedelta

    local = (moment or now()).astimezone(ZoneInfo(policy.timezone))
    start = datetime.strptime(policy.quiet_start, "%H:%M").time()
    end = datetime.strptime(policy.quiet_end, "%H:%M").time()
    current = local.time().replace(tzinfo=None)
    inside = (
        start <= current < end if start < end else current >= start or current < end
    )
    if not inside:
        return 0
    target = local.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)
    if target <= local:
        target += timedelta(days=1)
    return max(
        1,
        (
            target.astimezone(timezone.utc) - local.astimezone(timezone.utc)
        ).total_seconds(),
    )
