from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse

from ..config import Settings, get_settings
from ..media import media_type, resolve_media_path

router = APIRouter(prefix="/media", tags=["media"])

_HEADERS = {"Cache-Control": "public, max-age=604800", "X-Content-Type-Options": "nosniff"}


@router.get("/{filename}")
async def get_media(filename: str, settings: Settings = Depends(get_settings)):
    path = resolve_media_path(settings, filename)
    if path is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Файл не найден")
    return FileResponse(path, media_type=media_type(filename), headers=_HEADERS)
