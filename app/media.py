from __future__ import annotations

import os
import re
import secrets
import shutil
import tempfile
from pathlib import Path

from .config import Settings

UPLOADED_NAME = re.compile(r"[a-f0-9]{32}\.(png|jpg|webp)")
SEED_NAME = re.compile(r"seed_[a-z0-9_]+\.(png|jpg|webp)")
MEDIA_TYPES = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp"}
SEED_MEDIA_DIR = Path(__file__).resolve().parent.parent / "seed_media"

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_JPEG_SIGNATURE = b"\xff\xd8\xff"


class ImageRejected(ValueError):
    pass


def is_uploaded_name(name: str) -> bool:
    return UPLOADED_NAME.fullmatch(name) is not None


def is_media_name(name: str) -> bool:
    return is_uploaded_name(name) or SEED_NAME.fullmatch(name) is not None


def media_type(name: str) -> str:
    return MEDIA_TYPES[name.rsplit(".", 1)[1]]


def media_root(settings: Settings) -> Path:
    return Path(settings.media_dir).resolve()


def resolve_media_path(settings: Settings, name: str) -> Path | None:
    if not is_media_name(name):
        return None
    root = media_root(settings)
    path = root / name
    if path.parent != root or not path.is_file():
        return None
    return path


def media_url(settings: Settings, name: str | None) -> str | None:
    if not name:
        return None
    return f"{settings.public_media_base_url.rstrip('/')}/{name}"


def detect_image_extension(data: bytes) -> str | None:
    if data.startswith(_PNG_SIGNATURE):
        return "png"
    if data.startswith(_JPEG_SIGNATURE):
        return "jpg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def validate_image(data: bytes, max_bytes: int) -> str:
    if not data:
        raise ImageRejected("Файл пустой")
    if len(data) > max_bytes:
        raise ImageRejected(f"Файл больше {max_bytes // (1024 * 1024)} МБ")
    extension = detect_image_extension(data)
    if extension is None:
        raise ImageRejected("Можно загрузить только PNG, JPEG или WebP")
    return extension


def store_image(settings: Settings, data: bytes) -> str:
    extension = validate_image(data, settings.media_max_bytes)
    root = media_root(settings)
    root.mkdir(parents=True, exist_ok=True)
    name = f"{secrets.token_hex(16)}.{extension}"
    fd, temp_path = tempfile.mkstemp(dir=root, prefix=".upload-")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.chmod(temp_path, 0o644)
        os.replace(temp_path, root / name)
    except BaseException:
        Path(temp_path).unlink(missing_ok=True)
        raise
    return name


def remove_image(settings: Settings, name: str | None) -> bool:
    if not name or not is_uploaded_name(name):
        return False
    root = media_root(settings)
    path = root / name
    if path.parent != root:
        return False
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


def copy_seed_media(settings: Settings, source: Path = SEED_MEDIA_DIR) -> list[str]:
    root = media_root(settings)
    root.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    for item in sorted(source.iterdir()):
        if not item.is_file() or SEED_NAME.fullmatch(item.name) is None:
            continue
        target = root / item.name
        if target.exists():
            continue
        shutil.copyfile(item, target)
        os.chmod(target, 0o644)
        copied.append(item.name)
    return copied
