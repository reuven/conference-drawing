"""Pure validators and upload naming for the after-conference page settings.

Kept apart from `models.py` so they import without Django models and can be
mutation-tested on their own. `models.py` re-imports them, so migrations that
name `conference_drawing.raffle.models.validate_logo` still resolve.
"""

import io
import unicodedata
import uuid
import warnings
from typing import Any
from urllib.parse import urlsplit

from django.core.exceptions import ValidationError
from django.core.files import File
from django.core.files.base import ContentFile
from django.db import models
from django.db.models.fields.files import FieldFile
from PIL import Image, ImageOps, ImageSequence

MAX_REDIRECT_URL_LENGTH = 500
MAX_LOGO_BYTES = 2 * 1024 * 1024
_LOGO_EXTENSIONS = {
    "PNG": "png",
    "JPEG": "jpg",
    "MPO": "jpg",
    "WEBP": "webp",
    "GIF": "gif",
}


def validate_redirect_url(value: str) -> None:
    """Accept only a short absolute https URL with a host and no userinfo.

    Why: the entry page 302s visitors here, so a `javascript:` or credentialed
    URL, or one with control characters, must never get through.
    """
    if len(value) > MAX_REDIRECT_URL_LENGTH or any(
        c.isspace() or unicodedata.category(c).startswith("C") for c in value
    ):
        raise ValidationError("Enter a shorter https:// URL without spaces.")
    try:
        parts = urlsplit(value)
        host = parts.hostname
    except ValueError:
        raise ValidationError("Enter a valid https:// URL.") from None
    if parts.scheme.lower() != "https" or not host or "@" in parts.netloc:
        raise ValidationError("Enter a full https:// URL, without a username.")


def validate_logo(file: File[Any]) -> None:
    """Accept only a small, intact PNG, JPEG, WEBP or GIF.

    Why: the file is served from our own origin, so it must really be a raster
    image (never SVG or HTML renamed .png) and must not blow up memory when
    decoded. Pillow decides by content, not by the uploaded name.

    A file already stored was validated when uploaded; re-checking it on every
    admin re-save would crash if the file has gone missing from disk.
    """
    if isinstance(file, FieldFile) and file._committed:  # type: ignore[attr-defined]
        return
    if file.size is not None and file.size > MAX_LOGO_BYTES:
        raise ValidationError("The logo must be 2 MB or smaller.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            file.seek(0)
            with Image.open(file) as img:
                fmt = img.format
                img.verify()
    except (
        OSError,
        SyntaxError,
        ValueError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ):
        raise ValidationError("Upload a valid PNG, JPEG, WEBP or GIF image.") from None
    finally:
        file.seek(0)
    if fmt not in _LOGO_EXTENSIONS:
        raise ValidationError("Upload a PNG, JPEG, WEBP or GIF image.")


def logo_upload_to(instance: models.Model, filename: str) -> str:
    """Name an uploaded logo `logos/<random hex>.<ext>`, ignoring the user's name.

    Why: the uploaded filename is attacker-controlled. Django passes only that
    name, but the instance's `after_logo` already holds the upload, so the
    extension comes from the format Pillow detects in the bytes.
    """
    upload = instance.after_logo  # type: ignore[attr-defined]
    position = upload.file.tell()
    try:
        upload.file.seek(0)
        with Image.open(upload.file) as img:
            ext = _LOGO_EXTENSIONS.get(img.format or "")
    except OSError, SyntaxError, ValueError:
        ext = None
    finally:
        upload.file.seek(position)
    if ext is None:
        raise ValidationError("Upload a valid PNG, JPEG, WEBP or GIF image.")
    return f"logos/{uuid.uuid4().hex}.{ext}"


_REENCODE_FORMATS = {
    "PNG": "PNG",
    "JPEG": "JPEG",
    "MPO": "JPEG",
    "WEBP": "WEBP",
    "GIF": "GIF",
}


def reencode_image(file: File[Any]) -> ContentFile[bytes]:
    """Re-encode an uploaded image in its own format, without its metadata.

    Why: phone photos carry EXIF (GPS position, device, time) that a logo or
    cover served from our origin would publish. Pillow writes none of it unless
    asked, so decoding and saving again drops EXIF and XMP; the ICC color
    profile is kept so colors don't shift. The EXIF orientation is applied
    first (otherwise dropping it would leave a sideways picture), PNG/WebP
    transparency survives, and a GIF or WebP animation keeps every frame, its
    durations and its loop count. Decoding runs with decompression-bomb
    warnings promoted to errors, like `validate_logo`.

    Call it on an upload that already passed `validate_logo`; a file that
    still cannot be decoded raises `ValidationError`.
    """
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            file.seek(0)
            with Image.open(file) as img:
                fmt = _REENCODE_FORMATS.get(img.format or "")
                if fmt is None:
                    raise ValueError("unsupported format")
                data = _encode(img, fmt)
    except (
        OSError,
        SyntaxError,
        ValueError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ):
        raise ValidationError("Upload a valid PNG, JPEG, WEBP or GIF image.") from None
    finally:
        file.seek(0)
    return ContentFile(data, name=file.name)


def _encode(img: Image.Image, fmt: str) -> bytes:
    """Encode `img` as `fmt` with no EXIF/XMP; helper for `reencode_image`."""
    out = io.BytesIO()
    icc = img.info.get("icc_profile")
    if getattr(img, "is_animated", False) and fmt in {"GIF", "WEBP"}:
        frames = [frame.copy() for frame in ImageSequence.Iterator(img)]
        durations = []
        for index in range(len(frames)):
            img.seek(index)
            durations.append(img.info.get("duration", 100))
        frames[0].save(
            out,
            format=fmt,
            save_all=True,
            append_images=frames[1:],
            duration=durations,
            loop=img.info.get("loop", 0),
            **({"icc_profile": icc} if icc and fmt == "WEBP" else {}),
        )
        return out.getvalue()
    still = ImageOps.exif_transpose(img)
    if still.mode in {"P", "L", "RGB", "1"} and "transparency" in still.info:
        still = still.convert("RGBA")
    if fmt == "JPEG":
        if still.mode not in {"RGB", "L", "CMYK"}:
            still = still.convert("RGB")
        still.save(out, format=fmt, quality=90, optimize=True, icc_profile=icc)
    elif fmt == "WEBP":
        still.save(out, format=fmt, quality=90, icc_profile=icc)
    else:
        still.save(
            out, format=fmt, optimize=True, **({"icc_profile": icc} if icc else {})
        )
    return out.getvalue()
