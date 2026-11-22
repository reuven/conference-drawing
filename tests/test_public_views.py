import datetime
import logging
from collections.abc import Callable
from typing import Any

import pytest
from django.test import Client, RequestFactory
from django.urls import reverse
from hypothesis import given
from hypothesis import strategies as st

from conference_drawing.raffle.models import (
    Conference,
    Drawing,
    Entrant,
    InstallSettings,
    create_default_questions,
)
from conference_drawing.raffle.views.public import client_ip

THANKS = "Thanks — you're in the drawing."
CLOSED = "Entries for this conference are closed."

# 2027-05-11 21:00 UTC is 14:00 in Los Angeles (PDT), 17:00 in New York.
DRAW_AT = datetime.datetime(2027, 5, 11, 21, 0, tzinfo=datetime.UTC)


@pytest.fixture
def conference(conference_factory: Callable[..., Conference]) -> Conference:
    conf = conference_factory(slug="pycon", booth_location="Booth 12")
    create_default_questions(conf)
    return conf


def _post(client: Client, conference: Conference, **overrides: Any) -> Any:
    data: dict[str, Any] = {
        "name": "Ada Lovelace",
        "email": "ada@example.com",
        "followup_ack": "on",
    }
    return client.post(reverse("entry", args=[conference.slug]), {**data, **overrides})


def test_entry_page_renders_form(client: Client, conference: Conference) -> None:
    response = client.get(reverse("entry", args=[conference.slug]))
    html = response.content.decode()
    assert response.status_code == 200
    assert 'for="id_name"' in html
    assert 'for="id_email"' in html
    assert "What do you most want to learn?" in html
    assert 'name="bd_optin"' in html
    assert "checked" not in html
    assert "style=" not in html
    assert "<script" not in html
    assert response["Cache-Control"] == "private, no-store"
    assert "default-src 'self'" in response["Content-Security-Policy"]


def test_entry_page_lists_books_and_local_drawing_times(
    client: Client,
    conference: Conference,
    drawing_factory: Callable[..., Drawing],
    prize_line_factory: Callable[..., Any],
    book_factory: Callable[..., Any],
) -> None:
    book = book_factory(title="Fluent Python")
    d1 = drawing_factory(conference, scheduled_at=DRAW_AT)
    d2 = drawing_factory(conference, scheduled_at=DRAW_AT + datetime.timedelta(days=1))
    done = drawing_factory(
        conference, scheduled_at=DRAW_AT, status=Drawing.Status.FINISHED
    )
    prize_line_factory(d1, book=book)
    prize_line_factory(d2, book=book)
    prize_line_factory(done, book=book_factory(title="Finished Book"))

    html = client.get(reverse("entry", args=[conference.slug])).content.decode()

    assert (
        "Drawings at Tue 2:00 PM and Wed 2:00 PM at Booth 12. "
        "You must be present to win a printed book."
    ) in html
    assert html.count("Fluent Python") == 1
    assert "Finished Book" not in html


def test_entry_page_without_drawings_omits_schedule(
    client: Client, conference: Conference
) -> None:
    html = client.get(reverse("entry", args=[conference.slug])).content.decode()
    assert "You must be present" not in html


def test_thanks_page_repeats_must_be_present_line(
    client: Client,
    conference: Conference,
    drawing_factory: Callable[..., Drawing],
) -> None:
    drawing_factory(conference, scheduled_at=DRAW_AT)
    html = client.get(reverse("thanks", args=[conference.slug])).content.decode()
    assert THANKS in html
    assert (
        "Drawings at Tue 2:00 PM at Booth 12. "
        "You must be present to win a printed book."
    ) in html


def test_valid_post_creates_entrant_and_redirects_to_thanks(
    client: Client, conference: Conference
) -> None:
    response = _post(client, conference, q_want_to_learn="Rust")
    assert response.status_code == 303
    assert response["Location"] == reverse("thanks", args=[conference.slug])
    entrant = Entrant.objects.get()
    assert entrant.conference == conference
    assert entrant.answers == {"want_to_learn": "Rust"}


def test_invalid_post_redisplays_form_with_errors(
    client: Client, conference: Conference
) -> None:
    response = _post(client, conference, email="not-an-email")
    assert response.status_code == 200
    assert "Enter a valid email address." in response.content.decode()
    assert Entrant.objects.count() == 0


def test_repeat_and_new_entries_get_identical_thanks(
    client: Client, conference: Conference
) -> None:
    new = _post(client, conference)
    repeat = _post(client, conference, email=" ADA@example.com ", name="Other")
    assert (new.status_code, new["Location"]) == (
        repeat.status_code,
        repeat["Location"],
    )
    assert new.content == repeat.content

    page_new = client.get(new["Location"])
    page_repeat = client.get(repeat["Location"])
    assert page_new.status_code == page_repeat.status_code == 200
    assert page_new.content == page_repeat.content
    assert THANKS in page_new.content.decode()
    assert Entrant.objects.count() == 1


def test_honeypot_filled_shows_thanks_but_saves_nothing(
    client: Client, conference: Conference
) -> None:
    # Invalid on purpose: a bot must not be told which field failed.
    response = _post(client, conference, website="http://spam.example", name="")
    assert response.status_code == 303
    assert response["Location"] == reverse("thanks", args=[conference.slug])
    assert Entrant.objects.count() == 0


def test_wrapped_up_conference_shows_closed(
    client: Client, conference: Conference
) -> None:
    conference.wrapped_up_at = DRAW_AT
    conference.save()
    got = client.get(reverse("entry", args=[conference.slug]))
    assert got.status_code == 200
    assert CLOSED in got.content.decode()
    assert "<form" not in got.content.decode()
    posted = _post(client, conference)
    assert CLOSED in posted.content.decode()
    assert Entrant.objects.count() == 0


def test_unknown_slug_404(client: Client, db: None) -> None:
    assert client.get(reverse("entry", args=["nope"])).status_code == 404
    assert client.get(reverse("thanks", args=["nope"])).status_code == 404
    assert client.post(reverse("entry", args=["nope"]), {}).status_code == 404


def test_entry_rejects_other_methods(client: Client, conference: Conference) -> None:
    assert client.put(reverse("entry", args=[conference.slug])).status_code == 405


def test_post_without_csrf_token_is_refused(conference: Conference) -> None:
    strict = Client(enforce_csrf_checks=True)
    response = strict.post(reverse("entry", args=[conference.slug]), {"name": "x"})
    assert response.status_code == 403
    assert Entrant.objects.count() == 0


def test_post_rate_limited_per_ip(client: Client, conference: Conference) -> None:
    kwargs: dict[str, Any] = {"REMOTE_ADDR": "203.0.113.5"}
    url = reverse("entry", args=[conference.slug])
    statuses = [client.post(url, {}, **kwargs).status_code for _ in range(10)]
    assert set(statuses) == {200}
    blocked = client.post(url, {}, **kwargs)
    assert blocked.status_code == 429
    assert "too quickly" in blocked.content.decode()
    assert blocked["Cache-Control"] == "private, no-store"
    # A different client is unaffected, and GETs are never limited.
    assert client.post(url, {}, REMOTE_ADDR="203.0.113.6").status_code == 200
    assert client.get(url, **kwargs).status_code == 200


def test_rate_limit_uses_rightmost_forwarded_for(
    client: Client, conference: Conference
) -> None:
    url = reverse("entry", args=[conference.slug])
    for i in range(10):
        spoof: dict[str, Any] = {"HTTP_X_FORWARDED_FOR": f"10.0.0.{i}, 198.51.100.7"}
        assert client.post(url, {}, **spoof).status_code == 200
    spoof = {"HTTP_X_FORWARDED_FOR": "10.9.9.9, 198.51.100.7"}
    assert client.post(url, {}, **spoof).status_code == 429


def test_entry_never_logs_personal_data(
    client: Client, conference: Conference, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    _post(client, conference, name="Zedekiah Quux", email="zed.quux@example.com")
    _post(client, conference, name="Zedekiah Quux", email="zed.quux@example.com")
    _post(client, conference, name="Zedekiah Quux", website="x")
    assert "Zedekiah" not in caplog.text
    assert "quux" not in caplog.text.lower()


def test_privacy_page_escapes_note_and_keeps_line_breaks(
    client: Client, db: None
) -> None:
    install = InstallSettings.get()
    install.owner_name = "Grace"
    install.privacy_note = "Line one <script>alert(1)</script>\nLine two"
    install.save()
    html = client.get(reverse("privacy")).content.decode()
    assert "<script>alert" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;<br>Line two" in html
    assert "Grace" in html


def test_privacy_page_without_note(client: Client, db: None) -> None:
    response = client.get(reverse("privacy"))
    assert response.status_code == 200
    assert "No privacy note" in response.content.decode()


# --- client_ip -------------------------------------------------------------


def _ip(**meta: Any) -> str:
    return client_ip("group", RequestFactory().get("/", **meta))


def test_client_ip_takes_rightmost_forwarded_entry() -> None:
    assert _ip(HTTP_X_FORWARDED_FOR="1.1.1.1, 2.2.2.2 , 3.3.3.3") == "3.3.3.3"


def test_client_ip_falls_back_to_remote_addr() -> None:
    assert _ip(REMOTE_ADDR="192.0.2.9") == "192.0.2.9"
    assert _ip(HTTP_X_FORWARDED_FOR="", REMOTE_ADDR="192.0.2.9") == "192.0.2.9"


def test_client_ip_masks_ipv6_to_64() -> None:
    a = _ip(HTTP_X_FORWARDED_FOR="2001:db8:1:2:aaaa::1")
    b = _ip(HTTP_X_FORWARDED_FOR="2001:db8:1:2:bbbb::2")
    c = _ip(HTTP_X_FORWARDED_FOR="2001:db8:1:3::1")
    assert a == b == "2001:db8:1:2::/64"
    assert c != a


def test_client_ip_maps_ipv4_mapped_ipv6_to_ipv4() -> None:
    assert _ip(HTTP_X_FORWARDED_FOR="::ffff:192.0.2.1") == "192.0.2.1"


def test_client_ip_garbage_forwarded_falls_back_to_remote_addr() -> None:
    assert _ip(HTTP_X_FORWARDED_FOR="not-an-ip", REMOTE_ADDR="192.0.2.9") == "192.0.2.9"


def test_client_ip_with_nothing_usable_still_returns_a_string() -> None:
    assert _ip(REMOTE_ADDR="") == "unknown"
    assert _ip(REMOTE_ADDR="garbage") == "unknown"


@given(xff=st.text(), remote=st.text())
def test_client_ip_never_raises(xff: str, remote: str) -> None:
    result = _ip(HTTP_X_FORWARDED_FOR=xff, REMOTE_ADDR=remote)
    assert isinstance(result, str)
    assert result
