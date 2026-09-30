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
    iiko_min_request_interval_ms: int = 1200

    loyalty_tiers_enabled: bool = False
    loyalty_tier_sync_enabled: bool = False
    loyalty_flat_discount_percent: float = 10.0
    loyalty_bonus_wallet_id: str = ""
    loyalty_cache_ttl_seconds: int = 600
    loyalty_refresh_cooldown_seconds: int = 60
    loyalty_history_days_initial: int = 730
    loyalty_history_overlap_days: int = 3

    coffee_stamp_wallet_id: str = ""
    coffee_stamp_goal: int = 7

    sms_ru_api_id: str = ""

    review_bypass_phone: str = ""
    review_bypass_code: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
