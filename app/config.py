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
    otp_call_fallback_enabled: bool = True
    otp_requests_per_hour_per_phone: int = 5
    otp_calls_per_hour_per_phone: int = 3
    otp_calls_per_hour_per_ip: int = 10
    otp_failed_attempts_per_day_per_phone: int = 10

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

    referral_rewards_enabled: bool = False
    referral_bonus_amount: float = 500.0
    referral_share_url: str = "https://app.top1makovka.ru"

    media_dir: str = "./media"
    public_media_base_url: str = "https://app.top1makovka.ru/api/media"
    media_max_bytes: int = 5 * 1024 * 1024

    sms_ru_api_id: str = ""

    review_bypass_phone: str = ""
    review_bypass_code: str = ""
    review_demo_card_number: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()


def is_review_demo(settings: Settings, phone: str) -> bool:
    return bool(settings.review_demo_card_number and settings.review_bypass_phone) and (
        phone == settings.review_bypass_phone
    )
