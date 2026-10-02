import httpx
import pytest

from app.config import get_settings
from app.main import app
from app.media import (
    SEED_MEDIA_DIR,
    ImageRejected,
    copy_seed_media,
    detect_image_extension,
    is_media_name,
    is_uploaded_name,
    media_url,
    remove_image,
    resolve_media_path,
    store_image,
    validate_image,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 32
HEX_NAME = "0123456789abcdef0123456789abcdef.png"


class TestMagicBytes:
    @pytest.mark.parametrize(("data", "extension"), [(PNG, "png"), (JPEG, "jpg"), (WEBP, "webp")])
    def test_supported_formats(self, data, extension):
        assert detect_image_extension(data) == extension
        assert validate_image(data, 1024) == extension

    @pytest.mark.parametrize(
        "data",
        [
            b"GIF89a" + b"\x00" * 20,
            b"<svg xmlns='http://www.w3.org/2000/svg'></svg>",
            b"<html><script>alert(1)</script></html>",
            b"RIFF\x24\x00\x00\x00WAVEfmt ",
            b"\x89PNG",
            b"%PDF-1.7",
        ],
    )
    def test_everything_else_is_rejected(self, data):
        with pytest.raises(ImageRejected):
            validate_image(data, 1024)

    def test_empty_file(self):
        with pytest.raises(ImageRejected, match="пустой"):
            validate_image(b"", 1024)

    def test_size_limit(self):
        assert validate_image(PNG + b"\x00" * (1024 - len(PNG)), 1024) == "png"
        with pytest.raises(ImageRejected, match="больше"):
            validate_image(PNG + b"\x00" * (1025 - len(PNG)), 1024)


class TestNames:
    @pytest.mark.parametrize("name", [HEX_NAME, "ffffffffffffffffffffffffffffffff.jpg", "a" * 32 + ".webp"])
    def test_uploaded_names(self, name):
        assert is_uploaded_name(name) is True
        assert is_media_name(name) is True

    @pytest.mark.parametrize("name", ["seed_pizza_bbq.png", "seed_a1.jpg", "seed_x_y.webp"])
    def test_seed_names(self, name):
        assert is_uploaded_name(name) is False
        assert is_media_name(name) is True

    @pytest.mark.parametrize(
        "name",
        [
            "../.env",
            "..%2F.env",
            "/etc/passwd",
            "0123456789ABCDEF0123456789ABCDEF.png",
            "0123456789abcdef0123456789abcde.png",
            HEX_NAME + "\n",
            HEX_NAME + ".png",
            "0123456789abcdef0123456789abcdef.gif",
            "0123456789abcdef0123456789abcdef.svg",
            "seed_../x.png",
            "seed_Pizza.png",
            "seed_.png",
            "seed_pizza.png/..",
            "sub/" + HEX_NAME,
            "",
        ],
    )
    def test_everything_else(self, name):
        assert is_media_name(name) is False


class TestStorage:
    def test_store_resolve_and_remove(self, make_settings, tmp_path):
        settings = make_settings(media_dir=str(tmp_path / "media"))
        name = store_image(settings, PNG)
        assert is_uploaded_name(name) and name.endswith(".png")
        path = resolve_media_path(settings, name)
        assert path is not None and path.read_bytes() == PNG
        assert [item.name for item in (tmp_path / "media").iterdir()] == [name]
        assert remove_image(settings, name) is True
        assert resolve_media_path(settings, name) is None
        assert remove_image(settings, name) is False

    def test_store_rejects_invalid_upload_without_writing(self, make_settings, tmp_path):
        settings = make_settings(media_dir=str(tmp_path), media_max_bytes=16)
        with pytest.raises(ImageRejected):
            store_image(settings, PNG)
        with pytest.raises(ImageRejected):
            store_image(settings, b"GIF89a")
        assert list(tmp_path.iterdir()) == []

    def test_remove_never_touches_seed_or_foreign_files(self, make_settings, tmp_path):
        settings = make_settings(media_dir=str(tmp_path / "media"))
        (tmp_path / "media").mkdir()
        (tmp_path / "media" / "seed_pizza_bbq.png").write_bytes(PNG)
        (tmp_path / "secret.png").write_bytes(PNG)
        assert remove_image(settings, "seed_pizza_bbq.png") is False
        assert remove_image(settings, "../secret.png") is False
        assert remove_image(settings, None) is False
        assert (tmp_path / "media" / "seed_pizza_bbq.png").exists()
        assert (tmp_path / "secret.png").exists()

    def test_resolve_refuses_traversal_even_if_target_exists(self, make_settings, tmp_path):
        settings = make_settings(media_dir=str(tmp_path / "media"))
        (tmp_path / "media").mkdir()
        (tmp_path / HEX_NAME).write_bytes(PNG)
        assert resolve_media_path(settings, f"../{HEX_NAME}") is None
        assert resolve_media_path(settings, HEX_NAME) is None

    def test_seed_copy_only_fills_missing_files(self, make_settings, tmp_path):
        source = tmp_path / "seed"
        source.mkdir()
        (source / "seed_one.png").write_bytes(PNG)
        (source / "seed_two.png").write_bytes(PNG)
        (source / "README.txt").write_text("skip me")
        target = tmp_path / "media"
        target.mkdir()
        (target / "seed_two.png").write_bytes(b"customised")
        settings = make_settings(media_dir=str(target))
        assert copy_seed_media(settings, source) == ["seed_one.png"]
        assert (target / "seed_two.png").read_bytes() == b"customised"
        assert not (target / "README.txt").exists()
        assert copy_seed_media(settings, source) == []

    def test_bundled_seed_media_matches_seed_rows(self):
        names = sorted(item.name for item in SEED_MEDIA_DIR.iterdir())
        assert names == ["seed_croissant_chocolate.png", "seed_pechusha_ham_cheese.png", "seed_pizza_bbq.png"]
        assert all(detect_image_extension((SEED_MEDIA_DIR / name).read_bytes()) == "png" for name in names)

    def test_public_url(self, make_settings):
        settings = make_settings()
        assert media_url(settings, HEX_NAME) == f"https://app.top1makovka.ru/api/media/{HEX_NAME}"
        assert media_url(settings, None) is None


@pytest.fixture
async def media_client(make_settings, tmp_path):
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    (media_dir / HEX_NAME).write_bytes(PNG)
    (media_dir / "seed_pizza_bbq.png").write_bytes(PNG)
    (media_dir / "ffffffffffffffffffffffffffffffff.jpg").write_bytes(JPEG)
    (tmp_path / "secret.png").write_bytes(b"top secret")
    app.dependency_overrides[get_settings] = lambda: make_settings(media_dir=str(media_dir))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
        yield http
    app.dependency_overrides.clear()


class TestMediaRoute:
    async def test_serves_uploaded_file_publicly(self, media_client):
        response = await media_client.get(f"/media/{HEX_NAME}")
        assert response.status_code == 200
        assert response.content == PNG
        assert response.headers["content-type"] == "image/png"
        assert response.headers["cache-control"] == "public, max-age=604800"
        assert response.headers["x-content-type-options"] == "nosniff"

    async def test_serves_seed_file_and_jpeg_type(self, media_client):
        assert (await media_client.get("/media/seed_pizza_bbq.png")).status_code == 200
        jpeg = await media_client.get("/media/ffffffffffffffffffffffffffffffff.jpg")
        assert jpeg.headers["content-type"] == "image/jpeg"

    @pytest.mark.parametrize(
        "path",
        [
            "/media/..%2Fsecret.png",
            "/media/%2E%2E%2Fsecret.png",
            "/media/../secret.png",
            "/media/secret.png",
            "/media/0123456789abcdef0123456789abcdee.png",
            f"/media/{HEX_NAME}%0A",
            "/media/seed_missing.png",
        ],
    )
    async def test_everything_else_is_404(self, media_client, path):
        response = await media_client.get(path)
        assert response.status_code == 404
        assert b"top secret" not in response.content
