from pydantic_settings import BaseSettings
from typing import Optional


class Settings(BaseSettings):
    DATABASE_URL: str
    API_KEY: Optional[str] = None
    LOG_LEVEL: str = "INFO"
    ENABLE_JSON_LOGS: str = "false"
    ENVIRONMENT: str = "development"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
