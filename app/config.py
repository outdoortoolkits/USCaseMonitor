from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "USCaseMonitor"
    app_env: str = "development"
    secret_key: str = "change-me-before-production"
    admin_username: str = "admin"
    admin_password: str = "change-me-before-production"
    database_url: str = "sqlite:///./uscase.db"
    app_timezone: str = "Asia/Shanghai"
    poll_interval_hours: int = Field(default=24, ge=6)
    poll_batch_size: int = Field(default=20, ge=1, le=100)
    poll_delay_seconds: float = Field(default=1, ge=0)

    smtp_host: str = "smtp.163.com"
    smtp_port: int = 465
    smtp_use_ssl: bool = True
    smtp_username: str = ""
    smtp_auth_code: str = ""
    smtp_from_name: str = "USCaseMonitor"
    default_notify_emails: str = ""

    @property
    def notify_emails(self) -> list[str]:
        return [item.strip() for item in self.default_notify_emails.split(",") if item.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()

