from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from collections.abc import Sequence

from sqlalchemy import select

from ..config import get_settings
from ..db import async_session_factory, engine
from ..media import copy_seed_media
from ..models import AdminUser
from .security import generate_password, hash_password

PASSWORD_ENV = "ADMIN_PASSWORD"
MIN_PASSWORD_LENGTH = 12
_LOGIN = re.compile(r"[A-Za-z0-9_.@-]{3,64}")


class CliError(Exception):
    pass


def resolve_password() -> tuple[str, bool]:
    provided = os.environ.get(PASSWORD_ENV)
    if provided is None:
        return generate_password(), True
    if len(provided) < MIN_PASSWORD_LENGTH:
        raise CliError(f"{PASSWORD_ENV} должен быть не короче {MIN_PASSWORD_LENGTH} символов")
    return provided, False


def validate_login(login: str) -> str:
    if not _LOGIN.fullmatch(login):
        raise CliError("Логин: 3–64 символа, латиница, цифры и _ . @ -")
    return login


async def create_user(login: str) -> str | None:
    password, generated = resolve_password()
    async with async_session_factory() as db:
        if await db.scalar(select(AdminUser.id).where(AdminUser.login == login)) is not None:
            raise CliError(f"Пользователь {login} уже существует")
        db.add(AdminUser(login=login, password_hash=hash_password(password), is_active=True))
        await db.commit()
    return password if generated else None


async def set_password(login: str) -> str | None:
    password, generated = resolve_password()
    async with async_session_factory() as db:
        admin = await db.scalar(select(AdminUser).where(AdminUser.login == login))
        if admin is None:
            raise CliError(f"Пользователь {login} не найден")
        admin.password_hash = hash_password(password)
        await db.commit()
    return password if generated else None


async def _run(args: argparse.Namespace) -> None:
    try:
        if args.command == "seed-media":
            copied = copy_seed_media(get_settings())
            print(f"Скопировано файлов: {len(copied)}" + (f" ({', '.join(copied)})" if copied else ""))
            return
        login = validate_login(args.login)
        action = create_user if args.command == "create-user" else set_password
        password = await action(login)
        print(f"Готово: {login}")
        if password is not None:
            print(f"Пароль (показывается один раз): {password}")
    finally:
        await engine.dispose()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.admin.cli", description="Управление админкой Пекарни")
    commands = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("create-user", "создать администратора"),
        ("set-password", "сменить пароль администратора"),
    ):
        command = commands.add_parser(
            name, help=f"{help_text} (пароль из {PASSWORD_ENV} или сгенерированный)"
        )
        command.add_argument("--login", required=True)
    commands.add_parser("seed-media", help="скопировать стартовые картинки в MEDIA_DIR, если их там нет")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        asyncio.run(_run(args))
    except CliError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
