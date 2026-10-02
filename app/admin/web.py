from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import FormData, UploadFile
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ..config import Settings, get_settings
from ..db import get_db
from ..iiko.client import IikoClientError, IikoCloudClient
from ..media import ImageRejected, media_url, remove_image, store_image, validate_image
from ..models import AdminUser, Bakery, LoyaltyTier, Novelty, Promotion
from ..runtime_settings import (
    OVERRIDABLE_SETTINGS,
    SettingValueError,
    get_effective_settings,
    load_override_rows,
    save_overrides,
    validate_override,
    validate_setting,
)
from .forms import (
    AUDIENCE_LABELS,
    PROMOTION_KIND_LABELS,
    form_snapshot,
    model_snapshot,
    parse_bakery,
    parse_novelty,
    parse_promotion,
    parse_tier,
    tier_conflicts,
)
from .queries import (
    GUESTS_PAGE_SIZE,
    count_audience,
    list_guests,
    load_tiers,
    move_item,
    next_sort_order,
    ordered_items,
    search_digits,
)
from .rendering import templates
from .security import (
    COOKIE_PATH,
    LOGIN_CSRF_COOKIE,
    LOGIN_CSRF_TTL,
    SESSION_COOKIE,
    SESSION_TTL,
    AdminSession,
    client_ip,
    create_session_token,
    csrf_token,
    decode_session_token,
    login_csrf_token,
    login_throttle,
    new_login_nonce,
    password_checks,
    session_fingerprint,
    verify_csrf,
    verify_login_csrf,
    verify_password_or_decoy,
)

PREFIX = "/admin"
BASE_DIR = Path(__file__).resolve().parent
IIKO_CATEGORIES_TIMEOUT_SECONDS = 10

CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'none'; style-src 'self' 'unsafe-inline'; img-src 'self' https: data:; "
    "font-src 'self'; connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'; "
    "object-src 'none'"
)
SECURITY_HEADERS = {
    "x-frame-options": "DENY",
    "content-security-policy": CONTENT_SECURITY_POLICY,
    "cache-control": "no-store",
    "referrer-policy": "same-origin",
    "x-content-type-options": "nosniff",
}
_ENCODED_SECURITY_HEADERS = [(name.encode(), value.encode()) for name, value in SECURITY_HEADERS.items()]

_AUDIENCE_FIELDS = {"audience", "audience_phones", "audience_tiers"}

FLASH_MESSAGES = {
    "saved": "Изменения сохранены",
    "deleted": "Запись удалена",
    "confirm": "Отметьте подтверждение, чтобы удалить запись",
}


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = [
                    (name, value)
                    for name, value in message.get("headers", [])
                    if name.decode("latin-1").lower() not in SECURITY_HEADERS
                ]
                message = {**message, "headers": headers + _ENCODED_SECURITY_HEADERS}
            await send(message)

        await self.app(scope, receive, send_with_headers)


class AdminAuthRequired(Exception):
    pass


class CsrfRejected(Exception):
    pass


admin_app = FastAPI(title="Пекарня — админка", docs_url=None, redoc_url=None, openapi_url=None)
admin_app.add_middleware(SecurityHeadersMiddleware)
admin_app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


def _redirect(path: str) -> RedirectResponse:
    return RedirectResponse(f"{PREFIX}{path}", status_code=303)


def _render(request: Request, template: str, context: dict[str, Any], status_code: int = 200) -> HTMLResponse:
    return templates.TemplateResponse(request, template, context, status_code=status_code)


@admin_app.exception_handler(AdminAuthRequired)
async def _auth_required(request: Request, exc: AdminAuthRequired) -> Response:
    return _redirect("/login")


@admin_app.exception_handler(CsrfRejected)
async def _csrf_rejected(request: Request, exc: CsrfRejected) -> Response:
    return _render(
        request,
        "message.html",
        {
            "page": None,
            "message": None,
            "title": "Запрос отклонён",
            "text": "Форма устарела или отправлена не из админки. Обновите страницу и попробуйте ещё раз.",
        },
        status_code=403,
    )


@dataclass
class AdminContext:
    admin: AdminUser
    session: AdminSession
    csrf: str

    def page(self, section: str, request: Request, **extra: Any) -> dict[str, Any]:
        return {
            "page": {"admin": self.admin, "csrf": self.csrf, "section": section},
            "message": FLASH_MESSAGES.get(request.query_params.get("msg", "")),
            **extra,
        }


@dataclass
class AdminForm:
    context: AdminContext
    data: FormData


async def require_admin(
    request: Request,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> AdminContext:
    token = request.cookies.get(SESSION_COOKIE)
    session = decode_session_token(settings, token) if token else None
    if session is None:
        raise AdminAuthRequired()
    admin = await db.get(AdminUser, session.admin_id)
    if admin is None or not admin.is_active:
        raise AdminAuthRequired()
    if session_fingerprint(admin.password_hash, admin.session_version) != session.fingerprint:
        raise AdminAuthRequired()
    return AdminContext(admin=admin, session=session, csrf=csrf_token(settings, session.session_id))


async def require_admin_form(
    request: Request,
    context: AdminContext = Depends(require_admin),
    settings: Settings = Depends(get_settings),
) -> AdminForm:
    form = await request.form(max_files=1, max_fields=200)
    if not verify_csrf(settings, context.session.session_id, form.get("csrf_token")):
        raise CsrfRejected()
    return AdminForm(context=context, data=form)


def get_admin_iiko(request: Request) -> IikoCloudClient | None:
    return getattr(request.app.state, "iiko_client", None)


def _parse_id(raw: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(raw)
    except ValueError:
        return None


def _not_found(request: Request, context: AdminContext, section: str) -> HTMLResponse:
    return _render(
        request,
        "message.html",
        context.page(section, request, title="Не найдено", text="Запись не найдена или уже удалена."),
        status_code=404,
    )


def _login_page(request: Request, settings: Settings, error: str | None, status_code: int) -> HTMLResponse:
    nonce = new_login_nonce()
    response = _render(
        request,
        "login.html",
        {"page": None, "message": None, "csrf": login_csrf_token(settings, nonce), "error": error},
        status_code=status_code,
    )
    response.set_cookie(
        LOGIN_CSRF_COOKIE,
        nonce,
        max_age=int(LOGIN_CSRF_TTL.total_seconds()),
        path=COOKIE_PATH,
        secure=True,
        httponly=True,
        samesite="strict",
    )
    return response


@admin_app.get("/", include_in_schema=False)
async def admin_home(context: AdminContext = Depends(require_admin)) -> Response:
    return _redirect("/novelties")


@admin_app.get("/login", response_class=HTMLResponse)
async def login_form(request: Request, settings: Settings = Depends(get_settings)) -> Response:
    return _login_page(request, settings, None, 200)


@admin_app.post("/login", response_class=HTMLResponse)
async def login_submit(
    request: Request,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> Response:
    form = await request.form(max_files=0, max_fields=10)
    if not verify_login_csrf(settings, request.cookies.get(LOGIN_CSRF_COOKIE), form.get("csrf_token")):
        return _login_page(request, settings, "Форма устарела, попробуйте ещё раз.", 403)
    ip = client_ip(request)
    if password_checks.locked():
        return _login_page(request, settings, "Сервер занят, попробуйте войти через минуту.", 429)
    if not login_throttle.reserve_attempt(ip):
        return _login_page(request, settings, "Слишком много неудачных попыток. Попробуйте через 15 минут.", 429)
    async with password_checks:
        login = str(form.get("login") or "").strip()[:64]
        password = str(form.get("password") or "")[:256]
        admin = await db.scalar(select(AdminUser).where(AdminUser.login == login)) if login else None
        valid = await run_in_threadpool(verify_password_or_decoy, password, admin.password_hash if admin else None)
    if admin is None or not valid or not admin.is_active:
        return _login_page(request, settings, "Неверный логин или пароль", 401)

    login_throttle.reset(ip)
    admin.last_login_at = datetime.now(timezone.utc)
    await db.commit()
    response = _redirect("/novelties")
    response.set_cookie(
        SESSION_COOKIE,
        create_session_token(settings, admin.id, session_fingerprint(admin.password_hash, admin.session_version)),
        max_age=int(SESSION_TTL.total_seconds()),
        path=COOKIE_PATH,
        secure=True,
        httponly=True,
        samesite="strict",
    )
    response.delete_cookie(LOGIN_CSRF_COOKIE, path=COOKIE_PATH, secure=True, httponly=True, samesite="strict")
    return response


@admin_app.post("/logout")
async def logout(form: AdminForm = Depends(require_admin_form), db: AsyncSession = Depends(get_db)) -> Response:
    form.context.admin.session_version += 1
    await db.commit()
    response = _redirect("/login")
    response.delete_cookie(SESSION_COOKIE, path=COOKIE_PATH, secure=True, httponly=True, samesite="strict")
    return response


Parser = Callable[[FormData, AsyncSession], Awaitable[tuple[dict[str, Any], dict[str, str]]]]
Extras = Callable[[AsyncSession, Settings, Mapping[str, Any] | None], Awaitable[dict[str, Any]]]


async def _no_extras(db: AsyncSession, settings: Settings, values: Mapping[str, Any] | None) -> dict[str, Any]:
    return {}


@dataclass(frozen=True)
class Section:
    slug: str
    model: type[Novelty] | type[Promotion] | type[Bakery]
    title: str
    new_title: str
    edit_title: str
    list_template: str
    form_template: str
    parse: Parser
    snapshot: Callable[[Any], dict[str, Any]]
    has_image: bool = False
    multi_fields: tuple[str, ...] = ()
    extras: Extras = _no_extras
    new_defaults: dict[str, Any] = field(default_factory=dict)


async def _parse_novelty(form: FormData, db: AsyncSession) -> tuple[dict[str, Any], dict[str, str]]:
    return parse_novelty(form)


async def _parse_bakery(form: FormData, db: AsyncSession) -> tuple[dict[str, Any], dict[str, str]]:
    return parse_bakery(form)


async def _parse_promotion(form: FormData, db: AsyncSession) -> tuple[dict[str, Any], dict[str, str]]:
    return parse_promotion(form, [tier.code for tier in await load_tiers(db)])


def _novelty_snapshot(item: Novelty) -> dict[str, Any]:
    return model_snapshot({"title": item.title, "sort_order": item.sort_order, "is_active": item.is_active})


def _bakery_snapshot(item: Bakery) -> dict[str, Any]:
    return model_snapshot(
        {
            "name": item.name,
            "address": item.address,
            "metro": item.metro,
            "hours_text": item.hours_text,
            "latitude": item.latitude,
            "longitude": item.longitude,
            "sort_order": item.sort_order,
            "is_active": item.is_active,
        }
    )


def _promotion_snapshot(item: Promotion) -> dict[str, Any]:
    return model_snapshot(
        {
            "kind": item.kind,
            "title": item.title,
            "subtitle": item.subtitle,
            "description": item.description,
            "badge": item.badge,
            "price": item.price,
            "old_price": item.old_price,
            "time_text": item.time_text,
            "starts_at": item.starts_at,
            "ends_at": item.ends_at,
            "show_on_home": item.show_on_home,
            "sort_order": item.sort_order,
            "is_active": item.is_active,
            "audience": item.audience,
            "audience_phones": "\n".join(item.audience_phones or []),
            "audience_tiers": list(item.audience_tiers or []),
        }
    )


def _promotion_audience(item: Promotion) -> dict[str, Any]:
    return {
        "audience": item.audience,
        "audience_phones": list(item.audience_phones or []),
        "audience_tiers": list(item.audience_tiers or []),
    }


async def _promotion_extras(
    db: AsyncSession, settings: Settings, values: Mapping[str, Any] | None
) -> dict[str, Any]:
    preview = None
    if values is not None:
        preview = await count_audience(
            db,
            values["audience"],
            values["audience_phones"],
            values["audience_tiers"],
            tiers_enabled=settings.loyalty_tiers_enabled,
        )
    return {
        "tiers": await load_tiers(db),
        "kind_labels": PROMOTION_KIND_LABELS,
        "audience_labels": AUDIENCE_LABELS,
        "tiers_enabled": settings.loyalty_tiers_enabled,
        "preview": preview,
    }


SECTIONS = (
    Section(
        slug="novelties",
        model=Novelty,
        title="Новинки",
        new_title="Новая позиция",
        edit_title="Редактирование новинки",
        list_template="novelties.html",
        form_template="novelty_form.html",
        parse=_parse_novelty,
        snapshot=_novelty_snapshot,
        has_image=True,
        new_defaults={"is_active": "on"},
    ),
    Section(
        slug="promotions",
        model=Promotion,
        title="Акции и персональные предложения",
        new_title="Новая акция",
        edit_title="Редактирование акции",
        list_template="promotions.html",
        form_template="promotion_form.html",
        parse=_parse_promotion,
        snapshot=_promotion_snapshot,
        has_image=True,
        multi_fields=("audience_tiers",),
        extras=_promotion_extras,
        new_defaults={"is_active": "on", "show_on_home": "on", "kind": "generic", "audience": "all"},
    ),
    Section(
        slug="bakeries",
        model=Bakery,
        title="Пекарни",
        new_title="Новая пекарня",
        edit_title="Редактирование пекарни",
        list_template="bakeries.html",
        form_template="bakery_form.html",
        parse=_parse_bakery,
        snapshot=_bakery_snapshot,
        new_defaults={"is_active": "on"},
    ),
)


async def _render_form(
    request: Request,
    context: AdminContext,
    db: AsyncSession,
    settings: Settings,
    effective: Settings,
    section: Section,
    item: Any,
    data: dict[str, Any],
    errors: dict[str, str],
    preview_values: Mapping[str, Any] | None,
    status_code: int = 200,
) -> HTMLResponse:
    extras = await section.extras(db, effective, preview_values)
    image = getattr(item, "image", None) if section.has_image else None
    return _render(
        request,
        section.form_template,
        context.page(
            section.slug,
            request,
            section_info=section,
            item=item,
            data=data,
            errors=errors,
            image_url=media_url(settings, image),
            max_upload_mb=settings.media_max_bytes // (1024 * 1024),
            **extras,
        ),
        status_code=status_code,
    )


async def _read_upload(form: FormData, settings: Settings) -> tuple[bytes | None, str | None]:
    upload = form.get("image")
    if not isinstance(upload, UploadFile) or not upload.filename:
        return None, None
    data = await upload.read(settings.media_max_bytes + 1)
    try:
        validate_image(data, settings.media_max_bytes)
    except ImageRejected as exc:
        return None, str(exc)
    return data, None


async def _save_item(
    request: Request,
    form: AdminForm,
    db: AsyncSession,
    settings: Settings,
    effective: Settings,
    section: Section,
    item: Any,
) -> Response:
    values, errors = await section.parse(form.data, db)
    image_bytes: bytes | None = None
    if section.has_image:
        image_bytes, image_error = await _read_upload(form.data, settings)
        if image_error:
            errors["image"] = image_error
    preview_only = form.data.get("action") == "preview"
    if errors or preview_only:
        preview_values = None if errors.keys() & _AUDIENCE_FIELDS else values
        if errors and not preview_only:
            errors = {**errors, "form": "Исправьте ошибки в форме"}
        return await _render_form(
            request,
            form.context,
            db,
            settings,
            effective,
            section,
            item,
            form_snapshot(form.data, section.multi_fields),
            errors,
            preview_values,
            status_code=400 if errors else 200,
        )

    old_image = item.image if section.has_image and item is not None else None
    new_image = await run_in_threadpool(store_image, settings, image_bytes) if image_bytes else None
    if item is None:
        item = section.model()
        db.add(item)
    for key, value in values.items():
        setattr(item, key, value)
    if section.has_image:
        if new_image:
            item.image = new_image
        elif form.data.get("remove_image") == "on":
            item.image = None
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        remove_image(settings, new_image)
        raise
    if old_image and old_image != item.image:
        remove_image(settings, old_image)
    return _redirect(f"/{section.slug}?msg=saved")


def _register_section(section: Section) -> None:
    base = f"/{section.slug}"

    @admin_app.get(base, response_class=HTMLResponse, name=f"{section.slug}_list")
    async def list_items(
        request: Request,
        context: AdminContext = Depends(require_admin),
        db: AsyncSession = Depends(get_db),
        settings: Settings = Depends(get_settings),
        effective: Settings = Depends(get_effective_settings),
    ) -> Response:
        items = await ordered_items(db, section.model)
        return _render(
            request,
            section.list_template,
            context.page(
                section.slug,
                request,
                section_info=section,
                items=items,
                image_urls={item.id: media_url(settings, getattr(item, "image", None)) for item in items},
                kind_labels=PROMOTION_KIND_LABELS,
                audience_labels=AUDIENCE_LABELS,
                tiers_enabled=effective.loyalty_tiers_enabled,
            ),
        )

    @admin_app.get(f"{base}/new", response_class=HTMLResponse, name=f"{section.slug}_new")
    async def new_item(
        request: Request,
        context: AdminContext = Depends(require_admin),
        db: AsyncSession = Depends(get_db),
        settings: Settings = Depends(get_settings),
        effective: Settings = Depends(get_effective_settings),
    ) -> Response:
        data = {**section.new_defaults, "sort_order": str(await next_sort_order(db, section.model))}
        return await _render_form(request, context, db, settings, effective, section, None, data, {}, None)

    @admin_app.post(f"{base}/new", response_class=HTMLResponse, name=f"{section.slug}_create")
    async def create_item(
        request: Request,
        form: AdminForm = Depends(require_admin_form),
        db: AsyncSession = Depends(get_db),
        settings: Settings = Depends(get_settings),
        effective: Settings = Depends(get_effective_settings),
    ) -> Response:
        return await _save_item(request, form, db, settings, effective, section, None)

    @admin_app.get(f"{base}/{{item_id}}", response_class=HTMLResponse, name=f"{section.slug}_edit")
    async def edit_item(
        request: Request,
        item_id: str,
        context: AdminContext = Depends(require_admin),
        db: AsyncSession = Depends(get_db),
        settings: Settings = Depends(get_settings),
        effective: Settings = Depends(get_effective_settings),
    ) -> Response:
        parsed = _parse_id(item_id)
        item = await db.get(section.model, parsed) if parsed else None
        if item is None:
            return _not_found(request, context, section.slug)
        preview_values = _promotion_audience(item) if isinstance(item, Promotion) else None
        return await _render_form(
            request, context, db, settings, effective, section, item, section.snapshot(item), {}, preview_values
        )

    @admin_app.post(f"{base}/{{item_id}}", response_class=HTMLResponse, name=f"{section.slug}_update")
    async def update_item(
        request: Request,
        item_id: str,
        form: AdminForm = Depends(require_admin_form),
        db: AsyncSession = Depends(get_db),
        settings: Settings = Depends(get_settings),
        effective: Settings = Depends(get_effective_settings),
    ) -> Response:
        parsed = _parse_id(item_id)
        item = await db.get(section.model, parsed) if parsed else None
        if item is None:
            return _not_found(request, form.context, section.slug)
        return await _save_item(request, form, db, settings, effective, section, item)

    @admin_app.post(f"{base}/{{item_id}}/delete", name=f"{section.slug}_delete")
    async def delete_item(
        request: Request,
        item_id: str,
        form: AdminForm = Depends(require_admin_form),
        db: AsyncSession = Depends(get_db),
        settings: Settings = Depends(get_settings),
    ) -> Response:
        parsed = _parse_id(item_id)
        item = await db.get(section.model, parsed) if parsed else None
        if item is None:
            return _not_found(request, form.context, section.slug)
        if form.data.get("confirm") != "on":
            return _redirect(f"{base}/{item.id}?msg=confirm")
        image = getattr(item, "image", None)
        await db.delete(item)
        await db.commit()
        remove_image(settings, image)
        return _redirect(f"{base}?msg=deleted")

    @admin_app.post(f"{base}/{{item_id}}/toggle", name=f"{section.slug}_toggle")
    async def toggle_item(
        request: Request,
        item_id: str,
        form: AdminForm = Depends(require_admin_form),
        db: AsyncSession = Depends(get_db),
    ) -> Response:
        parsed = _parse_id(item_id)
        item = await db.get(section.model, parsed) if parsed else None
        if item is None:
            return _not_found(request, form.context, section.slug)
        item.is_active = not item.is_active
        await db.commit()
        return _redirect(f"{base}?msg=saved")

    @admin_app.post(f"{base}/{{item_id}}/move", name=f"{section.slug}_move")
    async def move(
        request: Request,
        item_id: str,
        form: AdminForm = Depends(require_admin_form),
        db: AsyncSession = Depends(get_db),
    ) -> Response:
        parsed = _parse_id(item_id)
        direction = {"up": -1, "down": 1}.get(str(form.data.get("direction")))
        if parsed is None or direction is None or not await move_item(db, section.model, parsed, direction):
            return _not_found(request, form.context, section.slug)
        return _redirect(base)


for _section in SECTIONS:
    _register_section(_section)


def _tier_snapshot(tier: LoyaltyTier) -> dict[str, Any]:
    return model_snapshot(
        {
            "name": tier.name,
            "min_spend": tier.min_spend,
            "discount_percent": tier.discount_percent,
            "cashback_percent": tier.cashback_percent,
            "perks": "\n".join(tier.perks or []),
            "iiko_category_id": tier.iiko_category_id,
            "iiko_category_manual": "",
            "is_active": tier.is_active,
        }
    )


async def load_iiko_categories(
    iiko: IikoCloudClient | None, settings: Settings
) -> tuple[list[dict[str, str]], str | None]:
    if iiko is None or not (settings.iiko_api_login and settings.iiko_organization_id):
        return [], "Интеграция с iiko не настроена — введите идентификатор категории вручную."
    try:
        categories = await asyncio.wait_for(iiko.list_customer_categories(), IIKO_CATEGORIES_TIMEOUT_SECONDS)
    except (IikoClientError, TimeoutError) as exc:
        return [], f"Не удалось загрузить категории из iiko ({exc or 'таймаут'}) — введите идентификатор вручную."
    return [
        {"id": str(category["id"]), "name": str(category.get("name") or category["id"])}
        for category in categories
        if isinstance(category, dict) and category.get("id")
    ], None


async def _render_tier(
    request: Request,
    context: AdminContext,
    tier: LoyaltyTier,
    data: dict[str, Any],
    errors: dict[str, str],
    iiko: IikoCloudClient | None,
    settings: Settings,
    status_code: int = 200,
) -> HTMLResponse:
    categories, categories_error = await load_iiko_categories(iiko, settings)
    return _render(
        request,
        "tier_form.html",
        context.page(
            "tiers",
            request,
            tier=tier,
            data=data,
            errors=errors,
            categories=categories,
            categories_error=categories_error,
            iiko_env=settings.iiko_env,
        ),
        status_code=status_code,
    )


@admin_app.get("/tiers", response_class=HTMLResponse)
async def tiers_list(
    request: Request,
    context: AdminContext = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    effective: Settings = Depends(get_effective_settings),
) -> Response:
    return _render(
        request,
        "tiers.html",
        context.page(
            "tiers",
            request,
            tiers=await load_tiers(db),
            tiers_enabled=effective.loyalty_tiers_enabled,
            tier_sync_enabled=effective.loyalty_tier_sync_enabled,
        ),
    )


@admin_app.get("/tiers/{tier_id}", response_class=HTMLResponse)
async def tier_edit(
    request: Request,
    tier_id: str,
    context: AdminContext = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
    iiko: IikoCloudClient | None = Depends(get_admin_iiko),
) -> Response:
    parsed = _parse_id(tier_id)
    tier = await db.get(LoyaltyTier, parsed) if parsed else None
    if tier is None:
        return _not_found(request, context, "tiers")
    return await _render_tier(request, context, tier, _tier_snapshot(tier), {}, iiko, settings)


@admin_app.post("/tiers/{tier_id}", response_class=HTMLResponse)
async def tier_update(
    request: Request,
    tier_id: str,
    form: AdminForm = Depends(require_admin_form),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
    iiko: IikoCloudClient | None = Depends(get_admin_iiko),
) -> Response:
    parsed = _parse_id(tier_id)
    tier = await db.get(LoyaltyTier, parsed) if parsed else None
    if tier is None:
        return _not_found(request, form.context, "tiers")
    values, errors = parse_tier(form.data)
    if not errors:
        errors = tier_conflicts(await load_tiers(db), tier, values)
    if errors:
        return await _render_tier(
            request,
            form.context,
            tier,
            form_snapshot(form.data),
            {**errors, "form": "Исправьте ошибки в форме"},
            iiko,
            settings,
            status_code=400,
        )
    for key, value in values.items():
        setattr(tier, key, value)
    await db.commit()
    return _redirect("/tiers?msg=saved")


def _stored_value(key: str, raw: Any) -> Any:
    try:
        return validate_setting(key, raw)
    except SettingValueError:
        return None


def _edited(key: str, raw: Any, env_value: Any) -> bool:
    return isinstance(raw, str) and _stored_value(key, raw) != _stored_value(key, env_value)


def _setting_rows(
    settings: Settings,
    overrides: Mapping[str, Any],
    submitted: Mapping[str, Any] | None,
    errors: Mapping[str, str],
) -> list[dict[str, Any]]:
    rows = []
    for setting in OVERRIDABLE_SETTINGS:
        override = overrides.get(setting.key)
        env_value = getattr(settings, setting.key)
        if submitted is None:
            stored = None if override is None else _stored_value(setting.key, override.value)
            value = env_value if stored is None else stored
            inherit = override is None
        else:
            value = submitted.get(f"value_{setting.key}", "")
            inherit = submitted.get(f"inherit_{setting.key}") == "on"
        rows.append(
            {
                "setting": setting,
                "env_value": env_value,
                "value": value,
                "inherit": inherit,
                "override": override,
                "error": errors.get(setting.key),
            }
        )
    return rows


def _render_settings(
    request: Request,
    context: AdminContext,
    settings: Settings,
    overrides: Mapping[str, Any],
    submitted: Mapping[str, Any] | None,
    errors: Mapping[str, str],
    status_code: int = 200,
) -> HTMLResponse:
    return _render(
        request,
        "settings.html",
        context.page(
            "settings",
            request,
            rows=_setting_rows(settings, overrides, submitted, errors),
            errors=errors,
            iiko_env=settings.iiko_env,
            iiko_mutations_enabled=settings.iiko_mutations_enabled,
            iiko_test_guest_configured=bool(settings.iiko_test_guest_phone),
        ),
        status_code=status_code,
    )


@admin_app.get("/settings", response_class=HTMLResponse)
async def settings_page(
    request: Request,
    context: AdminContext = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> Response:
    overrides = await load_override_rows(db)
    return _render_settings(request, context, settings, overrides, None, {})


@admin_app.post("/settings", response_class=HTMLResponse)
async def settings_save(
    request: Request,
    form: AdminForm = Depends(require_admin_form),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> Response:
    overrides = await load_override_rows(db)
    updates: dict[str, Any] = {}
    removals: list[str] = []
    errors: dict[str, str] = {}
    for setting in OVERRIDABLE_SETTINGS:
        current = overrides.get(setting.key)
        raw = form.data.get(f"value_{setting.key}")
        inherit = form.data.get(f"inherit_{setting.key}") == "on"
        if inherit and (current is not None or not _edited(setting.key, raw, getattr(settings, setting.key))):
            if current is not None:
                removals.append(setting.key)
            continue
        try:
            value = validate_override(settings, setting.key, raw if isinstance(raw, str) else "")
        except SettingValueError as exc:
            errors[setting.key] = str(exc)
            continue
        if current is None or _stored_value(setting.key, current.value) != value:
            updates[setting.key] = value
    if errors:
        return _render_settings(
            request,
            form.context,
            settings,
            overrides,
            form_snapshot(form.data),
            {**errors, "form": "Исправьте ошибки в форме"},
            status_code=400,
        )
    await save_overrides(db, updates, removals, form.context.admin.login)
    return _redirect("/settings?msg=saved")


@admin_app.get("/guests", response_class=HTMLResponse)
async def guests_page(
    request: Request,
    context: AdminContext = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> Response:
    digits = search_digits(request.query_params.get("q"))
    try:
        page_number = max(1, min(int(request.query_params.get("page", "1")), 100_000))
    except ValueError:
        page_number = 1
    rows, total = await list_guests(db, digits, page_number)
    pages = max(1, -(-total // GUESTS_PAGE_SIZE))
    return _render(
        request,
        "guests.html",
        context.page("guests", request, rows=rows, total=total, page_number=page_number, pages=pages, q=digits),
    )
