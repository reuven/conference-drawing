"""Wrap-up: lock the conference and hand every entrant to Drip through the outbox."""

import datetime
import logging
from collections.abc import Callable
from typing import Any

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse

from conference_drawing.raffle import wrapup
from conference_drawing.raffle.draw import DrawingError, start_drawing
from conference_drawing.raffle.models import (
    Conference,
    Drawing,
    Entrant,
    InstallSettings,
    OutboxJob,
    Pick,
    PrizeLine,
)
from conference_drawing.raffle.wrapup import WrapUpResult, wrap_up

NOW = datetime.datetime(2027, 5, 12, 18, 0, tzinfo=datetime.UTC)
OPTIN = datetime.datetime(2027, 5, 11, 17, 0, tzinfo=datetime.UTC)
CONSENT = "Also subscribe me to the newsletter"


@pytest.fixture(autouse=True)
def _fake_drip(settings: Any) -> None:
    settings.DRIP_ACCOUNT_ID = "1234567"
    settings.DRIP_API_KEY = "fake-key"


@pytest.fixture
def conference(conference_factory: Callable[..., Conference]) -> Conference:
    return conference_factory(slug="pycon", name="PyCon US")


@pytest.fixture
def give_pick(
    drawing_factory: Callable[..., Drawing],
    prize_line_factory: Callable[..., PrizeLine],
    pick_factory: Callable[..., Pick],
) -> Callable[[Conference, Entrant, Pick.Status], None]:
    """Give an entrant a pick with the given status in a finished drawing."""

    def give(conference: Conference, entrant: Entrant, status: Pick.Status) -> None:
        drawing = drawing_factory(conference, status=Drawing.Status.FINISHED)
        pick_factory(prize_line_factory(drawing), entrant, status=status)

    return give


def _subscribers() -> dict[str, dict[str, Any]]:
    jobs = OutboxJob.objects.filter(kind=OutboxJob.Kind.SUBSCRIBERS)
    return {s["email"]: s for j in jobs for s in j.payload["subscribers"]}


def test_wrapup_closes_entry(conference: Conference, client: Client) -> None:
    result = wrap_up(conference, now=NOW)
    conference.refresh_from_db()
    assert conference.wrapped_up_at == NOW
    assert result == WrapUpResult(entrants=0, winners=0, jobs=0, skipped=0, excluded=0)
    assert "closed" in client.get(reverse("entry", args=["pycon"])).content.decode()


def test_tags_per_entrant(
    conference: Conference,
    entrant_factory: Callable[..., Entrant],
    give_pick: Callable[[Conference, Entrant, Pick.Status], None],
) -> None:
    won = entrant_factory(conference, email="won@example.com")
    digital = entrant_factory(conference, email="dig@example.com")
    lost = entrant_factory(conference, email="lost@example.com")
    entrant_factory(
        conference,
        email="opt@example.com",
        newsletter_optin_at=OPTIN,
        newsletter_optin_text=CONSENT,
    )
    give_pick(conference, won, Pick.Status.CLAIMED)
    give_pick(conference, digital, Pick.Status.DIGITAL)
    # A not-here pick is not a win (the CSV export agrees).
    give_pick(conference, lost, Pick.Status.NOT_HERE)
    result = wrap_up(conference, now=NOW)
    subs = _subscribers()
    assert subs["won@example.com"]["tags"] == ["pycon-raffle", "pycon-raffle-winner"]
    assert subs["dig@example.com"]["tags"] == ["pycon-raffle", "pycon-raffle-winner"]
    assert subs["lost@example.com"]["tags"] == [
        "pycon-raffle",
        "pycon-raffle-nonwinner",
    ]
    assert subs["opt@example.com"]["tags"] == [
        "pycon-raffle",
        "pycon-raffle-nonwinner",
        "pycon-newsletter-optin",
    ]
    assert (result.entrants, result.winners, result.jobs) == (4, 2, 5)


def test_winner_in_another_conference_does_not_count(
    conference: Conference,
    conference_factory: Callable[..., Conference],
    entrant_factory: Callable[..., Entrant],
    give_pick: Callable[[Conference, Entrant, Pick.Status], None],
) -> None:
    entrant_factory(conference, email="a@example.com")
    other = conference_factory(slug="other")
    give_pick(other, entrant_factory(other, email="a@example.com"), Pick.Status.CLAIMED)
    wrap_up(conference, now=NOW)
    assert _subscribers()["a@example.com"]["tags"][-1] == "pycon-raffle-nonwinner"


def test_eu_consent_only_for_optins(
    conference: Conference, entrant_factory: Callable[..., Entrant]
) -> None:
    InstallSettings.objects.create(newsletter_checkbox_text=CONSENT)
    entrant_factory(
        conference,
        email="in@example.com",
        newsletter_optin_at=OPTIN,
        newsletter_optin_text=CONSENT,
    )
    entrant_factory(conference, email="out@example.com")
    wrap_up(conference, now=NOW)
    subs = _subscribers()
    assert subs["in@example.com"]["eu_consent"] == "granted"
    assert subs["in@example.com"]["eu_consent_message"] == CONSENT
    assert "eu_consent" not in subs["out@example.com"]
    assert "eu_consent_message" not in subs["out@example.com"]
    assert "denied" not in str(subs)


def test_optin_without_recorded_text_uses_install_text(
    conference: Conference, entrant_factory: Callable[..., Entrant]
) -> None:
    InstallSettings.objects.create(newsletter_checkbox_text="Install wording")
    entrant_factory(conference, email="in@example.com", newsletter_optin_at=OPTIN)
    wrap_up(conference, now=NOW)
    assert _subscribers()["in@example.com"]["eu_consent_message"] == "Install wording"


def test_entered_event_carries_answers_by_key(
    conference: Conference, entrant_factory: Callable[..., Entrant]
) -> None:
    entrant_factory(
        conference,
        email="a@example.com",
        answers={"want_to_learn": "Rust", "coding_style": "Mostly AI"},
    )
    wrap_up(conference, now=NOW)
    (job,) = OutboxJob.objects.filter(kind=OutboxJob.Kind.EVENT)
    assert job.status == OutboxJob.Status.PENDING
    assert job.next_attempt_at == NOW
    assert job.payload["email"] == "a@example.com"
    assert job.payload["action"] == "Entered raffle"
    assert job.payload["properties"] == {
        "conference": "PyCon US",
        "want_to_learn": "Rust",
        "coding_style": "Mostly AI",
    }


def test_chunks_of_1000(conference: Conference) -> None:
    Entrant.objects.bulk_create(
        Entrant(conference=conference, name="N", email=f"e{i}@example.com")
        for i in range(1001)
    )
    result = wrap_up(conference, now=NOW)
    sizes = sorted(
        len(j.payload["subscribers"])
        for j in OutboxJob.objects.filter(kind=OutboxJob.Kind.SUBSCRIBERS)
    )
    assert sizes == [1, 1000]
    assert OutboxJob.objects.filter(kind=OutboxJob.Kind.EVENT).count() == 1001
    assert result.jobs == 1003


def test_refuses_with_open_drawing(
    conference: Conference, drawing_factory: Callable[..., Drawing]
) -> None:
    drawing_factory(conference, status=Drawing.Status.OPEN)
    with pytest.raises(DrawingError, match="still open"):
        wrap_up(conference, now=NOW)
    conference.refresh_from_db()
    assert conference.wrapped_up_at is None


def test_refuses_with_pending_pick(
    conference: Conference,
    entrant_factory: Callable[..., Entrant],
    give_pick: Callable[[Conference, Entrant, Pick.Status], None],
) -> None:
    give_pick(conference, entrant_factory(conference), Pick.Status.PENDING)
    with pytest.raises(DrawingError, match="not been claimed"):
        wrap_up(conference, now=NOW)
    assert not OutboxJob.objects.exists()


def test_refuses_twice(
    conference: Conference, entrant_factory: Callable[..., Entrant]
) -> None:
    entrant_factory(conference)
    wrap_up(conference, now=NOW)
    before = OutboxJob.objects.count()
    with pytest.raises(DrawingError, match="already wrapped up"):
        wrap_up(conference, now=NOW + datetime.timedelta(hours=1))
    assert OutboxJob.objects.count() == before
    conference.refresh_from_db()
    assert conference.wrapped_up_at == NOW


def test_without_drip_only_closes_entry(
    conference: Conference, entrant_factory: Callable[..., Entrant], settings: Any
) -> None:
    settings.DRIP_ACCOUNT_ID = ""
    settings.DRIP_API_KEY = ""
    entrant_factory(conference)
    result = wrap_up(conference, now=NOW)
    conference.refresh_from_db()
    assert conference.wrapped_up_at == NOW
    assert result == WrapUpResult(entrants=1, winners=0, jobs=0, skipped=0, excluded=0)
    assert not OutboxJob.objects.exists()


def test_unqueueable_entrant_is_skipped_and_logged_by_id(
    conference: Conference,
    entrant_factory: Callable[..., Entrant],
    caplog: pytest.LogCaptureFixture,
) -> None:
    entrant_factory(conference, email="good@example.com")
    bad = entrant_factory(conference, email="bad@@example")
    with caplog.at_level(logging.INFO):
        result = wrap_up(conference, now=NOW)
    assert (result.entrants, result.skipped, result.jobs) == (2, 1, 2)
    assert list(_subscribers()) == ["good@example.com"]
    conference.refresh_from_db()
    assert conference.wrapped_up_at == NOW
    assert f"entrant {bad.pk}" in caplog.text
    assert "bad@@example" not in caplog.text
    assert "good@example.com" not in caplog.text


def test_blank_name_does_not_break_wrapup(
    conference: Conference, entrant_factory: Callable[..., Entrant]
) -> None:
    entrant = entrant_factory(conference, email="a@example.com")
    Entrant.objects.filter(pk=entrant.pk).update(name="   ")
    result = wrap_up(conference, now=NOW)
    assert result.skipped == 0
    assert "first_name" not in _subscribers()["a@example.com"]


def test_wrapup_logs_counts_only(
    conference: Conference,
    entrant_factory: Callable[..., Entrant],
    caplog: pytest.LogCaptureFixture,
) -> None:
    entrant_factory(conference, name="Secret Person", email="s@example.com")
    with caplog.at_level(logging.INFO, logger=wrapup.__name__):
        wrap_up(conference, now=NOW)
    assert "Secret" not in caplog.text
    assert "s@example.com" not in caplog.text
    assert "entrants=1" in caplog.text


def test_start_refuses_after_wrapup(
    conference: Conference,
    drawing_factory: Callable[..., Drawing],
    prize_line_factory: Callable[..., PrizeLine],
) -> None:
    drawing = drawing_factory(conference)
    prize_line_factory(drawing)
    wrap_up(conference, now=NOW)
    with pytest.raises(DrawingError, match="wrapped up"):
        start_drawing(drawing, now=NOW)
    drawing.refresh_from_db()
    assert drawing.status == Drawing.Status.SCHEDULED


@pytest.fixture
def owner(client: Client, db: None) -> Client:
    user = User.objects.create_user("owner", password="pw", is_superuser=True)
    client.force_login(user)
    return client


def test_view_requires_login(client: Client, conference: Conference) -> None:
    url = reverse("booth-wrapup", args=["pycon"])
    assert client.get(url).status_code == 302
    assert client.post(url).status_code == 302
    conference.refresh_from_db()
    assert conference.wrapped_up_at is None


def test_confirmation_page_shows_counts_without_enqueueing(
    owner: Client,
    conference: Conference,
    entrant_factory: Callable[..., Entrant],
    give_pick: Callable[[Conference, Entrant, Pick.Status], None],
) -> None:
    winner = entrant_factory(conference)
    entrant_factory(
        conference, newsletter_optin_at=OPTIN, newsletter_optin_text=CONSENT
    )
    entrant_factory(conference)
    give_pick(conference, winner, Pick.Status.CLAIMED)
    page = owner.get(reverse("booth-wrapup", args=["pycon"]))
    body = page.content.decode()
    assert page.status_code == 200
    assert "<dt>Entrants</dt><dd>3</dd>" in body
    assert "<dt>Winners</dt><dd>1</dd>" in body
    assert "<dt>Nonwinners</dt><dd>2</dd>" in body
    assert "<dt>Newsletter opt-ins</dt><dd>1</dd>" in body
    assert "handed to Drip" in body
    assert not OutboxJob.objects.exists()
    conference.refresh_from_db()
    assert conference.wrapped_up_at is None


def test_confirmation_page_says_nothing_sent_without_drip(
    owner: Client, conference: Conference, settings: Any
) -> None:
    settings.DRIP_ACCOUNT_ID = ""
    settings.DRIP_API_KEY = ""
    body = owner.get(reverse("booth-wrapup", args=["pycon"])).content.decode()
    assert "Nothing will be sent to Drip" in body


def test_post_wraps_up_and_redirects(
    owner: Client, conference: Conference, entrant_factory: Callable[..., Entrant]
) -> None:
    entrant_factory(conference)
    response = owner.post(reverse("booth-wrapup", args=["pycon"]), follow=True)
    conference.refresh_from_db()
    assert conference.wrapped_up_at is not None
    assert OutboxJob.objects.count() == 2
    assert "Wrapped up: 1 entrant, 0 winners, 0 excluded" in response.content.decode()


def test_success_message_pluralizes(
    owner: Client, conference: Conference, entrant_factory: Callable[..., Entrant]
) -> None:
    entrant_factory(conference)
    entrant_factory(conference)
    response = owner.post(reverse("booth-wrapup", args=["pycon"]), follow=True)
    assert "Wrapped up: 2 entrants, 0 winners" in response.content.decode()


def test_double_post_shows_already_wrapped_up(
    owner: Client, conference: Conference, entrant_factory: Callable[..., Entrant]
) -> None:
    entrant_factory(conference)
    url = reverse("booth-wrapup", args=["pycon"])
    owner.post(url)
    jobs = OutboxJob.objects.count()
    response = owner.post(url, follow=True)
    assert response.status_code == 200
    assert "This conference is already wrapped up." in response.content.decode()
    assert OutboxJob.objects.count() == jobs


def test_excluded_entrants_are_not_sent_to_drip(
    conference: Conference, entrant_factory: Callable[..., Entrant]
) -> None:
    conference.exclusion_emails = "Owner@Example.com, staff@example.com"
    conference.save()
    entrant_factory(conference, email="owner@example.com")
    entrant_factory(conference, email="Staff@example.com")
    entrant_factory(conference, email="guest@example.com")
    result = wrap_up(conference, now=NOW)
    assert list(_subscribers()) == ["guest@example.com"]
    events = OutboxJob.objects.filter(kind=OutboxJob.Kind.EVENT)
    assert [e.payload["email"] for e in events] == ["guest@example.com"]
    assert (result.entrants, result.excluded, result.jobs) == (3, 2, 2)


def test_excluded_count_shown_on_confirmation_and_success(
    owner: Client, conference: Conference, entrant_factory: Callable[..., Entrant]
) -> None:
    conference.exclusion_emails = "owner@example.com"
    conference.save()
    entrant_factory(conference, email="owner@example.com")
    entrant_factory(conference, email="guest@example.com")
    url = reverse("booth-wrapup", args=["pycon"])
    assert "<dt>Excluded (not sent)</dt><dd>1</dd>" in owner.get(url).content.decode()
    response = owner.post(url, follow=True)
    assert "1 excluded" in response.content.decode()


def test_caller_object_not_stamped_when_transaction_rolls_back(
    conference: Conference, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError

    monkeypatch.setattr(wrapup, "enqueue", boom)
    conference.entrant_set.create(name="A", email="a@example.com")
    with pytest.raises(RuntimeError):
        wrap_up(conference, now=NOW)
    assert conference.wrapped_up_at is None


def test_post_without_drip_says_nothing_sent(
    owner: Client,
    conference: Conference,
    entrant_factory: Callable[..., Entrant],
    settings: Any,
) -> None:
    settings.DRIP_ACCOUNT_ID = ""
    settings.DRIP_API_KEY = ""
    entrant_factory(conference)
    response = owner.post(reverse("booth-wrapup", args=["pycon"]), follow=True)
    assert "Nothing was sent to Drip" in response.content.decode()
    assert not OutboxJob.objects.exists()


def test_post_refusal_shows_message_not_500(
    owner: Client, conference: Conference, drawing_factory: Callable[..., Drawing]
) -> None:
    drawing_factory(conference, status=Drawing.Status.OPEN)
    response = owner.post(reverse("booth-wrapup", args=["pycon"]), follow=True)
    assert response.status_code == 200
    assert "still open" in response.content.decode()
    conference.refresh_from_db()
    assert conference.wrapped_up_at is None


def test_wrapped_conference_page_replaces_the_button(
    owner: Client, conference: Conference
) -> None:
    url = reverse("booth-conference", args=["pycon"])
    link = reverse("booth-wrapup", args=["pycon"])
    assert link in owner.get(url).content.decode()
    wrap_up(conference, now=NOW)
    body = owner.get(url).content.decode()
    assert link not in body
    assert "Wrapped up" in body
    page = owner.get(link).content.decode()
    assert "already wrapped up" in page
    assert f'action="{link}"' not in page


def test_booth_start_after_wrapup_shows_message(
    owner: Client,
    conference: Conference,
    drawing_factory: Callable[..., Drawing],
    prize_line_factory: Callable[..., PrizeLine],
) -> None:
    drawing = drawing_factory(conference)
    prize_line_factory(drawing)
    wrap_up(conference, now=NOW)
    response = owner.post(reverse("booth-start", args=[drawing.pk]), follow=True)
    assert "wrapped up" in response.content.decode()
