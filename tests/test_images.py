"""Uploaded images are re-encoded without metadata; covers are validated like logos."""

import io
from typing import Any, cast

import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image

from conference_drawing.raffle.validators import reencode_image

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


def test_reencode_refuses_non_images() -> None:
    with pytest.raises(ValidationError):
        reencode_image(SimpleUploadedFile("x.png", b"<html>nope</html>"))


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
