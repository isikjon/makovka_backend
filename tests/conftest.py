from collections.abc import Callable
from typing import Any

import pytest

from app.config import Settings

TEST_GUEST_PHONE = "+79990000001"


@pytest.fixture
def make_settings() -> Callable[..., Settings]:
    def factory(**overrides: Any) -> Settings:
        values: dict[str, Any] = {
            "iiko_api_login": "test-login",
            "iiko_organization_id": "org-1",
            "iiko_env": "staging",
            "iiko_test_guest_phone": TEST_GUEST_PHONE,
            "iiko_mutations_enabled": True,
            "iiko_min_request_interval_ms": 0,
        }
        values.update(overrides)
        return Settings(_env_file=None, **values)

    return factory
