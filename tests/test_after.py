"""After-conference settings: validators, upload naming, and the resolver."""

import io
import re
import struct
import zlib
from typing import Any
from urllib.parse import urlsplit

import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from hypothesis import example, given
from hypothesis import strategies as st
from PIL import Image
from pydantic import ValidationError as PydanticValidationError

from conference_drawing.raffle.after import AfterContent, resolve_after
from conference_drawing.raffle.markup import render_markdown
from conference_drawing.raffle.models import (
    AfterMode,
    Conference,
    InstallSettings,
    logo_upload_to,
    validate_logo,
    validate_redirect_url,
)

# --- validate_redirect_url ---------------------------------------------------


@pytest.mark.parametrize("url", ["https://example.com/x?y=1", "HTTPS://Example.com"])
def test_redirect_url_accepts_https(url: str) -> None:
    validate_redirect_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com",
        "javascript:alert(1)",
        "https://user:pw@example.com",
        "https:///nohost",
        "https://exa mple.com",
        "https://example.com/\n",
        "https://example.com/" + "a" * 500,
        "https://[::1",
        "https://example.com/\x9b",
        "https://exam\u200bple.com",
        "https://example.com/\u202e",
    ],
)
def test_redirect_url_refuses_bad_urls(url: str) -> None:
    with pytest.raises(ValidationError):
        validate_redirect_url(url)


@example("https://example.com")
@example("https://[::1")
@example("https://@x.com")
@example("https://example.com/\x9b")
@example("https://exam\u200bple.com")
@example("https://example.com/\u202e")
@given(st.text())
def test_redirect_url_property(value: str) -> None:
    try:
        validate_redirect_url(value)
    except ValidationError:
        return
    parts = urlsplit(value)
    assert parts.scheme.lower() == "https"
    assert "@" not in parts.netloc
    assert len(value) <= 500


# --- validate_logo / logo_upload_to ------------------------------------------


def _image(fmt: str, size: tuple[int, int] = (8, 8), name: str = "logo") -> Any:
    buf = io.BytesIO()
    Image.new("RGB", size, "red").save(buf, format=fmt)
    return SimpleUploadedFile(name, buf.getvalue())


def _chunk(kind: bytes, data: bytes) -> bytes:
    body = kind + data
    return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))


@pytest.mark.parametrize("fmt", ["PNG", "WEBP", "JPEG", "GIF"])
def test_logo_accepts_allowed_formats(fmt: str) -> None:
    upload = _image(fmt)
    validate_logo(upload)
    assert upload.tell() == 0


def test_logo_refuses_svg_named_png() -> None:
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>1</script></svg>'
    with pytest.raises(ValidationError):
        validate_logo(SimpleUploadedFile("logo.png", svg))


def test_logo_refuses_html_named_png() -> None:
    with pytest.raises(ValidationError):
        validate_logo(SimpleUploadedFile("logo.png", b"<html><body>hi</body></html>"))


def test_logo_refuses_truncated_png() -> None:
    data = _image("PNG", (64, 64)).read()
    with pytest.raises(ValidationError):
        validate_logo(SimpleUploadedFile("logo.png", data[: len(data) // 2]))


def test_logo_refuses_over_2mb() -> None:
    big = SimpleUploadedFile("logo.png", b"\x89PNG" + b"0" * (2 * 1024 * 1024))
    with pytest.raises(ValidationError, match="2 MB"):
        validate_logo(big)


def test_logo_refuses_decompression_bomb() -> None:
    header = b"\x89PNG\r\n\x1a\n" + _chunk(
        b"IHDR", struct.pack(">IIBBBBB", 50_000, 50_000, 8, 2, 0, 0, 0)
    )
    with pytest.raises(ValidationError):
        validate_logo(SimpleUploadedFile("logo.png", header))


def test_logo_refuses_warning_level_bomb(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 10)
    with pytest.raises(ValidationError):
        validate_logo(_image("PNG", (5, 4)))


def test_logo_refuses_unlisted_format() -> None:
    with pytest.raises(ValidationError):
        validate_logo(_image("BMP"))


def test_upload_name_ignores_uploaded_name() -> None:
    buf = _image("PNG")
    buf.name = "../../evil name.PNG"
    instance = InstallSettings(after_logo=buf)
    assert re.fullmatch(r"logos/[0-9a-f]{32}\.png", logo_upload_to(instance, buf.name))


@pytest.mark.parametrize(
    ("fmt", "ext"), [("JPEG", "jpg"), ("WEBP", "webp"), ("GIF", "gif")]
)
def test_upload_name_extension_comes_from_detected_format(fmt: str, ext: str) -> None:
    upload = _image(fmt)
    instance = InstallSettings(after_logo=upload)
    assert logo_upload_to(instance, "whatever.png").endswith("." + ext)
    assert upload.tell() == 0


def test_upload_name_for_non_image_raises() -> None:
    instance = InstallSettings(after_logo=SimpleUploadedFile("x.png", b"nope"))
    with pytest.raises(ValidationError):
        logo_upload_to(instance, "x.png")


# --- clean() -----------------------------------------------------------------


def test_site_redirect_without_url_is_refused() -> None:
    site = InstallSettings(after_mode=AfterMode.REDIRECT)
    with pytest.raises(ValidationError) as err:
        site.clean()
    assert "after_redirect_url" in err.value.message_dict


def test_site_redirect_with_url_passes() -> None:
    InstallSettings(
        after_mode=AfterMode.REDIRECT, after_redirect_url="https://example.com"
    ).clean()


def test_conference_redirect_needs_some_url(db: None) -> None:
    InstallSettings.get()
    conf = Conference(after_mode=AfterMode.REDIRECT)
    with pytest.raises(ValidationError) as err:
        conf.clean()
    assert "after_redirect_url" in err.value.message_dict


def test_conference_redirect_may_use_site_url(db: None) -> None:
    site = InstallSettings.get()
    site.after_redirect_url = "https://example.com"
    site.save()
    Conference(after_mode=AfterMode.REDIRECT).clean()


def test_conference_redirect_with_own_url_passes(db: None) -> None:
    Conference(
        after_mode=AfterMode.REDIRECT, after_redirect_url="https://example.com"
    ).clean()


def test_conference_non_redirect_modes_skip_the_url_rule() -> None:
    Conference(after_mode=AfterMode.PAGE).clean()


def test_markdown_over_20000_chars_is_refused() -> None:
    site = InstallSettings(after_markdown="x" * 20_001)
    with pytest.raises(ValidationError) as err:
        site.full_clean(exclude=["owner_name"])
    assert "after_markdown" in err.value.message_dict
    InstallSettings(after_markdown="x" * 20_000).full_clean()


# --- resolve_after -----------------------------------------------------------

SITE_LOGO = "logos/site.png"
CONF_LOGO = "logos/conf.png"
SITE_URL = "https://example.com/site"
CONF_URL = "https://example.com/conf"


def _site(mode: str, logo: str, md: str, url: str) -> InstallSettings:
    return InstallSettings(
        after_mode=mode, after_logo=logo, after_markdown=md, after_redirect_url=url
    )


@pytest.mark.parametrize("c_logo", ["", CONF_LOGO])
@pytest.mark.parametrize("c_md", ["", "# Conf"])
@pytest.mark.parametrize("c_url", ["", CONF_URL])
@pytest.mark.parametrize("s_logo", ["", SITE_LOGO])
@pytest.mark.parametrize("s_md", ["", "site *text*"])
@pytest.mark.parametrize("s_url", ["", SITE_URL])
def test_fields_fall_back_separately(
    c_logo: str, c_md: str, c_url: str, s_logo: str, s_md: str, s_url: str
) -> None:
    site = _site(AfterMode.REDIRECT, s_logo, s_md, s_url)
    conf = Conference(
        after_mode=AfterMode.INHERIT,
        after_logo=c_logo,
        after_markdown=c_md,
        after_redirect_url=c_url,
    )
    got = resolve_after(site, conf)
    url = c_url or s_url
    md = c_md or s_md
    logo = c_logo or s_logo
    assert got.logo_url == (f"/media/{logo}" if logo else None)
    assert got.html == render_markdown(md)
    if url:
        assert got.mode == "redirect"
        assert got.redirect_url == url
    else:
        assert got.mode == "page"
        assert got.redirect_url is None


def test_inherit_uses_site_mode() -> None:
    conf = Conference(after_mode=AfterMode.INHERIT)
    page = resolve_after(_site(AfterMode.PAGE, "", "", SITE_URL), conf)
    assert page.mode == "page"
    redirect = resolve_after(_site(AfterMode.REDIRECT, "", "", SITE_URL), conf)
    assert redirect.mode == "redirect"


def test_conference_mode_overrides_site_mode() -> None:
    site = _site(AfterMode.REDIRECT, "", "", SITE_URL)
    assert resolve_after(site, Conference(after_mode=AfterMode.PAGE)).mode == "page"
    site = _site(AfterMode.PAGE, "", "", SITE_URL)
    conf = Conference(after_mode=AfterMode.REDIRECT)
    assert resolve_after(site, conf).redirect_url == SITE_URL


def test_no_conference_uses_site_values_only() -> None:
    got = resolve_after(_site(AfterMode.PAGE, SITE_LOGO, "hi", ""), None)
    assert got == AfterContent(
        mode="page",
        logo_url="/media/logos/site.png",
        html=render_markdown("hi"),
        redirect_url=None,
    )


def test_is_empty() -> None:
    assert resolve_after(_site(AfterMode.PAGE, "", "  ", ""), None).is_empty
    assert not resolve_after(_site(AfterMode.PAGE, SITE_LOGO, "", ""), None).is_empty
    assert not resolve_after(_site(AfterMode.PAGE, "", "hi", ""), None).is_empty


def test_after_content_is_frozen() -> None:
    got = resolve_after(_site(AfterMode.PAGE, "", "", ""), None)
    with pytest.raises(PydanticValidationError):
        got.mode = "redirect"


def test_mpo_logo_is_accepted_and_named_jpg() -> None:
    buf = io.BytesIO()
    first = Image.new("RGB", (8, 8), "red")
    first.save(
        buf, format="MPO", save_all=True, append_images=[Image.new("RGB", (8, 8))]
    )
    assert Image.open(io.BytesIO(buf.getvalue())).format == "MPO"
    upload = SimpleUploadedFile("logo.jpg", buf.getvalue())
    validate_logo(upload)
    assert logo_upload_to(InstallSettings(after_logo=upload), "x").endswith(".jpg")


def test_conference_clean_does_not_create_the_site_row(db: None) -> None:
    with pytest.raises(ValidationError):
        Conference(after_mode=AfterMode.REDIRECT).clean()
    assert not InstallSettings.objects.exists()


def test_conference_clean_reports_both_errors_together(db: None) -> None:
    import datetime

    conf = Conference(
        after_mode=AfterMode.REDIRECT,
        starts_on=datetime.date(2026, 5, 2),
        ends_on=datetime.date(2026, 5, 1),
    )
    with pytest.raises(ValidationError) as err:
        conf.clean()
    assert set(err.value.message_dict) == {"ends_on", "after_redirect_url"}


def test_old_code_can_insert_rows_without_after_columns(db: None) -> None:
    """v0.1.2 inserts omit the after_* columns, so the DB must default them."""
    with connection.cursor() as cur:
        cur.execute(
            "INSERT INTO raffle_installsettings (id, owner_name, privacy_note,"
            " followup_ack_text, bd_checkbox_text) VALUES (1, '', '', 'a', 'b')"
        )
        cur.execute(
            "INSERT INTO raffle_conference (name, slug, time_zone, booth_location,"
            " starts_on, ends_on, exclusion_emails, display_token)"
            " VALUES ('N', 'n', 'UTC', '', '2026-01-01', '2026-01-02', '', 't')"
        )
    site = InstallSettings.objects.get(pk=1)
    conf = Conference.objects.get(slug="n")
    assert (site.after_mode, site.after_markdown, site.after_redirect_url) == (
        AfterMode.PAGE,
        "",
        "",
    )
    assert not site.after_logo
    assert (conf.after_mode, conf.after_markdown, conf.after_redirect_url) == (
        AfterMode.INHERIT,
        "",
        "",
    )
    assert not conf.after_logo
