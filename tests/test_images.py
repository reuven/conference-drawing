"""Uploaded images are re-encoded without metadata; covers are validated like logos."""

import io
import re
from pathlib import Path
from typing import Any, cast

import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image

from conference_drawing.raffle.models import Book, InstallSettings
from conference_drawing.raffle.validators import (
    cover_upload_to,
    reencode_image,
    validate_logo,
)
from tests.factories import make_conference

_GPS = {1: "N", 2: (37.0, 25.0, 19.0), 3: "W", 4: (122.0, 5.0, 6.0)}


def _gps_exif(orientation: int = 1) -> Image.Exif:
    exif = Image.Exif()
    exif[0x0112] = orientation
    exif[0x010F] = "SecretCam"
    exif[0x8825] = _GPS
    return exif


def _px(img: Image.Image, xy: tuple[int, int]) -> tuple[int, ...]:
    return cast("tuple[int, ...]", img.getpixel(xy))


def _frames(img: Any) -> int:
    return int(img.n_frames)


def _upload(img: Image.Image, fmt: str, name: str = "x", **save: Any) -> Any:
    buf = io.BytesIO()
    img.save(buf, format=fmt, **save)
    return SimpleUploadedFile(name, buf.getvalue())


def _reopen(content: Any) -> Image.Image:
    img = Image.open(io.BytesIO(content.read()))
    img.load()
    return img


def test_jpeg_loses_gps_exif_and_is_rotated_upright() -> None:
    # Landscape: left half red, right half blue, flagged "rotate 90 clockwise".
    img = Image.new("RGB", (80, 40), "blue")
    img.paste("red", (0, 0, 40, 40))
    upload = _upload(img, "JPEG", exif=_gps_exif(6), quality=95)
    assert 0x8825 in Image.open(io.BytesIO(upload.read())).getexif()
    upload.seek(0)
    out = _reopen(reencode_image(upload))
    assert out.format == "JPEG"
    assert out.size == (40, 80)
    assert dict(out.getexif()) == {}
    assert out.getexif().get_ifd(0x8825) == {}
    assert _px(out, (20, 10))[0] > 200  # red now on top
    assert _px(out, (20, 70))[2] > 200  # blue on the bottom


def test_png_keeps_alpha_and_drops_exif() -> None:
    img = Image.new("RGBA", (6, 6), (255, 0, 0, 0))
    img.putpixel((1, 1), (0, 255, 0, 128))
    upload = _upload(img, "PNG", exif=_gps_exif())
    out = _reopen(reencode_image(upload))
    assert out.format == "PNG"
    assert out.mode == "RGBA"
    assert _px(out, (0, 0))[3] == 0
    assert _px(out, (1, 1)) == (0, 255, 0, 128)
    assert dict(out.getexif()) == {}


def test_palette_png_with_transparency_stays_transparent() -> None:
    img = Image.new("P", (4, 4), 0)
    img.putpalette([255, 0, 0, 0, 255, 0] + [0] * 250 * 3)
    out = _reopen(reencode_image(_upload(img, "PNG", transparency=0)))
    assert _px(out.convert("RGBA"), (0, 0))[3] == 0


def test_animated_gif_keeps_every_frame_duration_and_loop() -> None:
    frames = [Image.new("RGB", (10, 10), c) for c in ("red", "green", "blue")]
    upload = _upload(
        frames[0],
        "GIF",
        save_all=True,
        append_images=frames[1:],
        duration=[100, 200, 300],
        loop=3,
    )
    out = Image.open(io.BytesIO(reencode_image(upload).read()))
    assert out.format == "GIF"
    assert _frames(out) == 3
    assert out.info["loop"] == 3
    seen = []
    for i in range(3):
        out.seek(i)
        seen.append(out.info["duration"])
    assert seen == [100, 200, 300]
    out.seek(1)
    assert _px(out.convert("RGB"), (0, 0))[1] > 100


def test_webp_roundtrip_with_alpha_and_animation() -> None:
    still = Image.new("RGBA", (8, 8), (1, 2, 3, 0))
    out = _reopen(reencode_image(_upload(still, "WEBP", lossless=True)))
    assert out.format == "WEBP"
    assert _px(out, (0, 0))[3] == 0
    frames = [Image.new("RGB", (8, 8), c) for c in ("red", "blue")]
    anim = _upload(
        frames[0], "WEBP", save_all=True, append_images=frames[1:], duration=120
    )
    moving = Image.open(io.BytesIO(reencode_image(anim).read()))
    assert _frames(moving) == 2


def test_reencode_refuses_non_images_and_other_formats() -> None:
    with pytest.raises(ValidationError):
        reencode_image(SimpleUploadedFile("x.png", b"<html>nope</html>"))
    with pytest.raises(ValidationError):
        reencode_image(_upload(Image.new("RGB", (4, 4)), "BMP"))


def test_transparent_animated_gif_stays_transparent() -> None:
    frames = [Image.new("RGBA", (10, 10), (255, 0, 0, 0)) for _ in range(2)]
    frames[1].putpixel((3, 3), (0, 0, 255, 255))
    upload = _upload(
        frames[0],
        "GIF",
        save_all=True,
        append_images=frames[1:],
        duration=50,
        transparency=0,
        disposal=2,
    )
    out = Image.open(io.BytesIO(reencode_image(upload).read()))
    out.seek(1)
    assert _px(out.convert("RGBA"), (0, 0))[3] == 0
    assert _px(out.convert("RGBA"), (3, 3)) == (0, 0, 255, 255)


@pytest.mark.parametrize("fmt", ["WEBP", "JPEG"])
def test_xmp_is_dropped(fmt: str) -> None:
    upload = _upload(Image.new("RGB", (5, 5)), fmt, xmp=b"<x:xmpmeta/>")
    assert b"xmpmeta" not in reencode_image(upload).read()


@pytest.fixture
def media(settings: Any, tmp_path: Path) -> Path:
    settings.MEDIA_ROOT = tmp_path
    return tmp_path


def _stored(upload_dir: Path) -> Image.Image:
    (path,) = list(upload_dir.iterdir())
    return Image.open(path)


@pytest.mark.django_db
def test_saving_a_conference_logo_stores_it_without_metadata(media: Path) -> None:
    img = Image.new("RGB", (20, 10), "red")
    conf = make_conference(after_logo=_upload(img, "JPEG", "l.jpg", exif=_gps_exif()))
    conf.refresh_from_db()
    assert dict(_stored(media / "logos").getexif()) == {}
    assert re.fullmatch(r"logos/[0-9a-f]{32}\.jpg", str(conf.after_logo.name))


@pytest.mark.django_db
def test_saving_install_settings_logo_stores_it_without_metadata(media: Path) -> None:
    site = InstallSettings.get()
    site.after_logo = _upload(
        Image.new("RGB", (20, 10), "red"), "JPEG", "l.jpg", exif=_gps_exif()
    )
    site.save()
    assert dict(_stored(media / "logos").getexif()) == {}


@pytest.mark.django_db
def test_saving_a_book_cover_stores_it_without_metadata_under_a_random_name(
    media: Path,
) -> None:
    cover = _upload(
        Image.new("RGB", (20, 10), "red"), "PNG", "cover.pdf", exif=_gps_exif()
    )
    book = Book.objects.create(title="B", cover=cover)
    assert re.fullmatch(r"covers/[0-9a-f]{32}\.png", str(book.cover.name))
    assert dict(_stored(media / "covers").getexif()) == {}


@pytest.mark.django_db
def test_resaving_a_book_does_not_rewrite_the_stored_cover(media: Path) -> None:
    book = Book.objects.create(
        title="B", cover=_upload(Image.new("RGB", (4, 4), "red"), "PNG", "c.png")
    )
    name = str(book.cover.name)
    before = (media / name).stat().st_mtime_ns
    book.title = "Renamed"
    book.save()
    assert book.cover.name == name
    assert (media / name).stat().st_mtime_ns == before


def test_cover_validation_matches_logos() -> None:
    field = Book._meta.get_field("cover")
    html = SimpleUploadedFile("c.png", b"<html></html>")
    with pytest.raises(ValidationError):
        field.clean(html, Book())
    big = SimpleUploadedFile("c.png", b"\x89PNG" + b"0" * (3 * 1024 * 1024))
    with pytest.raises(ValidationError, match="2 MB"):
        field.clean(big, Book())


def test_cover_upload_name_ignores_user_name_and_uses_detected_extension() -> None:
    book = Book(cover=_upload(Image.new("RGB", (4, 4)), "GIF", "../x.pdf"))
    assert re.fullmatch(r"covers/[0-9a-f]{32}\.gif", cover_upload_to(book, "x.pdf"))
    bad = Book(cover=SimpleUploadedFile("x.png", b"nope"))
    with pytest.raises(ValidationError):
        cover_upload_to(bad, "x.png")


def test_jpeg_and_gif_comments_are_dropped() -> None:
    jpeg = _upload(Image.new("RGB", (5, 5)), "JPEG", comment=b"secret-jpeg-note")
    assert b"secret-jpeg-note" in jpeg.read()
    jpeg.seek(0)
    assert b"secret-jpeg-note" not in reencode_image(jpeg).read()
    still = _upload(Image.new("P", (5, 5)), "GIF", comment=b"secret-gif-note")
    assert b"secret-gif-note" not in reencode_image(still).read()
    frames = [Image.new("P", (5, 5), i) for i in range(2)]
    anim = _upload(
        frames[0],
        "GIF",
        save_all=True,
        append_images=frames[1:],
        comment=b"secret-anim-note",
    )
    assert b"secret-anim-note" in anim.read()
    anim.seek(0)
    assert b"secret-anim-note" not in reencode_image(anim).read()


def test_play_once_gif_stays_play_once() -> None:
    frames = [Image.new("RGB", (5, 5), c) for c in ("red", "blue")]
    once = _upload(frames[0], "GIF", save_all=True, append_images=frames[1:])
    assert "loop" not in Image.open(io.BytesIO(once.read())).info
    once.seek(0)
    out = Image.open(io.BytesIO(reencode_image(once).read()))
    assert "loop" not in out.info
    assert _frames(out) == 2


def _many_frame_gif() -> Any:
    frames = [Image.new("RGB", (10, 10), (i * 10, 0, 0)) for i in range(20)]
    return _upload(frames[0], "GIF", save_all=True, append_images=frames[1:])


def test_frame_budget_is_checked_by_the_validator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upload = _many_frame_gif()
    assert len(upload.read()) < 2000  # tiny on disk
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 1000)  # 20 frames x 100 px = 2000
    upload.seek(0)
    with pytest.raises(ValidationError, match="too many pixels"):
        validate_logo(upload)
    with pytest.raises(ValidationError, match="too many pixels"):
        reencode_image(upload)
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 2000)
    validate_logo(upload)
