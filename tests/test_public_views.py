import datetime
import io
import logging
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, RequestFactory
from django.urls import reverse
from hypothesis import given
from hypothesis import strategies as st
from PIL import Image

from conference_drawing.raffle.forms import EntryForm
from conference_drawing.raffle.models import (
    AfterMode,
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
    assert "Please enter a valid email address." in response.content.decode()
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
    response = _post(client, conference, leave_blank="http://spam.example", name="")
    assert response.status_code == 303
    assert response["Location"] == reverse("thanks", args=[conference.slug])
    assert Entrant.objects.count() == 0


def test_honeypot_markup_hides_it_from_people_and_screen_readers(
    client: Client, conference: Conference
) -> None:
    html = client.get(reverse("entry", args=[conference.slug])).content.decode()
    wrapper = html.split('<div class="hp" aria-hidden="true">', 1)[1].split("</div>")[0]
    assert 'name="leave_blank"' in wrapper
    assert 'tabindex="-1"' in wrapper and 'autocomplete="off"' in wrapper
    assert 'name="website"' not in html


@pytest.mark.parametrize(
    "answer", ["evil‮txt.exe", "tab\there"], ids=["bidi-override", "tab"]
)
def test_answer_with_bidi_override_or_tab_is_rejected(
    client: Client, conference: Conference, answer: str
) -> None:
    response = _post(client, conference, q_want_to_learn=answer)
    assert response.status_code == 200
    assert "Remove tabs and invisible or control characters." in (
        response.content.decode()
    )
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


def test_wrap_up_landing_mid_submit_shows_closed(
    client: Client, conference: Conference, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The view's early check passed; the form's locked recheck catches it."""
    real = EntryForm.is_valid

    def wrap_up_then_validate(self: EntryForm) -> bool:
        Conference.objects.filter(pk=conference.pk).update(wrapped_up_at=DRAW_AT)
        return real(self)

    monkeypatch.setattr(EntryForm, "is_valid", wrap_up_then_validate)
    posted = _post(client, conference)
    assert posted.status_code == 200
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


@pytest.mark.usefixtures("frozen_ratelimit_clock")
def test_post_rate_limited_per_ip(client: Client, conference: Conference) -> None:
    kwargs: dict[str, Any] = {"REMOTE_ADDR": "203.0.113.5"}
    url = reverse("entry", args=[conference.slug])
    statuses = [client.post(url, {}, **kwargs).status_code for _ in range(60)]
    assert set(statuses) == {200}
    blocked = client.post(url, {}, **kwargs)
    assert blocked.status_code == 429
    assert "too quickly" in blocked.content.decode()
    assert blocked["Cache-Control"] == "private, no-store"
    # A different client is unaffected, and GETs are never limited.
    assert client.post(url, {}, REMOTE_ADDR="203.0.113.6").status_code == 200
    assert client.get(url, **kwargs).status_code == 200


@pytest.mark.usefixtures("frozen_ratelimit_clock")
def test_rate_limit_uses_rightmost_forwarded_for(
    client: Client, conference: Conference
) -> None:
    url = reverse("entry", args=[conference.slug])
    for i in range(60):
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
    _post(client, conference, name="Zedekiah Quux", leave_blank="x")
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


@given(spoofed=st.lists(st.text()), real=st.ip_addresses(v=4))
def test_client_ip_ignores_whatever_the_client_put_before_caddys_entry(
    spoofed: list[str], real: Any
) -> None:
    # Caddy (the one trusted proxy) appends the connecting address last;
    # anything before it came from the client.
    header = ", ".join([*spoofed, str(real)])
    assert _ip(HTTP_X_FORWARDED_FOR=header, REMOTE_ADDR="") == str(real)


@given(xff=st.text(), remote=st.text())
def test_client_ip_never_raises(xff: str, remote: str) -> None:
    result = _ip(HTTP_X_FORWARDED_FOR=xff, REMOTE_ADDR=remote)
    assert isinstance(result, str)
    assert result


# --- wrapped-up entry page and the site home page -----------------------------


def _png(name: str = "logo.png") -> Any:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), "red").save(buf, format="PNG")
    return SimpleUploadedFile(name, buf.getvalue(), content_type="image/png")


@pytest.fixture
def wrapped(conference: Conference) -> Conference:
    Conference.objects.filter(pk=conference.pk).update(wrapped_up_at=DRAW_AT)
    conference.refresh_from_db()
    return conference


def _main(html: str) -> str:
    return html.split("<main>")[1].split("</main>")[0]


def test_wrapped_up_entry_with_nothing_configured_is_unchanged(
    client: Client, wrapped: Conference
) -> None:
    html = client.get(reverse("entry", args=[wrapped.slug])).content.decode()
    main = _main(html)
    assert f"<h1>{wrapped.name}</h1>" in main
    assert f"<p>{CLOSED}</p>" in main
    assert "after-logo" not in html
    assert "<a " not in main


def test_wrapped_up_entry_shows_conference_markdown_and_logo(
    client: Client,
    wrapped: Conference,
    settings: Any,
    tmp_path: Path,
) -> None:
    settings.MEDIA_ROOT = tmp_path
    wrapped.after_markdown = "See **our site** at [here](https://example.com/x)."
    wrapped.after_logo = _png()
    wrapped.save()
    html = client.get(reverse("entry", args=[wrapped.slug])).content.decode()
    main = _main(html)
    assert "<strong>our site</strong>" in main
    assert 'href="https://example.com/x"' in main
    assert re.search(r'<img class="after-logo" src="/media/logos/[^"]+" alt="">', main)
    assert f"<p>{CLOSED}</p>" in main


def test_logo_alt_is_owner_name_when_set(
    client: Client, wrapped: Conference, settings: Any, tmp_path: Path
) -> None:
    settings.MEDIA_ROOT = tmp_path
    InstallSettings.objects.update_or_create(
        pk=1, defaults={"owner_name": "Example Owner", "after_logo": _png()}
    )
    html = client.get(reverse("entry", args=[wrapped.slug])).content.decode()
    assert re.search(r'<img class="after-logo" src="[^"]+" alt="Example Owner">', html)


def test_wrapped_up_entry_redirects_302(client: Client, wrapped: Conference) -> None:
    wrapped.after_mode = AfterMode.REDIRECT
    wrapped.after_redirect_url = "https://example.com/next"
    wrapped.save()
    response = client.get(reverse("entry", args=[wrapped.slug]))
    assert response.status_code == 302
    assert response["Location"] == "https://example.com/next"


def test_wrapped_up_entry_uses_site_redirect_when_inheriting(
    client: Client, wrapped: Conference
) -> None:
    install = InstallSettings.get()
    install.after_mode = AfterMode.REDIRECT
    install.after_redirect_url = "https://example.com/site"
    install.save()
    response = client.get(reverse("entry", args=[wrapped.slug]))
    assert response.status_code == 302
    assert response["Location"] == "https://example.com/site"


def test_head_on_wrapped_up_entry_redirects(
    client: Client, wrapped: Conference
) -> None:
    wrapped.after_mode = AfterMode.REDIRECT
    wrapped.after_redirect_url = "https://example.com/next"
    wrapped.save()
    response = client.head(reverse("entry", args=[wrapped.slug]))
    assert response.status_code == 302


def test_head_on_page_mode_is_200(client: Client, wrapped: Conference) -> None:
    assert client.head(reverse("entry", args=[wrapped.slug])).status_code == 200


def test_post_to_wrapped_up_entry_shows_closed_page_even_in_redirect_mode(
    client: Client, wrapped: Conference
) -> None:
    wrapped.after_mode = AfterMode.REDIRECT
    wrapped.after_redirect_url = "https://example.com/next"
    wrapped.save()
    response = _post(client, wrapped)
    assert response.status_code == 200
    assert CLOSED in response.content.decode()
    assert Entrant.objects.count() == 0


def test_open_conference_entry_ignores_after_settings(
    client: Client, conference: Conference
) -> None:
    conference.after_mode = AfterMode.REDIRECT
    conference.after_redirect_url = "https://example.com/next"
    conference.after_markdown = "Hello after"
    conference.save()
    response = client.get(reverse("entry", args=[conference.slug]))
    assert response.status_code == 200
    assert "<form" in response.content.decode()
    assert "Hello after" not in response.content.decode()


def test_home_404_when_nothing_configured(client: Client, db: None) -> None:
    assert client.get(reverse("home")).status_code == 404
    assert client.head(reverse("home")).status_code == 404


def test_home_page_mode_shows_site_content(
    client: Client, conference: Conference
) -> None:
    install = InstallSettings.get()
    install.after_markdown = "Site **news**"
    install.save()
    html = client.get(reverse("home")).content.decode()
    assert "<strong>news</strong>" in _main(html)
    assert conference.name not in html
    assert CLOSED not in html


def test_home_redirect_mode_302(client: Client, db: None) -> None:
    install = InstallSettings.get()
    install.after_mode = AfterMode.REDIRECT
    install.after_redirect_url = "https://example.com/site"
    install.save()
    response = client.get(reverse("home"))
    assert response.status_code == 302
    assert response["Location"] == "https://example.com/site"


def test_home_rejects_post(client: Client, db: None) -> None:
    assert client.post(reverse("home")).status_code == 405


def test_after_pages_keep_security_headers(client: Client, wrapped: Conference) -> None:
    install = InstallSettings.get()
    install.after_markdown = "Site text"
    install.save()
    reference = client.get(reverse("privacy"))
    assert reference["Content-Security-Policy"]
    for url in (reverse("entry", args=[wrapped.slug]), reverse("home")):
        response = client.get(url)
        assert response.status_code == 200
        for header in ("Content-Security-Policy", "X-Robots-Tag", "Cache-Control"):
            assert response[header] == reference[header]
