import datetime
import importlib
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import segno
from django.contrib.auth.models import User
from django.test import Client
from django.urls import clear_url_caches, reverse

from conference_drawing import urls as cd_urls
from conference_drawing.raffle.models import Conference, Drawing, PrizeLine
from tests.factories import make_book

URL = "booth-flyer"


@pytest.fixture
def owner(client: Client, db: None) -> Client:
    user = User.objects.create_user("owner", password="pw", is_staff=True)  # noqa: S106
    client.force_login(user)
    return client


@pytest.fixture
def conference(conference_factory: Callable[..., Conference]) -> Conference:
    return conference_factory(slug="pycon")


def _flyer(client: Client, conference: Conference) -> str:
    response = client.get(reverse(URL, args=[conference.slug]))
    assert response.status_code == 200
    return response.content.decode()


def test_flyer_requires_login(client: Client, conference: Conference) -> None:
    response = client.get(reverse(URL, args=[conference.slug]))
    assert response.status_code == 302
    assert response["Location"].startswith(reverse("admin:login"))


def test_flyer_unknown_conference_is_404(owner: Client) -> None:
    assert owner.get(reverse(URL, args=["nope"])).status_code == 404


def test_flyer_qr_encodes_entry_url(owner: Client, conference: Conference) -> None:
    html = _flyer(owner, conference)
    entry_url = "http://testserver" + reverse("entry", args=["pycon"])
    expected = segno.make(entry_url, error="m").svg_inline(scale=10)
    path_data = re.search(r' d="([^"]+)"', expected)
    assert path_data is not None
    assert path_data.group(1) in html
    # Short typeable form under the code.
    assert "testserver/c/pycon/" in html


def test_flyer_qr_is_https_behind_the_proxy(
    owner: Client, conference: Conference, settings: Any
) -> None:
    """Caddy terminates TLS and says so; the printed code must not be http://."""
    settings.ALLOWED_HOSTS = ["raffle.example.com"]
    response = owner.get(
        reverse(URL, args=[conference.slug]),
        HTTP_HOST="raffle.example.com",
        HTTP_X_FORWARDED_PROTO="https",
    )
    html = response.content.decode()
    want = "https://raffle.example.com" + reverse("entry", args=["pycon"])
    http = want.replace("https://", "http://", 1)
    for url, present in ((want, True), (http, False)):
        svg = segno.make(url, error="m").svg_inline(scale=10)
        path_data = re.search(r' d="([^"]+)"', svg)
        assert path_data is not None
        assert (path_data.group(1) in html) is present, url


def test_flyer_qr_carries_no_inline_style(
    owner: Client, conference: Conference
) -> None:
    html = _flyer(owner, conference)
    svg = re.search(r"<svg.*?</svg>", html, re.S)
    assert svg is not None
    assert "style" not in svg.group(0)
    assert "style=" not in html
    assert "<script" not in html


def test_flyer_links_print_css(owner: Client, conference: Conference) -> None:
    html = _flyer(owner, conference)
    assert "raffle/print.css" in html


def test_flyer_lists_each_book_once(
    owner: Client,
    conference: Conference,
    drawing_factory: Callable[..., Drawing],
    prize_line_factory: Callable[..., PrizeLine],
) -> None:
    shared = make_book(title="Shared Title", cover="covers/shared.png")
    other = make_book(title="Plain Title")
    first = drawing_factory(conference)
    second = drawing_factory(
        conference,
        scheduled_at=datetime.datetime(2027, 5, 12, 21, 0, tzinfo=datetime.UTC),
    )
    prize_line_factory(first, book=shared)
    prize_line_factory(first, book=other)
    prize_line_factory(second, book=shared)
    html = _flyer(owner, conference)
    assert html.count("Shared Title") == 2  # <img alt> + caption, one book
    assert html.count("Plain Title") == 1
    assert html.count('src="/media/covers/shared.png"') == 1
    assert "/media/" not in html.replace("/media/covers/shared.png", "")


def test_flyer_schedule_in_local_time(
    owner: Client,
    conference: Conference,
    drawing_factory: Callable[..., Drawing],
) -> None:
    # 21:00 UTC is 2:00 PM PDT on Tue May 11; 03:30 UTC May 13 is 8:30 PM PDT May 12.
    drawing_factory(conference)
    drawing_factory(
        conference,
        scheduled_at=datetime.datetime(2027, 5, 13, 3, 30, tzinfo=datetime.UTC),
    )
    html = _flyer(owner, conference)
    assert "Tuesday, May 11" in html
    assert "2:00 PM" in html
    assert "Wednesday, May 12" in html
    assert "8:30 PM" in html
    assert "9:00 PM" not in html
    assert "Booth 12" in html


def test_flyer_omits_finished_drawings(
    owner: Client,
    conference: Conference,
    drawing_factory: Callable[..., Drawing],
) -> None:
    drawing_factory(conference, status=Drawing.Status.FINISHED)
    html = _flyer(owner, conference)
    assert "2:00 PM" not in html


def test_flyer_has_must_be_present_line(owner: Client, conference: Conference) -> None:
    html = _flyer(owner, conference)
    assert "You must be present to win a printed book." in html


def test_conference_page_links_to_flyer(owner: Client, conference: Conference) -> None:
    response = owner.get(reverse("booth-conference", args=["pycon"]))
    assert reverse(URL, args=["pycon"]) in response.content.decode()


def test_print_css_is_one_page_and_black_and_white() -> None:
    css = (Path(cd_urls.__file__).parent / "raffle/static/raffle/print.css").read_text()
    assert "@media print" in css
    assert "@page" in css
    assert not re.search(r"#(?!000|fff)[0-9a-fA-F]{3,6}\b", css)  # black/white only


def test_media_url_setting(settings: Any) -> None:
    assert settings.MEDIA_URL == "/media/"


def test_media_served_in_debug(settings: Any, tmp_path: Path, client: Client) -> None:
    (tmp_path / "covers").mkdir()
    (tmp_path / "covers" / "a.png").write_bytes(b"png")
    settings.DEBUG = True
    settings.MEDIA_ROOT = tmp_path
    importlib.reload(cd_urls)
    clear_url_caches()
    try:
        assert client.get("/media/covers/a.png").status_code == 200
    finally:
        settings.DEBUG = False
        importlib.reload(cd_urls)
        clear_url_caches()
    assert client.get("/media/covers/a.png").status_code == 404
