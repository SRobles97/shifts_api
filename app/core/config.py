from pydantic_settings import BaseSettings
from typing import Optional


class Settings(BaseSettings):
    DATABASE_URL: str
    API_KEY: Optional[str] = None
    LOG_LEVEL: str = "INFO"
    ENABLE_JSON_LOGS: str = "false"
    ENVIRONMENT: str = "development"
    # auth_api base URL (…/auth-api). Only used to name who made a schedule
    # change in the notification email; unset means "No identificado", never
    # a failed save.
    AUTH_API_URL: str = ""
    AUTH_ME_TIMEOUT_SECONDS: float = 3.0

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"


settings = Settings()
