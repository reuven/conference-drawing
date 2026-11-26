import datetime
import io
from collections.abc import Callable
from typing import Any

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import ProtectedError
from hypothesis import given
from hypothesis import strategies as st

from conference_drawing.raffle.csv_export import write_csv
from conference_drawing.raffle.models import (
    RESERVED_QUESTION_KEYS,
    AuditEvent,
    Book,
    Conference,
    CouponCode,
    Drawing,
    InstallSettings,
    OutboxJob,
    Pick,
    PrizeLine,
    Question,
    create_default_questions,
    validate_time_zone,
)
from tests.factories import make_questions

Factory = Callable[..., Any]


# --- InstallSettings -------------------------------------------------------


def test_install_settings_is_a_singleton_with_default_copy(db: None) -> None:
    first = InstallSettings.get()
    second = InstallSettings.get()
    assert first.pk == second.pk == 1
    assert InstallSettings.objects.count() == 1
    assert (
        first.followup_ack_text
        == "Entering means one follow-up email from me after the conference."
    )
    assert first.newsletter_checkbox_text == "Also subscribe me to Better Developers"


def test_install_settings_save_always_uses_pk_one(db: None) -> None:
    InstallSettings(owner_name="A").save()
    InstallSettings(owner_name="B").save()
    assert InstallSettings.objects.count() == 1
    assert InstallSettings.get().owner_name == "B"


def test_install_settings_str(db: None) -> None:
    assert str(InstallSettings.get()) == "Install settings"


# --- Book ------------------------------------------------------------------


def test_book_str_and_blank_optional_fields(book_factory: Factory) -> None:
    book = book_factory(title="Python Morsels")
    book.full_clean()
    assert str(book) == "Python Morsels"
    assert book.link == ""


@pytest.mark.parametrize("bad", ["a\nb", "x\n", "\nx", " x", "x "])
def test_book_title_rejects_newlines_and_unstripped_values(bad: str) -> None:
    with pytest.raises(ValidationError):
        Book(title=bad).full_clean()


# --- Conference ------------------------------------------------------------


def test_conference_tags_follow_scheme(conference_factory: Factory) -> None:
    c = conference_factory(slug="pycon-us-2027")
    assert c.tags() == (
        "pycon-us-2027-raffle",
        "pycon-us-2027-raffle-winner",
        "pycon-us-2027-raffle-nonwinner",
        "pycon-us-2027-newsletter-optin",
    )
    assert c.tags().newsletter_optin == "pycon-us-2027-newsletter-optin"


def test_unknown_time_zone_rejected(conference_factory: Factory) -> None:
    c = conference_factory()
    c.time_zone = "Mars/Olympus"
    with pytest.raises(ValidationError) as exc:
        c.full_clean()
    assert "time_zone" in exc.value.message_dict


def test_known_time_zone_accepted(conference_factory: Factory) -> None:
    conference_factory().full_clean()


def test_ends_before_starts_rejected(conference_factory: Factory) -> None:
    c = conference_factory()
    c.ends_on = c.starts_on - datetime.timedelta(days=1)
    with pytest.raises(ValidationError) as exc:
        c.full_clean()
    assert "ends_on" in exc.value.message_dict


def test_single_day_conference_is_valid(conference_factory: Factory) -> None:
    c = conference_factory()
    c.ends_on = c.starts_on
    c.full_clean()


@pytest.mark.parametrize(
    "bad", ["Has Caps", "-lead", "trail-", "double--dash", "a_b", "", "pycon\n"]
)
def test_bad_slug_rejected(conference_factory: Factory, bad: str) -> None:
    c = conference_factory()
    c.slug = bad
    with pytest.raises(ValidationError) as exc:
        c.full_clean()
    assert "slug" in exc.value.message_dict


@pytest.mark.parametrize("good", ["a", "pycon-us-2027", "x1-y2"])
def test_good_slug_accepted(conference_factory: Factory, good: str) -> None:
    c = conference_factory()
    c.slug = good
    c.full_clean()


def test_slug_unique(conference_factory: Factory) -> None:
    conference_factory(slug="dup")
    with pytest.raises(IntegrityError), transaction.atomic():
        conference_factory(slug="dup")


def test_display_token_is_random_and_long(conference_factory: Factory) -> None:
    a, b = conference_factory(), conference_factory()
    assert a.display_token != b.display_token
    assert len(a.display_token) >= 40
    assert a.wrapped_up_at is None
    assert a.deleted_entrant_count is None


def test_excluded_emails_are_normalized(conference_factory: Factory) -> None:
    c = conference_factory(
        exclusion_emails=" Boss@Example.COM \n\nfriend@x.org, other@x.org;third@x.org\n"
    )
    assert c.excluded_emails == frozenset(
        {"boss@example.com", "friend@x.org", "other@x.org", "third@x.org"}
    )
    assert conference_factory().excluded_emails == frozenset()


@given(st.text())
def test_excluded_emails_never_contain_blank_or_unnormalized(raw: str) -> None:
    emails = Conference(exclusion_emails=raw).excluded_emails
    assert all(e and e == e.strip() and e == e.lower() for e in emails)


def test_conference_str(conference_factory: Factory) -> None:
    assert str(conference_factory(name="PyCon")) == "PyCon"


# --- Question --------------------------------------------------------------


def test_default_questions_exact(conference_factory: Factory) -> None:
    c = conference_factory()
    create_default_questions(c)
    assert [q.key for q in c.question_set.order_by("position")] == [
        "want_to_learn",
        "coding_style",
        "anything_else",
    ]
    style = c.question_set.get(key="coding_style")
    assert style.kind == "choice"
    assert style.prompt == "How do you code these days?"
    assert style.choices == [
        "I'm just starting to code",
        "I write my own code, and use AI a little or not at all",
        "I work alongside AI: chat, autocomplete, asking it to explain",
        "AI agents write most of my code; I direct and review",
    ]
    assert style.choices[3] == "AI agents write most of my code; I direct and review"
    learn = c.question_set.get(key="want_to_learn")
    assert (learn.prompt, learn.kind, learn.choices) == (
        "What do you most want to learn?",
        "text",
        [],
    )
    assert c.question_set.get(key="anything_else").prompt == "Anything else to tell me?"
    assert str(learn) == "want_to_learn"


def test_question_key_unique_per_conference(conference_factory: Factory) -> None:
    a, b = conference_factory(), conference_factory()
    Question.objects.create(conference=a, key="k", prompt="p")
    Question.objects.create(conference=b, key="k", prompt="p")
    with pytest.raises(IntegrityError), transaction.atomic():
        Question.objects.create(conference=a, key="k", prompt="p2")


@pytest.mark.parametrize(
    "bad", ["Bad Key", "a__b", "_lead", "trail_", "k\n", "a-_b", ""]
)
def test_question_key_must_be_slug(conference_factory: Factory, bad: str) -> None:
    q = Question(conference=conference_factory(), key=bad, prompt="p")
    with pytest.raises(ValidationError) as exc:
        q.full_clean()
    assert "key" in exc.value.message_dict


@pytest.mark.parametrize("good", ["want_to_learn", "coding_style", "k", "a-b", "x1"])
def test_question_key_accepts_the_default_style(
    conference_factory: Factory, good: str
) -> None:
    Question(conference=conference_factory(), key=good, prompt="p").full_clean()


def test_default_questions_pass_their_own_validation(
    conference_factory: Factory,
) -> None:
    c = conference_factory()
    make_questions(c)
    for q in c.question_set.all():
        q.full_clean()


@pytest.mark.parametrize("key", sorted(RESERVED_QUESTION_KEYS))
def test_question_key_cannot_shadow_a_csv_column_or_conference(
    conference_factory: Factory, key: str
) -> None:
    """Its CSV column (or Drip event property) would collide with a fixed one."""
    q = Question(conference=conference_factory(), key=key, prompt="p")
    with pytest.raises(ValidationError) as exc:
        q.full_clean()
    assert "reserved" in exc.value.message_dict["key"][0]


def test_reserved_keys_are_the_csv_columns_and_conference() -> None:
    out = io.StringIO()
    write_csv([], [], out)
    header = out.getvalue().strip().split(",")
    assert RESERVED_QUESTION_KEYS == {*header, "conference"}


def test_choice_question_needs_string_choices(conference_factory: Factory) -> None:
    c = conference_factory()

    def question(choices: Any) -> Question:
        return Question(
            conference=c, key="k", prompt="p", kind="choice", choices=choices
        )

    with pytest.raises(ValidationError) as exc:
        question([]).full_clean()
    assert "choices" in exc.value.message_dict
    with pytest.raises(ValidationError):
        question([1]).full_clean()
    with pytest.raises(ValidationError):
        question("x").full_clean()
    question(["a"]).full_clean()


# --- Entrant ---------------------------------------------------------------


def test_entrant_email_normalized_on_save(
    conference_factory: Factory, entrant_factory: Factory
) -> None:
    e = entrant_factory(conference_factory(), email="  Alice@Example.COM ")
    e.refresh_from_db()
    assert e.email == "alice@example.com"
    assert e.newsletter_optin_at is None
    assert e.answers == {}
    assert str(e) == e.name


def test_duplicate_email_per_conference_rejected(
    conference_factory: Factory, entrant_factory: Factory
) -> None:
    c = conference_factory()
    entrant_factory(c, email="a@example.com")
    with pytest.raises(IntegrityError), transaction.atomic():
        entrant_factory(c, email="A@Example.com")


def test_same_email_in_two_conferences_allowed(
    conference_factory: Factory, entrant_factory: Factory
) -> None:
    entrant_factory(conference_factory(), email="a@example.com")
    entrant_factory(conference_factory(), email="a@example.com")


# --- Drawing ---------------------------------------------------------------


def test_one_open_drawing_per_conference(
    conference_factory: Factory, drawing_factory: Factory
) -> None:
    c = conference_factory()
    drawing_factory(c, status="open")
    drawing_factory(c, status="scheduled")
    drawing_factory(c, status="finished")
    drawing_factory(c, status="finished")
    with pytest.raises(IntegrityError), transaction.atomic():
        drawing_factory(c, status="open")


def test_open_drawings_in_different_conferences_allowed(
    conference_factory: Factory, drawing_factory: Factory
) -> None:
    drawing_factory(conference_factory(), status="open")
    drawing_factory(conference_factory(), status="open")


def test_drawing_defaults(
    conference_factory: Factory, drawing_factory: Factory
) -> None:
    d = drawing_factory(conference_factory())
    assert d.status == "scheduled"
    assert d.awarded_count == 0
    assert d.started_at is None
    assert d.cutoff_at is None
    assert d.closed_at is None
    assert str(d).startswith("Drawing ")


# --- PrizeLine -------------------------------------------------------------


@pytest.fixture
def drawing(conference_factory: Factory, drawing_factory: Factory) -> Drawing:
    d: Drawing = drawing_factory(conference_factory())
    return d


def _line(drawing: Drawing, book_factory: Factory, **kw: Any) -> PrizeLine:
    return PrizeLine(drawing=drawing, book=book_factory(), **kw)


def test_physical_line_valid(drawing: Drawing, book_factory: Factory) -> None:
    _line(drawing, book_factory, kind="physical", quantity=2).full_clean()


def test_physical_line_rejects_code_fields(
    drawing: Drawing, book_factory: Factory
) -> None:
    for extra in (
        {"code_mode": "pool"},
        {"code_mode": "shared"},
        {"shared_code": "X"},
        {"url_template": "https://x.example/{code}"},
    ):
        with pytest.raises(ValidationError):
            _line(drawing, book_factory, kind="physical", **extra).full_clean()


def test_digital_line_requires_url_and_code_source(
    drawing: Drawing, book_factory: Factory
) -> None:
    with pytest.raises(ValidationError):
        _line(drawing, book_factory, kind="digital").full_clean()
    with pytest.raises(ValidationError):
        _line(
            drawing,
            book_factory,
            kind="digital",
            url_template="https://x.example/{code}",
        ).full_clean()
    with pytest.raises(ValidationError):
        _line(drawing, book_factory, kind="digital", code_mode="pool").full_clean()


def test_digital_shared_needs_shared_code(
    drawing: Drawing, book_factory: Factory
) -> None:
    with pytest.raises(ValidationError):
        _line(
            drawing,
            book_factory,
            kind="digital",
            url_template="https://x.example/{code}",
            code_mode="shared",
        ).full_clean()


def test_digital_lines_valid(drawing: Drawing, book_factory: Factory) -> None:
    _line(
        drawing,
        book_factory,
        kind="digital",
        url_template="https://x.example/{code}",
        code_mode="pool",
    ).full_clean()
    _line(
        drawing,
        book_factory,
        kind="digital",
        url_template="https://x.example/{code}",
        code_mode="shared",
        shared_code="SAVE",
    ).full_clean()


def test_digital_pool_rejects_shared_code(
    drawing: Drawing, book_factory: Factory
) -> None:
    with pytest.raises(ValidationError):
        _line(
            drawing,
            book_factory,
            kind="digital",
            url_template="https://x.example/{code}",
            code_mode="pool",
            shared_code="SAVE",
        ).full_clean()


def test_quantity_must_be_positive(drawing: Drawing, book_factory: Factory) -> None:
    with pytest.raises(ValidationError) as exc:
        _line(drawing, book_factory, kind="physical", quantity=0).full_clean()
    assert "quantity" in exc.value.message_dict


def test_book_is_protected_from_deletion(
    drawing: Drawing, prize_line_factory: Factory
) -> None:
    line = prize_line_factory(drawing)
    with pytest.raises(ProtectedError):
        line.book.delete()


def test_prize_line_str(drawing: Drawing, prize_line_factory: Factory) -> None:
    line = prize_line_factory(drawing, quantity=3)
    assert str(line) == f"3 x {line.book.title}"


# --- CouponCode / Pick / AuditEvent / OutboxJob ------------------------------


def test_coupon_code_unique_per_line(
    drawing: Drawing, prize_line_factory: Factory
) -> None:
    line = prize_line_factory(drawing)
    other = prize_line_factory(drawing)
    CouponCode.objects.create(prize_line=line, code="A")
    CouponCode.objects.create(prize_line=other, code="A")
    with pytest.raises(IntegrityError), transaction.atomic():
        CouponCode.objects.create(prize_line=line, code="A")


def test_deleting_a_pick_unassigns_its_code(
    drawing: Drawing,
    prize_line_factory: Factory,
    entrant_factory: Factory,
    pick_factory: Factory,
) -> None:
    line = prize_line_factory(drawing)
    pick = pick_factory(line, entrant_factory(drawing.conference))
    code = CouponCode.objects.create(prize_line=line, code="A", pick=pick)
    pick.delete()
    code.refresh_from_db()
    assert code.pick is None
    assert str(code) == "A"


def test_pick_defaults_and_slot_bounds(
    drawing: Drawing,
    prize_line_factory: Factory,
    entrant_factory: Factory,
    pick_factory: Factory,
) -> None:
    line = prize_line_factory(drawing, quantity=2)
    e = entrant_factory(drawing.conference)
    pick = pick_factory(line, e, slot=2)
    pick.full_clean()
    assert pick.status == "pending"
    assert pick.replaces is None
    assert pick.marked_at is None
    assert str(pick) == f"Pick {pick.pk} (slot 2)"
    for bad in (0, 3):
        with pytest.raises(ValidationError) as exc:
            Pick(prize_line=line, entrant=e, slot=bad, pool_size=1).full_clean()
        assert "slot" in exc.value.message_dict


def test_deleting_an_entrant_deletes_their_picks(
    drawing: Drawing,
    prize_line_factory: Factory,
    entrant_factory: Factory,
    pick_factory: Factory,
) -> None:
    e = entrant_factory(drawing.conference)
    pick_factory(prize_line_factory(drawing), e)
    e.delete()
    assert Pick.objects.count() == 0


def test_audit_event_keeps_history_when_pick_is_deleted(
    drawing: Drawing,
    prize_line_factory: Factory,
    entrant_factory: Factory,
    pick_factory: Factory,
) -> None:
    pick = pick_factory(
        prize_line_factory(drawing), entrant_factory(drawing.conference)
    )
    pick_id = pick.pk
    ev = AuditEvent.objects.create(
        drawing=drawing, kind="picked", pick=pick, detail={"pick_id": pick_id}
    )
    pick.delete()
    ev.refresh_from_db()
    assert ev.pick is None
    assert ev.detail == {"pick_id": pick_id}
    assert str(ev).startswith("picked")


def test_outbox_job_defaults(conference_factory: Factory) -> None:
    job = OutboxJob.objects.create(
        conference=conference_factory(), kind="event", payload={"a": 1}
    )
    assert job.status == "pending"
    assert job.attempts == 0
    assert job.sent_at is None
    assert job.last_error_class == ""
    assert job.next_attempt_at is not None
    assert str(job) == f"event job {job.pk} (pending)"


def test_validate_time_zone_refuses_a_bare_city_name() -> None:
    # The dropdown's choices now refuse bad values first, but migrations
    # reference this validator, and it still guards the field on its own.
    with pytest.raises(ValidationError, match="Unknown time zone: 'Jerusalem'"):
        validate_time_zone("Jerusalem")
    validate_time_zone("Asia/Jerusalem")


@pytest.mark.parametrize("word", ["login", "logout", "drawing", "pick"])
def test_reserved_slug_is_refused_by_name(
    word: str, conference_factory: Factory
) -> None:
    """These words are booth URL segments, so a conference can't take them."""
    conf = conference_factory(slug="fine-slug")
    conf.slug = word
    with pytest.raises(ValidationError, match=f"\u201c{word}\u201d is reserved"):
        conf.full_clean()


@pytest.mark.parametrize("slug", ["pick-up-2027", "login-day"])
def test_slug_containing_a_reserved_word_is_fine(
    slug: str, conference_factory: Factory
) -> None:
    conf = conference_factory(slug="fine-slug")
    conf.slug = slug
    conf.full_clean()
