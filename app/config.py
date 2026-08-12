from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    database_url: str = "postgresql+asyncpg://localhost/makovka_dev"

    jwt_secret: str = "dev-secret-change-me"
    jwt_algorithm: str = "HS256"
    access_token_ttl_minutes: int = 15
    refresh_token_ttl_days: int = 30

    otp_mock_mode: bool = True
    otp_dev_code: str = "000000"
    otp_ttl_minutes: int = 5

    iiko_api_login: str = ""
    iiko_base_url: str = "https://api-ru.iiko.services"
    iiko_organization_id: str = ""
    iiko_env: str = "staging"
    iiko_test_guest_phone: str = ""
    iiko_mutations_enabled: bool = True

    sms_ru_api_id: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
