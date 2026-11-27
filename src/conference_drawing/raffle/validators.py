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


class _FrameBudgetError(Exception):
    """An image's frames add up to more pixels than we will decode."""


def _check_frame_budget(img: Image.Image) -> None:
    """Refuse an animation whose frames together exceed `Image.MAX_IMAGE_PIXELS`.

    Why: Pillow's bomb check looks at one frame, but re-encoding an animation
    holds every decoded frame in memory, and a tiny file can declare hundreds
    of large frames. Checked before any frame is decoded.
    """
    limit = Image.MAX_IMAGE_PIXELS
    frames = getattr(img, "n_frames", 1)
    if limit is not None and frames * img.width * img.height > limit:
        raise _FrameBudgetError


_TOO_MANY_PIXELS = (
    "This image has too many pixels in total (all frames of an animation count). "
    "Upload a smaller or shorter one."
)


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
        raise ValidationError("The image must be 2 MB or smaller.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            file.seek(0)
            with Image.open(file) as img:
                fmt = img.format
                _check_frame_budget(img)
                img.verify()
    except _FrameBudgetError:
        raise ValidationError(_TOO_MANY_PIXELS) from None
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


def _random_image_name(folder: str, upload: Any) -> str:
    """Name an upload `<folder>/<random hex>.<ext>`, ext from the detected format."""
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
    return f"{folder}/{uuid.uuid4().hex}.{ext}"


def logo_upload_to(instance: models.Model, filename: str) -> str:
    """Name an uploaded logo `logos/<random hex>.<ext>`, ignoring the user's name.

    Why: the uploaded filename is attacker-controlled. Django passes only that
    name, but the instance's `after_logo` already holds the upload, so the
    extension comes from the format Pillow detects in the bytes.
    """
    return _random_image_name("logos", instance.after_logo)  # type: ignore[attr-defined]


def cover_upload_to(instance: models.Model, filename: str) -> str:
    """Name an uploaded book cover `covers/<random hex>.<ext>`, like a logo.

    Why: a cover is served from our origin too, so a `cover.pdf` that is really
    a PNG must be stored as `.png` under a name the uploader doesn't choose.
    """
    return _random_image_name("covers", instance.cover)  # type: ignore[attr-defined]


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
                _check_frame_budget(img)
                data = _encode(img, fmt)
    except _FrameBudgetError:
        raise ValidationError(_TOO_MANY_PIXELS) from None
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
        frames = []
        durations = []
        for frame in ImageSequence.Iterator(img):
            durations.append(frame.info.get("duration", 100))
            frame.info.pop("comment", None)
            frames.append(frame.copy())
        extra: dict[str, Any] = {}
        if "loop" in img.info:
            extra["loop"] = img.info["loop"]
        if icc and fmt == "WEBP":
            extra["icc_profile"] = icc
        frames[0].save(
            out,
            format=fmt,
            save_all=True,
            append_images=frames[1:],
            duration=durations,
            **extra,
        )
        return out.getvalue()
    still = ImageOps.exif_transpose(img)
    still.info.pop("comment", None)  # Pillow would copy a JPEG/GIF comment through
    if still.mode in {"P", "L", "RGB", "1"} and "transparency" in still.info:
        still = still.convert("RGBA")
    if fmt == "JPEG":
        still.save(out, format=fmt, quality=90, optimize=True, icc_profile=icc)
    elif fmt == "WEBP":
        still.save(out, format=fmt, quality=90, icc_profile=icc)
    else:
        still.save(
            out, format=fmt, optimize=True, **({"icc_profile": icc} if icc else {})
        )
    return out.getvalue()


def reencode_pending_upload(field_file: FieldFile) -> None:
    """Swap a not-yet-stored upload for its metadata-free re-encoding.

    Why: call it from a model's `save()` so every path that stores an image
    (admin form, shell, tests) strips EXIF. A file already stored, or an empty
    field, is left alone, so a re-save never rewrites what is on disk.
    """
    if field_file and not field_file._committed:  # type: ignore[attr-defined]
        field_file.file = reencode_image(field_file.file)
