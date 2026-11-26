import datetime
import threading
from collections.abc import Callable
from typing import Any

import pytest
from django.db import IntegrityError, connection, transaction

from conference_drawing.raffle.forms import EntriesClosed, EntryForm
from conference_drawing.raffle.models import (
    Conference,
    Entrant,
    InstallSettings,
    Question,
)
from tests.factories import make_questions

NOW = datetime.datetime(2027, 5, 11, 18, 0, tzinfo=datetime.UTC)

pytestmark = pytest.mark.django_db


def _data(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "name": "Ada Lovelace",
        "email": "ada@example.com",
        "followup_ack": "on",
    }
    return {**base, **overrides}


def _enter(conference: Conference, **overrides: Any) -> Entrant | None:
    form = EntryForm(conference, _data(**overrides))
    assert form.is_valid(), form.errors
    return form.save(NOW)


@pytest.fixture
def conference(conference_factory: Callable[..., Conference]) -> Conference:
    conf = conference_factory()
    make_questions(conf)
    return conf


def test_form_requires_name_email_and_ack(conference: Conference) -> None:
    form = EntryForm(conference, {})
    assert not form.is_valid()
    assert set(form.errors) == {"name", "email", "followup_ack"}


@pytest.mark.parametrize(
    "email", ["a@localhost", "a@[127.0.0.1]", "a@foo.test", "a@x.local", "a@x.invalid"]
)
def test_form_rejects_addresses_drip_would_refuse(
    conference: Conference, email: str
) -> None:
    form = EntryForm(conference, _data(email=email))
    assert not form.is_valid()
    assert form.errors["email"] == ["Please enter a valid email address."]


def test_newsletter_optin_unchecked_by_default(conference: Conference) -> None:
    html = str(EntryForm(conference)["newsletter_optin"])
    assert "checkbox" in html
    assert "checked" not in html


def test_choice_question_rejects_unknown_value(conference: Conference) -> None:
    form = EntryForm(conference, _data(q_coding_style="Something else"))
    assert not form.is_valid()
    assert "q_coding_style" in form.errors


def test_answers_saved_by_question_key(conference: Conference) -> None:
    choice = Question.objects.get(conference=conference, key="coding_style").choices[0]
    entrant = _enter(
        conference, q_want_to_learn="  Async\r\nPython ", q_coding_style=choice
    )
    assert entrant is not None
    assert entrant.answers == {
        "want_to_learn": "Async\nPython",
        "coding_style": choice,
    }


def test_blank_optional_answers_are_not_stored(conference: Conference) -> None:
    entrant = _enter(conference, q_want_to_learn="   ", q_coding_style="")
    assert entrant is not None
    assert entrant.answers == {}


@pytest.mark.parametrize(
    "value",
    ["a\x00b", "a‮b", "a\tb", "x" * 1001],
    ids=["nul", "bidi", "tab", "long"],
)
def test_free_text_rejects_hostile_input(conference: Conference, value: str) -> None:
    form = EntryForm(conference, _data(q_anything_else=value))
    assert not form.is_valid()
    assert "q_anything_else" in form.errors


def test_free_text_allows_exactly_1000_chars(conference: Conference) -> None:
    assert EntryForm(conference, _data(q_anything_else="x" * 1000)).is_valid()


@pytest.mark.parametrize("name", ["Ada\nLovelace", "Ada​L", "x" * 201])
def test_name_rejects_unsafe_values(conference: Conference, name: str) -> None:
    form = EntryForm(conference, _data(name=name))
    assert not form.is_valid()
    assert "name" in form.errors


def test_consent_texts_and_timestamps_recorded(conference: Conference) -> None:
    install = InstallSettings.get()
    install.followup_ack_text = "Custom ack."
    install.newsletter_checkbox_text = "Custom BD."
    install.save()

    e1 = _enter(conference)
    assert e1 is not None
    assert e1.followup_ack_text == "Custom ack."
    assert e1.followup_ack_at == NOW
    assert e1.created_at == NOW
    assert e1.newsletter_optin_at is None
    assert e1.newsletter_optin_text == ""

    e2 = _enter(conference, email="b@example.com", newsletter_optin="on")
    assert e2 is not None
    assert e2.newsletter_optin_at == NOW
    assert e2.newsletter_optin_text == "Custom BD."


def test_form_labels_use_install_texts(conference: Conference) -> None:
    install = InstallSettings.get()
    form = EntryForm(conference)
    assert form.fields["followup_ack"].label == install.followup_ack_text
    assert form.fields["newsletter_optin"].label == install.newsletter_checkbox_text


def test_repeat_entry_with_case_variant_does_not_duplicate(
    conference: Conference,
) -> None:
    assert _enter(conference, q_want_to_learn="original") is not None
    again = _enter(
        conference,
        name="Someone Else",
        email=" ADA@Example.COM ",
        q_want_to_learn="new",
    )
    assert again is None

    entrant = Entrant.objects.get()
    assert entrant.name == "Ada Lovelace"
    assert entrant.answers == {"want_to_learn": "original"}


def test_same_email_in_another_conference_is_not_a_repeat(
    conference: Conference, conference_factory: Callable[..., Conference]
) -> None:
    other = conference_factory()
    assert _enter(conference) is not None
    assert _enter(other) is not None
    assert Entrant.objects.count() == 2


def test_race_on_unique_constraint_is_treated_as_repeat(
    conference: Conference, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _enter(conference) is not None
    form = EntryForm(conference, _data())
    assert form.is_valid()

    # Simulate the other request committing between our check and our insert:
    # the first check says "not entered", every later one tells the truth.
    real = EntryForm._already_entered
    calls: list[str] = []

    def stale_then_real(self: EntryForm, email: str) -> bool:
        calls.append(email)
        return False if len(calls) == 1 else real(self, email)

    monkeypatch.setattr(EntryForm, "_already_entered", stale_then_real)
    assert form.save(NOW) is None
    assert Entrant.objects.count() == 1


def test_save_refuses_once_the_conference_is_wrapped_up(
    conference: Conference,
) -> None:
    """The view checked before wrap-up committed; save rechecks under a lock."""
    form = EntryForm(conference, _data())
    assert form.is_valid()
    Conference.objects.filter(pk=conference.pk).update(wrapped_up_at=NOW)
    with pytest.raises(EntriesClosed):
        form.save(NOW)
    assert not Entrant.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_save_waits_for_a_wrap_up_in_progress_and_then_refuses(
    conference: Conference,
) -> None:
    locked, release = threading.Event(), threading.Event()
    outcomes: list[object] = []

    def wrap_up_in_progress() -> None:
        try:
            with transaction.atomic():
                Conference.objects.select_for_update().get(pk=conference.pk)
                Conference.objects.filter(pk=conference.pk).update(wrapped_up_at=NOW)
                locked.set()
                release.wait(timeout=5)
        finally:
            connection.close()

    def enter() -> None:
        try:
            outcomes.append(_enter(conference))
        except Exception as exc:  # recorded for the assertion below
            outcomes.append(exc)
        finally:
            connection.close()

    holder = threading.Thread(target=wrap_up_in_progress)
    holder.start()
    worker = threading.Thread(target=enter)
    try:
        assert locked.wait(timeout=5)
        worker.start()
        worker.join(timeout=0.5)
        waited = worker.is_alive()
    finally:
        release.set()
        holder.join()
        worker.join()
    assert waited
    assert [type(o) for o in outcomes] == [EntriesClosed]
    assert not Entrant.objects.exists()


def test_unrelated_integrity_error_is_not_swallowed(
    conference: Conference, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(self: Entrant, *args: Any, **kwargs: Any) -> None:
        raise IntegrityError("other")

    monkeypatch.setattr(Entrant, "save", boom)
    form = EntryForm(conference, _data())
    assert form.is_valid()
    with pytest.raises(IntegrityError):
        form.save(NOW)


def test_honeypot_detection(conference: Conference) -> None:
    assert not EntryForm(conference, _data()).honeypot_filled()
    assert EntryForm(conference, _data(leave_blank="http://spam")).honeypot_filled()
    # Browser autofill puts a URL in a field named "website"; that is a person.
    assert not EntryForm(conference, _data(website="https://ada.dev")).honeypot_filled()


def test_honeypot_field_is_hidden_and_never_autofilled(conference: Conference) -> None:
    html = str(EntryForm(conference)["leave_blank"])
    assert 'name="leave_blank"' in html
    assert 'autocomplete="off"' in html
    assert 'tabindex="-1"' in html


def test_unknown_and_server_side_keys_cannot_be_mass_assigned(
    conference: Conference, conference_factory: Callable[..., Conference]
) -> None:
    other = conference_factory()
    posted = _data(
        q_not_a_question="smuggled",
        q_want_to_learn="Async",
        answers='{"evil": 1}',
        followup_ack_text="I agree to anything",
        newsletter_optin_text="forged consent",
        newsletter_optin_at="2000-01-01T00:00:00Z",
        conference=str(other.pk),
        created_at="2000-01-01T00:00:00Z",
    )
    form = EntryForm(conference, posted)
    assert form.is_valid(), form.errors
    entrant = form.save(NOW)
    assert entrant is not None
    entrant.refresh_from_db()
    install = InstallSettings.get()
    assert entrant.answers == {"want_to_learn": "Async"}
    assert entrant.conference_id == conference.pk
    assert entrant.followup_ack_text == install.followup_ack_text
    assert (entrant.newsletter_optin_text, entrant.newsletter_optin_at) == ("", None)
    assert entrant.created_at == NOW


def test_answer_and_name_accept_zwj_emoji(conference: Conference) -> None:
    entrant = _enter(
        conference,
        name="Dev 👩\u200d💻",
        q_want_to_learn="I want to learn 👩\u200d💻 agents",
    )
    assert entrant is not None
    assert entrant.answers == {"want_to_learn": "I want to learn 👩\u200d💻 agents"}
