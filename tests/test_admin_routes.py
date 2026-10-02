import asyncio
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from app.admin import web
from app.admin.security import (
    create_session_token,
    csrf_token,
    decode_session_token,
    hash_password,
    login_throttle,
    session_fingerprint,
)
from app.admin.web import SECURITY_HEADERS, admin_app, get_admin_iiko
from app.config import get_settings
from app.db import get_db
from app.iiko.client import IikoClientError
from app.main import app
from app.models import AdminUser, AppSetting, LoyaltyTier, Novelty
from app.runtime_settings import OVERRIDABLE_KEYS, get_effective_settings
from app.security import create_access_token

PASSWORD = "correct-horse-battery"
PASSWORD_HASH = hash_password(PASSWORD)
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
OLD_IMAGE = "0123456789abcdef0123456789abcdef.png"
CATEGORY = "3fa85f64-5717-4562-b3fc-2c963f66afa6"
OTHER_CATEGORY = "11111111-2222-3333-4444-555555555555"


class AdminDb:
    def __init__(self, admin: AdminUser):
        self.admin = admin
        self.objects: dict[tuple[type, uuid.UUID], object] = {(AdminUser, admin.id): admin}
        self.scalar_result: object = admin
        self.listed: list = []
        self.added: list = []
        self.deleted: list = []
        self.commits = 0

    def put(self, obj) -> None:
        self.objects[(type(obj), obj.id)] = obj

    async def get(self, entity, key, **kwargs):
        return self.objects.get((entity, key))

    async def scalar(self, statement):
        return self.scalar_result

    async def scalars(self, statement):
        return list(self.listed)

    def add(self, obj) -> None:
        if obj.id is None:
            obj.id = uuid.uuid4()
        self.added.append(obj)

    async def delete(self, obj) -> None:
        self.deleted.append(obj)

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        pass


class AdminEnv:
    def __init__(self, settings, db: AdminDb):
        self.settings = settings
        self.db = db
        self.admin = db.admin

    def session_token(self, **kwargs) -> str:
        fingerprint = session_fingerprint(self.admin.password_hash, self.admin.session_version)
        return create_session_token(self.settings, self.admin.id, fingerprint, **kwargs)

    def auth(self, token: str | None = None) -> tuple[dict[str, str], str]:
        token = token or self.session_token()
        session = decode_session_token(self.settings, token)
        return {"Cookie": f"admin_session={token}"}, csrf_token(self.settings, session.session_id)

    @property
    def media(self) -> Path:
        return Path(self.settings.media_dir)

    def media_files(self) -> list[str]:
        return sorted(path.name for path in self.media.iterdir())


@pytest.fixture
def admin_env(make_settings, tmp_path):
    media = tmp_path / "media"
    media.mkdir()
    settings = make_settings(jwt_secret="admin-test-secret", media_dir=str(media), media_max_bytes=1024)
    admin = AdminUser(
        id=uuid.uuid4(), login="admin", password_hash=PASSWORD_HASH, is_active=True, session_version=0
    )
    db = AdminDb(admin)

    async def fake_db():
        yield db

    admin_app.dependency_overrides[get_db] = fake_db
    admin_app.dependency_overrides[get_settings] = lambda: settings
    admin_app.dependency_overrides[get_effective_settings] = lambda: settings
    admin_app.dependency_overrides[get_admin_iiko] = lambda: None
    login_throttle.clear()
    yield AdminEnv(settings, db)
    admin_app.dependency_overrides.clear()
    login_throttle.clear()


@pytest.fixture
async def client():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://test") as http:
        yield http


def _novelty(**fields) -> Novelty:
    values = {"id": uuid.uuid4(), "title": "Круассан", "image": None, "sort_order": 0, "is_active": True}
    values.update(fields)
    return Novelty(**values)


def _assert_security_headers(response: httpx.Response) -> None:
    for name, value in SECURITY_HEADERS.items():
        assert response.headers[name] == value


PROTECTED_PAGES = [
    "/admin/",
    "/admin/novelties",
    "/admin/novelties/new",
    "/admin/promotions",
    "/admin/promotions/new",
    "/admin/bakeries",
    "/admin/tiers",
    "/admin/settings",
    "/admin/guests",
    f"/admin/novelties/{uuid.uuid4()}",
    f"/admin/tiers/{uuid.uuid4()}",
]
PROTECTED_ACTIONS = [
    "/admin/novelties/new",
    f"/admin/novelties/{uuid.uuid4()}",
    f"/admin/novelties/{uuid.uuid4()}/delete",
    f"/admin/promotions/{uuid.uuid4()}/toggle",
    f"/admin/bakeries/{uuid.uuid4()}/move",
    f"/admin/tiers/{uuid.uuid4()}",
    "/admin/settings",
    "/admin/logout",
]


class TestSecurityHeaders:
    async def test_login_page(self, admin_env, client):
        response = await client.get("/admin/login")
        assert response.status_code == 200
        _assert_security_headers(response)
        assert "script-src 'none'" in response.headers["content-security-policy"]

    async def test_static_and_redirects(self, admin_env, client):
        css = await client.get("/admin/static/admin.css")
        assert css.status_code == 200
        _assert_security_headers(css)
        _assert_security_headers(await client.get("/admin/novelties"))

    async def test_api_is_unaffected(self, client):
        assert "x-frame-options" not in (await client.get("/health")).headers


class TestAuthRequired:
    @pytest.mark.parametrize("path", PROTECTED_PAGES)
    async def test_pages_redirect_to_login(self, admin_env, client, path):
        response = await client.get(path)
        assert response.status_code == 303
        assert response.headers["location"] == "/admin/login"

    @pytest.mark.parametrize("path", PROTECTED_ACTIONS)
    async def test_actions_redirect_without_side_effects(self, admin_env, client, path):
        response = await client.post(path, data={"csrf_token": "x", "confirm": "on"})
        assert response.status_code == 303
        assert response.headers["location"] == "/admin/login"
        assert admin_env.db.commits == 0

    async def test_app_access_token_is_rejected(self, admin_env, client):
        token = create_access_token(admin_env.admin.id)
        response = await client.get("/admin/novelties", headers={"Cookie": f"admin_session={token}"})
        assert response.status_code == 303

    async def test_expired_session(self, admin_env, client):
        token = admin_env.session_token(now=datetime.now(timezone.utc) - timedelta(hours=13))
        response = await client.get("/admin/settings", headers={"Cookie": f"admin_session={token}"})
        assert response.status_code == 303

    async def test_deactivated_admin(self, admin_env, client):
        headers, _ = admin_env.auth()
        admin_env.admin.is_active = False
        assert (await client.get("/admin/settings", headers=headers)).status_code == 303

    async def test_password_change_ends_old_sessions(self, admin_env, client):
        headers, _ = admin_env.auth()
        admin_env.admin.password_hash = hash_password("another-password")
        assert (await client.get("/admin/settings", headers=headers)).status_code == 303

    async def test_deleted_admin(self, admin_env, client):
        headers, _ = admin_env.auth()
        admin_env.db.objects.clear()
        assert (await client.get("/admin/settings", headers=headers)).status_code == 303


class TestCsrf:
    async def test_missing_token(self, admin_env, client):
        item = _novelty()
        admin_env.db.put(item)
        headers, _ = admin_env.auth()
        response = await client.post(f"/admin/novelties/{item.id}/toggle", headers=headers)
        assert response.status_code == 403
        assert item.is_active is True
        assert admin_env.db.commits == 0

    async def test_token_from_another_session(self, admin_env, client):
        item = _novelty()
        admin_env.db.put(item)
        headers, _ = admin_env.auth()
        _, foreign_csrf = admin_env.auth()
        response = await client.post(
            f"/admin/novelties/{item.id}/toggle", headers=headers, data={"csrf_token": foreign_csrf}
        )
        assert response.status_code == 403
        assert item.is_active is True

    async def test_valid_token(self, admin_env, client):
        item = _novelty()
        admin_env.db.put(item)
        headers, csrf = admin_env.auth()
        response = await client.post(f"/admin/novelties/{item.id}/toggle", headers=headers, data={"csrf_token": csrf})
        assert response.status_code == 303
        assert response.headers["location"] == "/admin/novelties?msg=saved"
        assert item.is_active is False
        assert admin_env.db.commits == 1

    async def test_settings_and_logout_need_token(self, admin_env, client):
        headers, _ = admin_env.auth()
        assert (await client.post("/admin/settings", headers=headers, data={})).status_code == 403
        response = await client.post("/admin/logout", headers=headers, data={"csrf_token": "forged"})
        assert response.status_code == 403
        assert "set-cookie" not in response.headers


def _csrf_from(html: str) -> str:
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


async def _login(client, login: str, password: str, ip: str = "203.0.113.1") -> httpx.Response:
    page = await client.get("/admin/login")
    return await client.post(
        "/admin/login",
        data={"csrf_token": _csrf_from(page.text), "login": login, "password": password},
        headers={"X-Real-IP": ip},
    )


class SlowVerifier:
    def __init__(self, delay: float = 0.05):
        self.delay = delay
        self.calls = 0
        self.active = 0
        self.peak = 0
        self._lock = threading.Lock()

    def __call__(self, password: str, stored: str | None) -> bool:
        with self._lock:
            self.calls += 1
            self.active += 1
            self.peak = max(self.peak, self.active)
        time.sleep(self.delay)
        with self._lock:
            self.active -= 1
        return False


async def _burst(client, count: int, ips: list[str]) -> list[int]:
    page = await client.get("/admin/login")
    token = _csrf_from(page.text)
    responses = await asyncio.gather(
        *(
            client.post(
                "/admin/login",
                data={"csrf_token": token, "login": "admin", "password": "wrong"},
                headers={"X-Real-IP": ips[index % len(ips)]},
            )
            for index in range(count)
        )
    )
    return sorted(response.status_code for response in responses)


class TestLogin:
    async def test_login_nonce_cookie_flags(self, admin_env, client):
        cookie = (await client.get("/admin/login")).headers["set-cookie"]
        assert cookie.startswith("admin_login_csrf=")
        for flag in ("HttpOnly", "Secure", "SameSite=strict", "Path=/admin"):
            assert flag in cookie

    async def test_success_sets_hardened_session_cookie(self, admin_env, client):
        response = await _login(client, "admin", PASSWORD)
        assert response.status_code == 303
        assert response.headers["location"] == "/admin/novelties"
        session_cookie = next(
            value for value in response.headers.get_list("set-cookie") if value.startswith("admin_session=")
        )
        for flag in ("HttpOnly", "Secure", "SameSite=strict", "Path=/admin", "Max-Age=43200"):
            assert flag in session_cookie
        token = session_cookie.split(";", 1)[0].split("=", 1)[1]
        assert decode_session_token(admin_env.settings, token).admin_id == admin_env.admin.id
        assert admin_env.admin.last_login_at is not None
        assert any(
            value.startswith("admin_login_csrf=") and "Max-Age=0" in value
            for value in response.headers.get_list("set-cookie")
        )

    @pytest.mark.parametrize(("login", "password"), [("admin", "wrong"), ("nobody", PASSWORD), ("", "")])
    async def test_rejected_credentials(self, admin_env, client, login, password):
        if login != "admin":
            admin_env.db.scalar_result = None
        response = await _login(client, login, password)
        assert response.status_code == 401
        assert "Неверный логин или пароль" in response.text
        assert "admin_session=" not in response.headers.get("set-cookie", "")

    async def test_inactive_admin_cannot_log_in(self, admin_env, client):
        admin_env.admin.is_active = False
        assert (await _login(client, "admin", PASSWORD)).status_code == 401

    async def test_missing_login_csrf(self, admin_env, client):
        response = await client.post("/admin/login", data={"login": "admin", "password": PASSWORD})
        assert response.status_code == 403
        assert admin_env.admin.last_login_at is None

    async def test_throttled_after_five_failures_per_ip(self, admin_env, client):
        for _ in range(5):
            assert (await _login(client, "admin", "wrong", ip="203.0.113.1")).status_code == 401
        blocked = await _login(client, "admin", PASSWORD, ip="203.0.113.1")
        assert blocked.status_code == 429
        assert "admin_session=" not in blocked.headers.get("set-cookie", "")
        assert (await _login(client, "admin", PASSWORD, ip="203.0.113.2")).status_code == 303

    async def test_success_resets_failures(self, admin_env, client):
        for _ in range(4):
            await _login(client, "admin", "wrong")
        assert (await _login(client, "admin", PASSWORD)).status_code == 303
        for _ in range(4):
            await _login(client, "admin", "wrong")
        assert (await _login(client, "admin", PASSWORD)).status_code == 303

    async def test_parallel_burst_gets_only_five_attempts(self, admin_env, client, monkeypatch):
        verifier = SlowVerifier()
        monkeypatch.setattr(web, "verify_password_or_decoy", verifier)
        monkeypatch.setattr(web, "password_checks", asyncio.Semaphore(100))
        statuses = await _burst(client, 12, ["203.0.113.9"])
        assert verifier.calls == 5
        assert statuses == [401] * 5 + [429] * 7

    async def test_concurrent_password_checks_are_capped(self, admin_env, client, monkeypatch):
        verifier = SlowVerifier()
        monkeypatch.setattr(web, "verify_password_or_decoy", verifier)
        statuses = await _burst(client, 6, [f"203.0.113.{index}" for index in range(10, 16)])
        assert verifier.peak <= 2
        assert 429 in statuses
        assert set(statuses) <= {401, 429}

    async def test_busy_server_rejects_without_using_attempts(self, admin_env, client, monkeypatch):
        verifier = SlowVerifier(delay=0)
        monkeypatch.setattr(web, "verify_password_or_decoy", verifier)
        monkeypatch.setattr(web, "password_checks", asyncio.Semaphore(0))
        for _ in range(6):
            busy = await _login(client, "admin", PASSWORD)
            assert busy.status_code == 429
            assert "Сервер занят" in busy.text
        assert verifier.calls == 0
        monkeypatch.undo()
        assert (await _login(client, "admin", PASSWORD)).status_code == 303

    async def test_pages_use_new_brand(self, admin_env, client):
        page = await client.get("/admin/login")
        assert "Пекарня" in page.text
        assert "Маковка" not in page.text
        assert app.title == "Пекарня API"


class TestLogout:
    async def test_clears_session_cookie(self, admin_env, client):
        headers, csrf = admin_env.auth()
        response = await client.post("/admin/logout", headers=headers, data={"csrf_token": csrf})
        assert response.status_code == 303
        assert response.headers["location"] == "/admin/login"
        cookie = response.headers["set-cookie"]
        assert cookie.startswith("admin_session=") and "Max-Age=0" in cookie and "Path=/admin" in cookie

    async def test_logout_invalidates_existing_session_tokens(self, admin_env, client):
        headers, csrf = admin_env.auth()
        other_headers, _ = admin_env.auth()
        response = await client.post("/admin/logout", headers=headers, data={"csrf_token": csrf})
        assert response.status_code == 303
        assert admin_env.admin.session_version == 1
        assert admin_env.db.commits == 1
        assert (await client.get("/admin/settings", headers=headers)).status_code == 303
        assert (await client.get("/admin/settings", headers=other_headers)).status_code == 303
        fresh_headers, _ = admin_env.auth()
        assert (await client.get("/admin/settings", headers=fresh_headers)).status_code == 200


class TestPages:
    async def test_list_page_escapes_content(self, admin_env, client):
        admin_env.db.listed = [_novelty(title="<script>alert(1)</script>")]
        headers, _ = admin_env.auth()
        response = await client.get("/admin/novelties", headers=headers)
        assert response.status_code == 200
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in response.text
        assert "<script>alert(1)" not in response.text

    async def test_unknown_item_is_404(self, admin_env, client):
        headers, _ = admin_env.auth()
        assert (await client.get(f"/admin/novelties/{uuid.uuid4()}", headers=headers)).status_code == 404
        assert (await client.get("/admin/novelties/not-a-uuid", headers=headers)).status_code == 404


class TestUploads:
    async def test_create_with_valid_image(self, admin_env, client):
        headers, csrf = admin_env.auth()
        response = await client.post(
            "/admin/novelties/new",
            headers=headers,
            data={"csrf_token": csrf, "title": "Круассан", "sort_order": "2", "is_active": "on"},
            files={"image": ("photo.png", PNG, "image/png")},
        )
        assert response.status_code == 303
        (created,) = admin_env.db.added
        assert re.fullmatch(r"[a-f0-9]{32}\.png", created.image)
        assert admin_env.media_files() == [created.image]
        assert (created.title, created.sort_order, created.is_active) == ("Круассан", 2, True)

    @pytest.mark.parametrize(
        ("content", "message"),
        [
            (b"GIF89a" + b"\x00" * 16, "PNG, JPEG или WebP"),
            (b"<svg xmlns='http://www.w3.org/2000/svg'/>", "PNG, JPEG или WebP"),
            (PNG + b"\x00" * 2048, "больше"),
        ],
    )
    async def test_rejected_upload_writes_nothing(self, admin_env, client, content, message):
        headers, csrf = admin_env.auth()
        response = await client.post(
            "/admin/novelties/new",
            headers=headers,
            data={"csrf_token": csrf, "title": "<b>Круассан</b>"},
            files={"image": ("photo.png", content, "image/png")},
        )
        assert response.status_code == 400
        assert message in response.text
        assert 'value="&lt;b&gt;Круассан&lt;/b&gt;"' in response.text
        assert admin_env.media_files() == []
        assert admin_env.db.added == []
        assert admin_env.db.commits == 0

    async def test_replacing_image_removes_old_file(self, admin_env, client):
        media = admin_env.media
        (media / OLD_IMAGE).write_bytes(PNG)
        (media / "seed_pizza_bbq.png").write_bytes(PNG)
        item = _novelty(image=OLD_IMAGE)
        admin_env.db.put(item)
        headers, csrf = admin_env.auth()
        response = await client.post(
            f"/admin/novelties/{item.id}",
            headers=headers,
            data={"csrf_token": csrf, "title": "Круассан"},
            files={"image": ("new.png", PNG, "image/png")},
        )
        assert response.status_code == 303
        assert item.image != OLD_IMAGE
        assert admin_env.media_files() == sorted([item.image, "seed_pizza_bbq.png"])

    async def test_seed_image_survives_replacement(self, admin_env, client):
        media = admin_env.media
        (media / "seed_pizza_bbq.png").write_bytes(PNG)
        item = _novelty(image="seed_pizza_bbq.png")
        admin_env.db.put(item)
        headers, csrf = admin_env.auth()
        await client.post(
            f"/admin/novelties/{item.id}",
            headers=headers,
            data={"csrf_token": csrf, "title": "Круассан", "remove_image": "on"},
        )
        assert item.image is None
        assert admin_env.media_files() == ["seed_pizza_bbq.png"]

    async def test_delete_requires_confirmation_and_removes_file(self, admin_env, client):
        media = admin_env.media
        (media / OLD_IMAGE).write_bytes(PNG)
        item = _novelty(image=OLD_IMAGE)
        admin_env.db.put(item)
        headers, csrf = admin_env.auth()
        response = await client.post(f"/admin/novelties/{item.id}/delete", headers=headers, data={"csrf_token": csrf})
        assert response.headers["location"] == f"/admin/novelties/{item.id}?msg=confirm"
        assert admin_env.db.deleted == []
        response = await client.post(
            f"/admin/novelties/{item.id}/delete", headers=headers, data={"csrf_token": csrf, "confirm": "on"}
        )
        assert response.headers["location"] == "/admin/novelties?msg=deleted"
        assert admin_env.db.deleted == [item]
        assert admin_env.media_files() == []


class TestSettingsPage:
    @pytest.fixture
    def saved(self, monkeypatch):
        calls: list = []
        rows: dict[str, AppSetting] = {"coffee_stamp_goal": AppSetting(key="coffee_stamp_goal", value=9)}

        async def load_override_rows(db):
            return rows

        async def save_overrides(db, updates, removals, updated_by):
            calls.append((dict(updates), list(removals), updated_by))

        monkeypatch.setattr(web, "load_override_rows", load_override_rows)
        monkeypatch.setattr(web, "save_overrides", save_overrides)
        return calls

    @staticmethod
    def _inherit_all(csrf: str) -> dict[str, str]:
        return {"csrf_token": csrf, **{f"inherit_{key}": "on" for key in OVERRIDABLE_KEYS}}

    async def test_saves_only_changes(self, admin_env, client, saved):
        headers, csrf = admin_env.auth()
        data = self._inherit_all(csrf)
        data.update(
            {
                "inherit_coffee_stamp_goal": "",
                "value_coffee_stamp_goal": "9",
                "inherit_referral_rewards_enabled": "",
                "value_referral_rewards_enabled": "true",
                "value_iiko_env": "prod",
            }
        )
        response = await client.post("/admin/settings", headers=headers, data=data)
        assert response.status_code == 303
        assert saved == [({"referral_rewards_enabled": True}, [], "admin")]

    async def test_edited_value_is_saved_even_if_inherit_stays_checked(self, admin_env, client, saved):
        headers, csrf = admin_env.auth()
        data = self._inherit_all(csrf)
        data.update(
            {
                "inherit_coffee_stamp_goal": "",
                "value_coffee_stamp_goal": "9",
                "value_referral_rewards_enabled": "true",
                "value_loyalty_flat_discount_percent": "10",
                "value_coffee_stamp_wallet_id": "",
            }
        )
        response = await client.post("/admin/settings", headers=headers, data=data)
        assert response.status_code == 303
        assert saved == [({"referral_rewards_enabled": True}, [], "admin")]

    async def test_edited_invalid_value_with_inherit_checked_is_reported(self, admin_env, client, saved):
        headers, csrf = admin_env.auth()
        data = self._inherit_all(csrf)
        data.update({"inherit_coffee_stamp_goal": "", "value_coffee_stamp_goal": "9"})
        data["value_loyalty_flat_discount_percent"] = "150"
        response = await client.post("/admin/settings", headers=headers, data=data)
        assert response.status_code == 400
        assert "не больше 100" in response.text
        assert saved == []

    async def test_page_shows_stored_override_not_cached_settings(self, admin_env, client, saved):
        headers, _ = admin_env.auth()
        response = await client.get("/admin/settings", headers=headers)
        assert 'name="value_coffee_stamp_goal" value="9"' in response.text
        assert 'name="inherit_coffee_stamp_goal">' in response.text
        assert 'name="inherit_referral_rewards_enabled" checked>' in response.text

    async def test_coffee_wallet_cannot_be_the_bonus_wallet(self, admin_env, client, saved):
        settings = admin_env.settings.model_copy(update={"loyalty_bonus_wallet_id": CATEGORY.upper()})
        admin_app.dependency_overrides[get_settings] = lambda: settings
        headers, csrf = admin_env.auth()
        data = self._inherit_all(csrf)
        data.update(
            {
                "inherit_coffee_stamp_goal": "",
                "value_coffee_stamp_goal": "9",
                "inherit_coffee_stamp_wallet_id": "",
                "value_coffee_stamp_wallet_id": CATEGORY.replace("-", ""),
            }
        )
        response = await client.post("/admin/settings", headers=headers, data=data)
        assert response.status_code == 400
        assert "бонусный кошелёк" in response.text
        assert saved == []

    async def test_inherit_removes_override(self, admin_env, client, saved):
        headers, csrf = admin_env.auth()
        response = await client.post("/admin/settings", headers=headers, data=self._inherit_all(csrf))
        assert response.status_code == 303
        assert saved == [({}, ["coffee_stamp_goal"], "admin")]

    async def test_invalid_values_are_not_saved(self, admin_env, client, saved):
        headers, csrf = admin_env.auth()
        data = self._inherit_all(csrf)
        data.update({"inherit_loyalty_flat_discount_percent": "", "value_loyalty_flat_discount_percent": "150"})
        response = await client.post("/admin/settings", headers=headers, data=data)
        assert response.status_code == 400
        assert "не больше 100" in response.text
        assert saved == []

    async def test_page_shows_iiko_env_read_only(self, admin_env, client, saved):
        headers, _ = admin_env.auth()
        response = await client.get("/admin/settings", headers=headers)
        assert response.status_code == 200
        assert "IIKO_ENV=prod" in response.text
        assert 'name="value_iiko_env"' not in response.text
        assert 'name="value_iiko_mutations_enabled"' not in response.text


class CategoriesIiko:
    def __init__(self, result):
        self.result = result
        self.calls = 0

    async def list_customer_categories(self, db=None):
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _tier() -> LoyaltyTier:
    return LoyaltyTier(
        id=uuid.uuid4(),
        code="silver",
        name="Серебро",
        min_spend=Decimal("5000"),
        discount_percent=Decimal("15"),
        cashback_percent=Decimal("5"),
        perks=["Десерт"],
        iiko_category_id=CATEGORY,
        sort_order=1,
        is_active=True,
    )


class TestTiers:
    @pytest.fixture
    def tier(self, admin_env):
        tier = _tier()
        admin_env.db.put(tier)
        return tier

    def _use_iiko(self, admin_env, iiko, **overrides):
        settings = admin_env.settings.model_copy(update=overrides)
        admin_app.dependency_overrides[get_settings] = lambda: settings
        admin_app.dependency_overrides[get_admin_iiko] = lambda: iiko

    async def test_category_picker_from_live_list(self, admin_env, client, tier):
        iiko = CategoriesIiko(
            [{"id": CATEGORY, "name": "Серебряные гости"}, {"id": OTHER_CATEGORY, "name": "Золото"}]
        )
        self._use_iiko(admin_env, iiko)
        headers, _ = admin_env.auth()
        response = await client.get(f"/admin/tiers/{tier.id}", headers=headers)
        assert response.status_code == 200
        assert f'<option value="{CATEGORY}" selected>Серебряные гости</option>' in response.text
        assert f'<option value="{OTHER_CATEGORY}">Золото</option>' in response.text
        assert iiko.calls == 1

    async def test_manual_fallback_when_iiko_fails(self, admin_env, client, tier):
        self._use_iiko(admin_env, CategoriesIiko(IikoClientError("iiko responded 401", 401)))
        headers, _ = admin_env.auth()
        response = await client.get(f"/admin/tiers/{tier.id}", headers=headers)
        assert "Не удалось загрузить категории из iiko" in response.text
        assert f'name="iiko_category_manual" value="{CATEGORY}"' in response.text

    async def test_not_configured_iiko_is_not_called(self, admin_env, client, tier):
        iiko = CategoriesIiko([])
        self._use_iiko(admin_env, iiko, iiko_api_login="")
        headers, _ = admin_env.auth()
        response = await client.get(f"/admin/tiers/{tier.id}", headers=headers)
        assert "Интеграция с iiko не настроена" in response.text
        assert iiko.calls == 0

    async def test_update(self, admin_env, client, tier):
        headers, csrf = admin_env.auth()
        response = await client.post(
            f"/admin/tiers/{tier.id}",
            headers=headers,
            data={
                "csrf_token": csrf,
                "name": "Серебро+",
                "min_spend": "6000",
                "discount_percent": "16",
                "cashback_percent": "6",
                "perks": "Десерт\nКофе",
                "iiko_category_id": CATEGORY,
                "iiko_category_manual": OTHER_CATEGORY,
                "is_active": "on",
            },
        )
        assert response.status_code == 303
        assert (tier.name, tier.min_spend, tier.perks, tier.iiko_category_id) == (
            "Серебро+",
            Decimal("6000.00"),
            ["Десерт", "Кофе"],
            OTHER_CATEGORY,
        )
        assert tier.code == "silver"

    @staticmethod
    def _form(csrf: str, **fields) -> dict[str, str]:
        data = {
            "csrf_token": csrf,
            "name": "Серебро",
            "min_spend": "5000",
            "discount_percent": "15",
            "cashback_percent": "5",
            "perks": "",
            "iiko_category_id": CATEGORY,
            "iiko_category_manual": "",
            "is_active": "on",
        }
        data.update(fields)
        return data

    @staticmethod
    def _gold(**fields) -> LoyaltyTier:
        values = {
            "id": uuid.uuid4(),
            "code": "gold",
            "name": "Золото",
            "min_spend": Decimal("10000"),
            "discount_percent": Decimal("20"),
            "cashback_percent": Decimal("0"),
            "perks": [],
            "iiko_category_id": OTHER_CATEGORY,
            "sort_order": 2,
            "is_active": True,
        }
        values.update(fields)
        return LoyaltyTier(**values)

    async def test_duplicate_threshold_of_active_tier_is_rejected(self, admin_env, client, tier):
        admin_env.db.listed = [tier, self._gold()]
        headers, csrf = admin_env.auth()
        response = await client.post(f"/admin/tiers/{tier.id}", headers=headers, data=self._form(csrf, min_spend="10000"))
        assert response.status_code == 400
        assert "такая же сумма покупок" in response.text
        assert tier.min_spend == Decimal("5000")
        assert admin_env.db.commits == 0

    async def test_same_threshold_as_inactive_tier_is_allowed(self, admin_env, client, tier):
        admin_env.db.listed = [tier, self._gold(is_active=False)]
        headers, csrf = admin_env.auth()
        response = await client.post(f"/admin/tiers/{tier.id}", headers=headers, data=self._form(csrf, min_spend="10000"))
        assert response.status_code == 303
        assert tier.min_spend == Decimal("10000.00")

    async def test_category_of_another_tier_is_rejected(self, admin_env, client, tier):
        admin_env.db.listed = [tier, self._gold()]
        headers, csrf = admin_env.auth()
        response = await client.post(
            f"/admin/tiers/{tier.id}",
            headers=headers,
            data=self._form(csrf, iiko_category_manual=OTHER_CATEGORY.upper().replace("-", "")),
        )
        assert response.status_code == 400
        assert "уже назначена другому уровню" in response.text
        assert tier.iiko_category_id == CATEGORY

    async def test_category_is_stored_as_canonical_uuid(self, admin_env, client, tier):
        headers, csrf = admin_env.auth()
        manual = "{" + OTHER_CATEGORY + "}"
        rejected = await client.post(
            f"/admin/tiers/{tier.id}", headers=headers, data=self._form(csrf, iiko_category_manual=manual)
        )
        assert rejected.status_code == 400
        response = await client.post(
            f"/admin/tiers/{tier.id}",
            headers=headers,
            data=self._form(csrf, iiko_category_manual=OTHER_CATEGORY.upper().replace("-", "")),
        )
        assert response.status_code == 303
        assert tier.iiko_category_id == OTHER_CATEGORY

    async def test_invalid_update_changes_nothing(self, admin_env, client, tier):
        headers, csrf = admin_env.auth()
        response = await client.post(
            f"/admin/tiers/{tier.id}",
            headers=headers,
            data={"csrf_token": csrf, "name": "", "min_spend": "-5", "discount_percent": "200", "cashback_percent": "0"},
        )
        assert response.status_code == 400
        assert tier.name == "Серебро"
        assert admin_env.db.commits == 0
