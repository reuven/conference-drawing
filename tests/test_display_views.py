import datetime
import re
from collections.abc import Callable

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse

from conference_drawing.raffle.models import Conference, Drawing, Pick, PrizeLine
from conference_drawing.raffle.views import display
from tests.factories import make_book

S = Pick.Status
NOW = datetime.datetime(2027, 5, 11, 20, 0, tzinfo=datetime.UTC)


@pytest.fixture(autouse=True)
def _fixed_now(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(display, "_now", lambda: NOW)


@pytest.fixture
def conference(conference_factory: Callable[..., Conference]) -> Conference:
    return conference_factory(slug="pycon")


def _panel(client: Client, conference: Conference) -> str:
    response = client.get(reverse("display-panel", args=[conference.display_token]))
    assert response.status_code == 200
    return response.content.decode()


def _open_drawing_with_picks(
    conference: Conference,
    drawing_factory: Callable[..., Drawing],
    prize_line_factory: Callable[..., PrizeLine],
    entrant_factory: Callable[..., object],
    pick_factory: Callable[..., Pick],
    statuses: dict[str, str],
    **drawing_kwargs: object,
) -> Drawing:
    drawing = drawing_factory(conference, **drawing_kwargs)
    line = prize_line_factory(drawing, quantity=len(statuses))
    for slot, (name, status) in enumerate(statuses.items(), start=1):
        entrant = entrant_factory(conference, name=name)
        pick_factory(line, entrant, slot=slot, status=status)
    return drawing


def test_wrong_token_404(client: Client, conference: Conference) -> None:
    for name in ("display", "display-panel"):
        assert client.get(reverse(name, args=["nope"])).status_code == 404
        assert client.get(reverse(name, args=["x" * 40])).status_code == 404


def test_page_with_right_token_and_no_login(
    client: Client, conference: Conference
) -> None:
    response = client.get(reverse("display", args=[conference.display_token]))
    html = response.content.decode()
    assert response.status_code == 200
    assert 'hx-trigger="every 2s"' in html
    assert re.search(r'<script src="[^"]*htmx-2\.0\.11\.min\.js"', html)
    assert 'name="htmx-config"' in html
    assert "display.css" in html
    assert "style=" not in html
    assert "<script>" not in html


def test_panel_shows_winners_during_open_drawing(
    client: Client,
    conference: Conference,
    drawing_factory: Callable[..., Drawing],
    prize_line_factory: Callable[..., PrizeLine],
    entrant_factory: Callable[..., object],
    pick_factory: Callable[..., Pick],
) -> None:
    drawing = drawing_factory(conference, status=Drawing.Status.OPEN)
    line = prize_line_factory(drawing, quantity=2, book=make_book(title="Fluent Py"))
    other = prize_line_factory(drawing, book=make_book(title="Pandas Work"), position=1)
    pick_factory(line, entrant_factory(conference, name="Ada Lovelace"), slot=1)
    pick_factory(
        line, entrant_factory(conference, name="Bob Ng"), slot=2, status=S.CLAIMED
    )
    pick_factory(
        other,
        entrant_factory(conference, name="Cy Digital"),
        status=S.DIGITAL,
    )
    html = _panel(client, conference)
    assert html.index("Fluent Py") < html.index("Ada Lovelace")
    assert html.index("Ada Lovelace") < html.index("Bob Ng")
    assert html.index("Pandas Work") < html.index("Cy Digital")
    assert "Drawing now" in html


def test_not_here_and_void_picks_not_shown(
    client: Client,
    conference: Conference,
    drawing_factory: Callable[..., Drawing],
    prize_line_factory: Callable[..., PrizeLine],
    entrant_factory: Callable[..., object],
    pick_factory: Callable[..., Pick],
) -> None:
    _open_drawing_with_picks(
        conference,
        drawing_factory,
        prize_line_factory,
        entrant_factory,
        pick_factory,
        {"Gone Gail": S.NOT_HERE, "Void Vic": S.VOID, "Here Hal": S.PENDING},
        status=Drawing.Status.OPEN,
    )
    html = _panel(client, conference)
    assert "Here Hal" in html
    assert "Gone Gail" not in html
    assert "Void Vic" not in html


def test_panel_never_shows_email_or_codes(
    client: Client,
    conference: Conference,
    drawing_factory: Callable[..., Drawing],
    prize_line_factory: Callable[..., PrizeLine],
    entrant_factory: Callable[..., object],
    pick_factory: Callable[..., Pick],
) -> None:
    drawing = drawing_factory(conference, status=Drawing.Status.OPEN)
    line = prize_line_factory(
        drawing,
        kind=PrizeLine.Kind.DIGITAL,
        shared_code="SECRETCODE",
        url_template="https://x.example/{code}",
    )
    entrant = entrant_factory(conference, name="Dee", email="dee@secret.example")
    pick_factory(line, entrant, status=S.DIGITAL)
    html = _panel(client, conference)
    assert "Dee" in html
    for leak in ("dee@secret.example", "SECRETCODE", "x.example"):
        assert leak not in html


def test_panel_shows_next_drawing_and_entry_count_when_idle(
    client: Client,
    conference: Conference,
    drawing_factory: Callable[..., Drawing],
    entrant_factory: Callable[..., object],
) -> None:
    drawing_factory(
        conference,
        scheduled_at=datetime.datetime(2027, 5, 12, 21, 0, tzinfo=datetime.UTC),
    )
    drawing_factory(
        conference,
        scheduled_at=datetime.datetime(2027, 5, 11, 21, 0, tzinfo=datetime.UTC),
    )
    for _ in range(3):
        entrant_factory(conference)
    html = _panel(client, conference)
    assert "Tue 2:00 PM" in html  # 21:00 UTC is 2 pm in Los Angeles (PDT)
    assert "Wed" not in html
    assert re.search(r">\s*3\s*<", html)


def test_idle_with_no_drawings(client: Client, conference: Conference) -> None:
    html = _panel(client, conference)
    assert "No drawings scheduled" in html
    assert re.search(r">\s*0\s*<", html)


@pytest.mark.parametrize(
    ("age_minutes", "shown"), [(29, True), (30, True), (31, False)]
)
def test_recently_closed_drawing_shown_for_30_minutes(
    client: Client,
    conference: Conference,
    drawing_factory: Callable[..., Drawing],
    prize_line_factory: Callable[..., PrizeLine],
    entrant_factory: Callable[..., object],
    pick_factory: Callable[..., Pick],
    age_minutes: int,
    shown: bool,
) -> None:
    _open_drawing_with_picks(
        conference,
        drawing_factory,
        prize_line_factory,
        entrant_factory,
        pick_factory,
        {"Winner Wes": S.CLAIMED},
        status=Drawing.Status.FINISHED,
        closed_at=NOW - datetime.timedelta(minutes=age_minutes),
    )
    html = _panel(client, conference)
    assert ("Winner Wes" in html) is shown
    assert ("Entries so far" in html) is not shown


def test_open_drawing_beats_recently_closed(
    client: Client,
    conference: Conference,
    drawing_factory: Callable[..., Drawing],
    prize_line_factory: Callable[..., PrizeLine],
    entrant_factory: Callable[..., object],
    pick_factory: Callable[..., Pick],
) -> None:
    for name, kwargs in (
        ("Old Olga", {"status": Drawing.Status.FINISHED, "closed_at": NOW}),
        ("New Nate", {"status": Drawing.Status.OPEN}),
    ):
        _open_drawing_with_picks(
            conference,
            drawing_factory,
            prize_line_factory,
            entrant_factory,
            pick_factory,
            {name: S.PENDING},
            **kwargs,
        )
    html = _panel(client, conference)
    assert "New Nate" in html
    assert "Old Olga" not in html


def test_other_conference_data_not_shown(
    client: Client,
    conference: Conference,
    conference_factory: Callable[..., Conference],
    drawing_factory: Callable[..., Drawing],
    prize_line_factory: Callable[..., PrizeLine],
    entrant_factory: Callable[..., object],
    pick_factory: Callable[..., Pick],
) -> None:
    other = conference_factory()
    _open_drawing_with_picks(
        other,
        drawing_factory,
        prize_line_factory,
        entrant_factory,
        pick_factory,
        {"Elsewhere Eve": S.PENDING},
        status=Drawing.Status.OPEN,
    )
    assert "Elsewhere Eve" not in _panel(client, conference)


@pytest.mark.parametrize("name", ["display", "display-panel"])
def test_display_response_no_store(
    client: Client, conference: Conference, name: str
) -> None:
    response = client.get(reverse(name, args=[conference.display_token]))
    assert response["Cache-Control"] == "private, no-store"
    assert response["X-Robots-Tag"] == "noindex, nofollow"


def test_display_is_get_only(client: Client, conference: Conference) -> None:
    url = reverse("display-panel", args=[conference.display_token])
    assert client.post(url).status_code == 405


def test_booth_conference_shows_display_link_to_owner(
    client: Client, conference: Conference
) -> None:
    url = reverse("booth-conference", args=[conference.slug])
    assert client.get(url).status_code == 302
    client.force_login(User.objects.create_user("o", password="pw", is_staff=True))  # noqa: S106
    html = client.get(url).content.decode()
    assert reverse("display", args=[conference.display_token]) in html


def test_real_clock_is_timezone_aware(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.undo()
    assert display._now().tzinfo is not None
