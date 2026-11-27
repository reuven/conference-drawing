"""Admin setup screens: driven through the test client as a superuser."""

import datetime
import io
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from conference_drawing.raffle.admin import (
    AuditEventAdmin,
    ConferenceAdmin,
    DrawingAdminForm,
    InstallSettingsAdmin,
    PrizeLineForm,
)
from conference_drawing.raffle.models import (
    AuditEvent,
    Book,
    Conference,
    CouponCode,
    Drawing,
    InstallSettings,
    OutboxJob,
    PrizeLine,
    Question,
)
from tests.factories import make_questions

Factory = Callable[..., Any]


@pytest.fixture
def admin_client(db: None) -> Client:
    user = get_user_model().objects.create_superuser("boss", "boss@example.com", "pw")
    client = Client()
    client.force_login(user)
    return client


def _formset(
    prefix: str, rows: list[dict[str, Any]], initial: int = 0
) -> dict[str, Any]:
    data: dict[str, Any] = {
        f"{prefix}-TOTAL_FORMS": len(rows),
        f"{prefix}-INITIAL_FORMS": initial,
        f"{prefix}-MIN_NUM_FORMS": 0,
        f"{prefix}-MAX_NUM_FORMS": 1000,
    }
    for i, row in enumerate(rows):
        data.update({f"{prefix}-{i}-{k}": v for k, v in row.items()})
    return data


def _conference_post(**over: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "name": "PyCon",
        "slug": "pycon",
        "time_zone": "America/New_York",
        "booth_location": "",
        "starts_on": "2027-05-10",
        "ends_on": "2027-05-12",
        "exclusion_emails": "",
        "entries_per_network_per_hour": "60",
        "after_mode": "inherit",
        "after_markdown": "",
        "after_redirect_url": "",
        **_formset("question_set", []),
    }
    return {**data, **over}


def test_new_conference_gets_no_questions(admin_client: Client) -> None:
    resp = admin_client.post(reverse("admin:raffle_conference_add"), _conference_post())
    assert resp.status_code == 302
    conf = Conference.objects.get(slug="pycon")
    assert Question.objects.filter(conference=conf).count() == 0


def test_editing_conference_does_not_add_questions_again(
    admin_client: Client, conference_factory: Factory
) -> None:
    conf = conference_factory()
    data = _conference_post(slug=conf.slug)
    resp = admin_client.post(
        reverse("admin:raffle_conference_change", args=[conf.pk]), data
    )
    assert resp.status_code == 302
    assert Question.objects.filter(conference=conf).count() == 0


def test_editing_conference_with_its_default_questions_saves(
    admin_client: Client, conference_factory: Factory
) -> None:
    """The browser posts the default question rows back; they must validate."""
    conf = conference_factory()
    make_questions(conf)
    rows = [
        {
            "id": q.pk,
            "conference": conf.pk,
            "key": q.key,
            "prompt": q.prompt,
            "kind": q.kind,
            "choices": json.dumps(q.choices),
            "position": q.position,
        }
        for q in conf.question_set.all()
    ]
    data = _conference_post(slug=conf.slug, **_formset("question_set", rows, len(rows)))
    resp = admin_client.post(
        reverse("admin:raffle_conference_change", args=[conf.pk]), data
    )
    errors = re.findall(r"errorlist.{0,300}", resp.content.decode(), re.S)
    assert resp.status_code == 302, errors


def test_display_token_and_wrapped_up_are_read_only(
    admin_client: Client, conference_factory: Factory
) -> None:
    conf = conference_factory()
    old = conf.display_token
    data = _conference_post(
        slug=conf.slug, **{"display_token": "x", "wrapped_up_at": "2027-01-01 00:00:00"}
    )
    admin_client.post(reverse("admin:raffle_conference_change", args=[conf.pk]), data)
    conf.refresh_from_db()
    assert conf.display_token == old
    assert conf.wrapped_up_at is None


def test_display_token_not_in_conference_list(
    admin_client: Client, conference_factory: Factory
) -> None:
    conf = conference_factory()
    resp = admin_client.get(reverse("admin:raffle_conference_changelist"))
    assert resp.status_code == 200
    assert conf.display_token not in resp.content.decode()


def _line_row(**over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "kind": "digital",
        "quantity": 2,
        "position": 0,
        "url_template": "https://example.com/redeem?c={code}",
        "code_mode": "pool",
        "shared_code": "",
        "codes_text": "",
    }
    return {**row, **over}


def _drawing_post(
    conf: Conference, book_id: int, rows: list[dict[str, Any]], **over: Any
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "conference": conf.pk,
        "status": "scheduled",
        "scheduled_at_0": "2027-05-11",
        "scheduled_at_1": "14:00:00",
        **_formset("prizeline_set", [{"book": book_id, **r} for r in rows]),
    }
    return {**data, **over}


def test_drawing_local_time_saved_as_utc(
    admin_client: Client, conference_factory: Factory, book_factory: Factory
) -> None:
    conf = conference_factory(time_zone="America/New_York")
    book = book_factory()
    physical = _line_row(kind="physical", url_template="", code_mode="")
    resp = admin_client.post(
        reverse("admin:raffle_drawing_add"), _drawing_post(conf, book.pk, [physical])
    )
    assert resp.status_code == 302, resp.content.decode()[:3000]
    drawing = Drawing.objects.get()
    # 14:00 EDT == 18:00 UTC
    assert drawing.scheduled_at == datetime.datetime(
        2027, 5, 11, 18, 0, tzinfo=datetime.UTC
    )


def test_drawing_form_shows_local_time_back(
    admin_client: Client, conference_factory: Factory, drawing_factory: Factory
) -> None:
    conf = conference_factory(time_zone="America/New_York")
    drawing = drawing_factory(
        conf, scheduled_at=datetime.datetime(2027, 5, 11, 18, 0, tzinfo=datetime.UTC)
    )
    resp = admin_client.get(reverse("admin:raffle_drawing_change", args=[drawing.pk]))
    html = resp.content.decode()
    assert 'name="scheduled_at_0"' in html
    assert 'value="2027-05-11"' in html
    assert 'value="14:00:00"' in html


def test_drawing_spring_forward_gap_is_a_form_error(
    conference_factory: Factory,
) -> None:
    conf = conference_factory(time_zone="America/New_York")
    form = DrawingAdminForm(
        data={
            "conference": conf.pk,
            "status": "scheduled",
            "scheduled_at_0": "2027-03-14",
            "scheduled_at_1": "02:30:00",
        }
    )
    assert not form.is_valid()
    assert "scheduled_at" in form.errors


def test_drawing_fall_back_ambiguous_time_is_accepted(
    conference_factory: Factory,
) -> None:
    conf = conference_factory(time_zone="America/New_York")
    form = DrawingAdminForm(
        data={
            "conference": conf.pk,
            "status": "scheduled",
            "scheduled_at_0": "2027-11-07",
            "scheduled_at_1": "01:30:00",
        }
    )
    assert form.is_valid(), form.errors


def test_pasted_codes_create_coupon_rows_without_duplicates(
    admin_client: Client,
    conference_factory: Factory,
    drawing_factory: Factory,
    prize_line_factory: Factory,
) -> None:
    conf = conference_factory()
    drawing = drawing_factory(conf)
    line = prize_line_factory(
        drawing,
        kind=PrizeLine.Kind.DIGITAL,
        url_template="https://example.com/r?c={code}",
        code_mode=PrizeLine.CodeMode.POOL,
        quantity=3,
    )
    CouponCode.objects.create(prize_line=line, code="AAA")
    url = reverse("admin:raffle_drawing_change", args=[drawing.pk])
    row = _line_row(
        id=line.pk,
        drawing=drawing.pk,
        book=line.book_id,
        quantity=3,
        codes_text="AAA\nBBB\n\nBBB\nCCC\n",
    )
    data = _drawing_post(
        conf,
        line.book_id,
        [],
        scheduled_at_0="2027-05-11",
        scheduled_at_1="14:00:00",
        **_formset("prizeline_set", [row], 1),
    )
    resp = admin_client.post(url, data)
    assert resp.status_code == 302, resp.content.decode()[:3000]
    assert sorted(CouponCode.objects.values_list("code", flat=True)) == [
        "AAA",
        "BBB",
        "CCC",
    ]
    html = admin_client.get(url).content.decode()
    assert "3 unused / 3 total" in html


def test_assigned_codes_are_untouched_and_counted_used(
    admin_client: Client,
    conference_factory: Factory,
    drawing_factory: Factory,
    prize_line_factory: Factory,
    entrant_factory: Factory,
    pick_factory: Factory,
) -> None:
    conf = conference_factory()
    drawing = drawing_factory(conf)
    line = prize_line_factory(
        drawing,
        kind=PrizeLine.Kind.DIGITAL,
        url_template="https://example.com/r?c={code}",
        code_mode=PrizeLine.CodeMode.POOL,
    )
    pick = pick_factory(line, entrant_factory(conf))
    CouponCode.objects.create(prize_line=line, code="AAA", pick=pick)
    CouponCode.objects.create(prize_line=line, code="BBB")
    html = admin_client.get(
        reverse("admin:raffle_drawing_change", args=[drawing.pk])
    ).content.decode()
    assert "1 unused / 2 total" in html
    # Prize lines with picks cannot be deleted, so assigned codes survive.
    assert 'name="prizeline_set-0-DELETE"' not in html
    resp = admin_client.post(
        reverse("admin:raffle_drawing_delete", args=[drawing.pk]), {"post": "yes"}
    )
    assert resp.status_code == 403
    assert Drawing.objects.filter(pk=drawing.pk).exists()
    assert CouponCode.objects.filter(pick=pick).exists()


def test_codes_text_rejected_for_non_pool_line(
    admin_client: Client, conference_factory: Factory, book_factory: Factory
) -> None:
    conf = conference_factory()
    book = book_factory()
    row = _line_row(code_mode="shared", shared_code="X", codes_text="AAA")
    resp = admin_client.post(
        reverse("admin:raffle_drawing_add"), _drawing_post(conf, book.pk, [row])
    )
    assert resp.status_code == 200
    assert not CouponCode.objects.exists()


def test_bad_code_paste_names_the_line(
    admin_client: Client, conference_factory: Factory, book_factory: Factory
) -> None:
    conf = conference_factory()
    book = book_factory()
    row = _line_row(codes_text="AAA\nbad code")
    resp = admin_client.post(
        reverse("admin:raffle_drawing_add"), _drawing_post(conf, book.pk, [row])
    )
    assert resp.status_code == 200
    assert "line 2" in resp.content.decode()
    assert not Drawing.objects.exists()


def test_new_line_with_pasted_codes_creates_them(
    admin_client: Client, conference_factory: Factory, book_factory: Factory
) -> None:
    conf = conference_factory()
    book = book_factory()
    row = _line_row(codes_text="AAA\nBBB")
    resp = admin_client.post(
        reverse("admin:raffle_drawing_add"), _drawing_post(conf, book.pk, [row])
    )
    assert resp.status_code == 302
    assert CouponCode.objects.count() == 2


@pytest.mark.parametrize(
    "bad", ["javascript:alert(1)", "ftp://x/{code}", "//x/{code}", "https:///{code}"]
)
def test_prize_line_url_must_be_http_or_https(
    bad: str,
    drawing_factory: Factory,
    conference_factory: Factory,
    book_factory: Factory,
) -> None:
    drawing = drawing_factory(conference_factory())
    line = PrizeLine(
        drawing=drawing,
        book=book_factory(),
        kind=PrizeLine.Kind.DIGITAL,
        url_template=bad,
        code_mode=PrizeLine.CodeMode.SHARED,
        shared_code="X",
    )
    with pytest.raises(ValidationError) as exc:
        line.clean()
    assert "url_template" in exc.value.message_dict


def test_prize_line_http_urls_are_accepted(
    drawing_factory: Factory, conference_factory: Factory, book_factory: Factory
) -> None:
    drawing = drawing_factory(conference_factory())
    for url in ("http://x.example/{code}", "HTTPS://x.example/r?c={code}"):
        PrizeLine(
            drawing=drawing,
            book=book_factory(),
            kind=PrizeLine.Kind.DIGITAL,
            url_template=url,
            code_mode=PrizeLine.CodeMode.SHARED,
            shared_code="X",
        ).clean()


def test_admin_rejects_javascript_url(
    admin_client: Client, conference_factory: Factory, book_factory: Factory
) -> None:
    conf = conference_factory()
    book = book_factory()
    row = _line_row(url_template="javascript:alert(1)")
    resp = admin_client.post(
        reverse("admin:raffle_drawing_add"), _drawing_post(conf, book.pk, [row])
    )
    assert resp.status_code == 200
    assert not PrizeLine.objects.exists()


def test_install_settings_plural_label_is_not_settingss() -> None:
    assert InstallSettings._meta.verbose_name_plural == "install settings"


def test_install_settings_singleton(admin_client: Client) -> None:
    add = reverse("admin:raffle_installsettings_add")
    assert admin_client.get(add).status_code == 200
    settings_row = InstallSettings.get()
    assert admin_client.get(add).status_code == 403
    delete = reverse("admin:raffle_installsettings_delete", args=[settings_row.pk])
    assert admin_client.post(delete, {"post": "yes"}).status_code == 403
    assert InstallSettings.objects.count() == 1


def test_book_admin_creates_a_book(admin_client: Client) -> None:
    resp = admin_client.post(
        reverse("admin:raffle_book_add"),
        {"title": "Python Workout", "link": "https://example.com/b"},
    )
    assert resp.status_code == 302
    assert Book.objects.get().title == "Python Workout"


def test_entrant_admin_is_read_only(
    admin_client: Client, conference_factory: Factory, entrant_factory: Factory
) -> None:
    conf = conference_factory()
    entrant = entrant_factory(conf, name="Ada Lovelace", email="ada@example.com")
    entrant_factory(conf, name="Bob", email="bob@example.com")
    listing = reverse("admin:raffle_entrant_changelist")
    html = admin_client.get(listing, {"q": "ada@example"}).content.decode()
    assert "Ada Lovelace" in html
    assert "bob@example.com" not in html
    html = admin_client.get(listing, {"q": "Bob"}).content.decode()
    assert "Ada Lovelace" not in html
    assert admin_client.get(reverse("admin:raffle_entrant_add")).status_code == 403
    change = reverse("admin:raffle_entrant_change", args=[entrant.pk])
    assert admin_client.post(change, {"name": "Changed"}).status_code == 403
    delete = reverse("admin:raffle_entrant_delete", args=[entrant.pk])
    assert admin_client.post(delete, {"post": "yes"}).status_code == 403
    entrant.refresh_from_db()
    assert entrant.name == "Ada Lovelace"


def test_outbox_admin_is_read_only_and_hides_payload(
    admin_client: Client, conference_factory: Factory
) -> None:
    conf = conference_factory()
    job = OutboxJob.objects.create(
        conference=conf,
        kind=OutboxJob.Kind.EVENT,
        payload={"email": "secret@example.com"},
        status=OutboxJob.Status.FAILED,
        last_error_class="DripError",
    )
    listing = admin_client.get(reverse("admin:raffle_outboxjob_changelist"))
    html = listing.content.decode()
    assert "DripError" in html
    assert "secret@example.com" not in html
    assert admin_client.get(reverse("admin:raffle_outboxjob_add")).status_code == 403
    detail = admin_client.get(reverse("admin:raffle_outboxjob_change", args=[job.pk]))
    assert "secret@example.com" not in detail.content.decode()
    delete = reverse("admin:raffle_outboxjob_delete", args=[job.pk])
    assert admin_client.post(delete, {"post": "yes"}).status_code == 403


def test_retry_failed_action_requeues(
    admin_client: Client, conference_factory: Factory
) -> None:
    conf = conference_factory()
    future = timezone.now() + datetime.timedelta(days=1)
    failed = OutboxJob.objects.create(
        conference=conf,
        kind=OutboxJob.Kind.EVENT,
        status=OutboxJob.Status.FAILED,
        attempts=5,
        next_attempt_at=future,
    )
    sent = OutboxJob.objects.create(
        conference=conf,
        kind=OutboxJob.Kind.EVENT,
        status=OutboxJob.Status.SENT,
        attempts=1,
        next_attempt_at=future,
    )
    before = timezone.now()
    resp = admin_client.post(
        reverse("admin:raffle_outboxjob_changelist"),
        {"action": "retry_failed", "_selected_action": [failed.pk, sent.pk]},
    )
    assert resp.status_code == 302
    failed.refresh_from_db()
    sent.refresh_from_db()
    assert failed.status == OutboxJob.Status.PENDING
    assert failed.attempts == 0
    assert before <= failed.next_attempt_at <= timezone.now()
    assert sent.status == OutboxJob.Status.SENT
    assert sent.attempts == 1
    assert sent.next_attempt_at == future


def test_drawing_list_shows_local_time(
    admin_client: Client, conference_factory: Factory, drawing_factory: Factory
) -> None:
    conf = conference_factory(time_zone="America/New_York")
    drawing_factory(
        conf, scheduled_at=datetime.datetime(2027, 5, 11, 18, 0, tzinfo=datetime.UTC)
    )
    html = admin_client.get(reverse("admin:raffle_drawing_changelist")).content
    assert "2027-05-11 14:00" in html.decode()


def test_drawing_form_requires_conference_and_time(db: None) -> None:
    form = DrawingAdminForm(data={"status": "scheduled"})
    assert not form.is_valid()
    assert {"conference", "scheduled_at"} <= set(form.errors)


def test_prize_line_form_uncommitted_save_still_adds_codes(
    drawing_factory: Factory, conference_factory: Factory, book_factory: Factory
) -> None:
    drawing = drawing_factory(conference_factory())
    form = PrizeLineForm(
        data={
            "book": book_factory().pk,
            "kind": "digital",
            "quantity": 1,
            "position": 0,
            "url_template": "https://example.com/{code}",
            "code_mode": "pool",
            "shared_code": "",
            "codes_text": "AAA\nBBB",
        }
    )
    form.instance.drawing = drawing
    assert form.is_valid(), form.errors
    line = form.save(commit=False)
    assert not CouponCode.objects.exists()
    line.save()
    form.save_m2m()
    assert CouponCode.objects.filter(prize_line=line).count() == 2


def test_resaving_unchanged_ambiguous_time_keeps_the_same_instant(
    admin_client: Client, conference_factory: Factory, drawing_factory: Factory
) -> None:
    conf = conference_factory(time_zone="America/New_York")
    # 06:30 UTC on 2027-11-07 is the SECOND 01:30 (EST, fold=1).
    second = datetime.datetime(2027, 11, 7, 6, 30, tzinfo=datetime.UTC)
    drawing = drawing_factory(conf, scheduled_at=second)
    data = {
        "status": "scheduled",
        "scheduled_at_0": "2027-11-07",
        "scheduled_at_1": "01:30:00",
        **_formset("prizeline_set", []),
    }
    resp = admin_client.post(
        reverse("admin:raffle_drawing_change", args=[drawing.pk]), data
    )
    assert resp.status_code == 302, resp.content.decode()[:2000]
    drawing.refresh_from_db()
    assert drawing.scheduled_at == second


def test_changing_time_on_existing_drawing_converts_again(
    admin_client: Client, conference_factory: Factory, drawing_factory: Factory
) -> None:
    conf = conference_factory(time_zone="America/New_York")
    drawing = drawing_factory(conf)
    data = {
        "status": "scheduled",
        "scheduled_at_0": "2027-05-11",
        "scheduled_at_1": "15:00:00",
        **_formset("prizeline_set", []),
    }
    admin_client.post(reverse("admin:raffle_drawing_change", args=[drawing.pk]), data)
    drawing.refresh_from_db()
    assert drawing.scheduled_at == datetime.datetime(
        2027, 5, 11, 19, 0, tzinfo=datetime.UTC
    )


def test_conference_is_read_only_on_existing_drawing(
    admin_client: Client, conference_factory: Factory, drawing_factory: Factory
) -> None:
    conf = conference_factory()
    other = conference_factory()
    drawing = drawing_factory(conf)
    url = reverse("admin:raffle_drawing_change", args=[drawing.pk])
    assert 'name="conference"' not in admin_client.get(url).content.decode()
    data = {
        "conference": other.pk,
        "status": "scheduled",
        "scheduled_at_0": "2027-05-11",
        "scheduled_at_1": "14:00:00",
        **_formset("prizeline_set", []),
    }
    admin_client.post(url, data)
    drawing.refresh_from_db()
    assert drawing.conference_id == conf.pk


def test_book_in_use_delete_page_is_blocked_not_an_error(
    admin_client: Client,
    conference_factory: Factory,
    drawing_factory: Factory,
    prize_line_factory: Factory,
) -> None:
    line = prize_line_factory(drawing_factory(conference_factory()))
    resp = admin_client.get(reverse("admin:raffle_book_delete", args=[line.book_id]))
    assert resp.status_code == 200
    assert "protected" in resp.content.decode().lower()
    assert Book.objects.filter(pk=line.book_id).exists()


def test_codes_text_rejected_for_physical_line(
    admin_client: Client, conference_factory: Factory, book_factory: Factory
) -> None:
    conf = conference_factory()
    book = book_factory()
    row = _line_row(kind="physical", url_template="", code_mode="", codes_text="AAA")
    resp = admin_client.post(
        reverse("admin:raffle_drawing_add"), _drawing_post(conf, book.pk, [row])
    )
    assert resp.status_code == 200
    assert not Drawing.objects.exists()
    assert not CouponCode.objects.exists()


def test_rotate_display_token_action_breaks_the_old_url(
    admin_client: Client, conference_factory: Factory
) -> None:
    """For a display link that leaked: the old URL must stop working at once."""
    conf, other = conference_factory(), conference_factory()
    old, untouched = conf.display_token, other.display_token
    assert admin_client.get(f"/display/{old}/").status_code == 200
    resp = admin_client.post(
        reverse("admin:raffle_conference_changelist"),
        {"action": "rotate_display_token", "_selected_action": [conf.pk]},
        follow=True,
    )
    assert "Rotated the display token of 1 conference(s)." in resp.content.decode()
    conf.refresh_from_db()
    other.refresh_from_db()
    assert conf.display_token != old and len(conf.display_token) >= 40
    assert other.display_token == untouched
    assert admin_client.get(f"/display/{old}/").status_code == 404
    assert admin_client.get(f"/display/{conf.display_token}/").status_code == 200
    assert old not in resp.content.decode()


# --- login rate limit ----------------------------------------------------------


@pytest.mark.usefixtures("frozen_ratelimit_clock")
def test_admin_login_posts_are_rate_limited_per_ip(db: None) -> None:
    """Password guessing is capped, so it can't freeze the booth's workers."""
    client = Client()
    url = reverse("admin:login")
    assert url == "/admin/login/"
    guess = {"username": "boss", "password": "wrong"}
    who: dict[str, Any] = {"REMOTE_ADDR": "203.0.113.9"}
    # A different account each time, so only the per-IP limit can answer 429.
    statuses = [
        client.post(url, {**guess, "username": f"u{i}"}, **who).status_code
        for i in range(10)
    ]
    assert set(statuses) == {200}  # the form again, with an error
    blocked = client.post(url, guess, **who)
    assert blocked.status_code == 429
    assert blocked["Cache-Control"] == "private, no-store"
    # GET is never limited, and another client has its own count.
    assert client.get(url, **who).status_code == 200
    assert client.post(url, guess, REMOTE_ADDR="203.0.113.10").status_code == 200


@pytest.mark.usefixtures("frozen_ratelimit_clock")
def test_admin_login_still_logs_in(db: None) -> None:
    get_user_model().objects.create_superuser("boss", "boss@example.com", "pw")
    client = Client()
    resp = client.post(
        reverse("admin:login"),
        {"username": "boss", "password": "pw", "next": "/admin/"},
    )
    assert (resp.status_code, resp["Location"]) == (302, "/admin/")


# --- the drawing state machine is the booth's, never the admin's ---------------


@pytest.mark.parametrize(
    ("status", "posted"),
    [
        (Drawing.Status.FINISHED, "scheduled"),  # restart: would award twice
        (Drawing.Status.SCHEDULED, "open"),  # open with no cutoff: 500 on Not here
    ],
)
def test_drawing_status_is_read_only(
    admin_client: Client,
    conference_factory: Factory,
    drawing_factory: Factory,
    status: Drawing.Status,
    posted: str,
) -> None:
    drawing = drawing_factory(conference_factory(), status=status)
    url = reverse("admin:raffle_drawing_change", args=[drawing.pk])
    assert 'name="status"' not in admin_client.get(url).content.decode()
    data = {
        "status": posted,
        "scheduled_at_0": "2027-05-11",
        "scheduled_at_1": "14:00:00",
        **_formset("prizeline_set", []),
    }
    assert admin_client.post(url, data).status_code == 302
    drawing.refresh_from_db()
    assert drawing.status == status


def test_new_drawing_ignores_posted_status(
    admin_client: Client, conference_factory: Factory, book_factory: Factory
) -> None:
    conf = conference_factory()
    physical = _line_row(kind="physical", url_template="", code_mode="")
    data = _drawing_post(conf, book_factory().pk, [physical], status="open")
    resp = admin_client.post(reverse("admin:raffle_drawing_add"), data)
    assert resp.status_code == 302
    assert Drawing.objects.get().status == Drawing.Status.SCHEDULED


@pytest.mark.parametrize("status", [Drawing.Status.OPEN, Drawing.Status.FINISHED])
def test_prize_lines_are_frozen_once_the_drawing_starts(
    admin_client: Client,
    conference_factory: Factory,
    drawing_factory: Factory,
    prize_line_factory: Factory,
    book_factory: Factory,
    status: Drawing.Status,
) -> None:
    """No edits, adds or deletes: the picks, emails and CSV must keep agreeing."""
    conf = conference_factory()
    drawing = drawing_factory(conf, status=status)
    line = prize_line_factory(
        drawing,
        kind=PrizeLine.Kind.DIGITAL,
        url_template="https://example.com/r?c={code}",
        code_mode=PrizeLine.CodeMode.SHARED,
        shared_code="SHARED1",
        quantity=2,
    )
    url = reverse("admin:raffle_drawing_change", args=[drawing.pk])
    html = admin_client.get(url).content.decode()
    for name in ("kind", "quantity", "shared_code", "url_template", "DELETE"):
        assert f'name="prizeline_set-0-{name}"' not in html
    changed = _line_row(
        id=line.pk,
        drawing=drawing.pk,
        book=line.book_id,
        kind="physical",
        quantity=5,
        url_template="https://evil.example/{code}",
        code_mode="shared",
        shared_code="CHANGED",
        DELETE="on",
    )
    added = _line_row(book=book_factory().pk, kind="physical", code_mode="")
    data = {
        "scheduled_at_0": "2027-05-11",
        "scheduled_at_1": "14:00:00",
        **_formset("prizeline_set", [changed, added], 1),
    }
    admin_client.post(url, data)
    line.refresh_from_db()
    assert (line.kind, line.quantity, line.shared_code, line.url_template) == (
        PrizeLine.Kind.DIGITAL,
        2,
        "SHARED1",
        "https://example.com/r?c={code}",
    )
    assert list(PrizeLine.objects.filter(drawing=drawing)) == [line]


def test_prize_lines_stay_editable_while_scheduled(
    admin_client: Client,
    conference_factory: Factory,
    drawing_factory: Factory,
    prize_line_factory: Factory,
) -> None:
    drawing = drawing_factory(conference_factory())
    prize_line_factory(drawing)
    html = admin_client.get(
        reverse("admin:raffle_drawing_change", args=[drawing.pk])
    ).content.decode()
    for name in ("kind", "quantity", "DELETE"):
        assert f'name="prizeline_set-0-{name}"' in html


@pytest.mark.parametrize("status", [Drawing.Status.OPEN, Drawing.Status.FINISHED])
def test_started_drawing_cannot_be_deleted_even_without_picks(
    admin_client: Client,
    conference_factory: Factory,
    drawing_factory: Factory,
    prize_line_factory: Factory,
    status: Drawing.Status,
) -> None:
    drawing = drawing_factory(conference_factory(), status=status)
    prize_line_factory(drawing)  # an empty pool: started, but nobody picked
    delete = reverse("admin:raffle_drawing_delete", args=[drawing.pk])
    assert admin_client.post(delete, {"post": "yes"}).status_code == 403
    bulk = {
        "action": "delete_selected",
        "_selected_action": [drawing.pk],
        "post": "yes",
    }
    admin_client.post(reverse("admin:raffle_drawing_changelist"), bulk)
    assert Drawing.objects.filter(pk=drawing.pk).exists()
    assert PrizeLine.objects.filter(drawing=drawing).exists()


def test_scheduled_drawing_can_still_be_deleted(
    admin_client: Client, conference_factory: Factory, drawing_factory: Factory
) -> None:
    drawing = drawing_factory(conference_factory())
    delete = reverse("admin:raffle_drawing_delete", args=[drawing.pk])
    assert admin_client.post(delete, {"post": "yes"}).status_code == 302
    assert not Drawing.objects.exists()


# --- After-conference content and logo file lifecycle ---------------------


def _png(name: str = "logo.png") -> SimpleUploadedFile:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), "red").save(buf, format="PNG")
    return SimpleUploadedFile(name, buf.getvalue(), content_type="image/png")


def _site_post(**over: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "owner_name": "",
        "privacy_note": "",
        "followup_ack_text": "ack",
        "newsletter_checkbox_text": "box",
        "after_mode": "page",
        "after_markdown": "",
        "after_redirect_url": "",
    }
    return {**data, **over}


def _site_url() -> str:
    InstallSettings.get()
    return reverse("admin:raffle_installsettings_change", args=[1])


def _logo_files(root: Path) -> list[str]:
    return sorted(p.name for p in (root / "logos").glob("*")) if root.exists() else []


@pytest.fixture
def media(settings: Any, tmp_path: Path) -> Path:
    settings.MEDIA_ROOT = tmp_path
    return tmp_path


def test_both_admins_have_after_a_conference_fieldset(
    admin_client: Client, conference_factory: Factory
) -> None:
    conf = conference_factory()
    fields = {"after_mode", "after_logo", "after_markdown", "after_redirect_url"}
    for admin_obj, obj in (
        (admin.site._registry[InstallSettings], InstallSettings.get()),
        (admin.site._registry[Conference], conf),
    ):
        fieldsets = dict((t, o) for t, o in admin_obj.get_fieldsets(None, obj))  # type: ignore[arg-type]
        assert fields <= set(fieldsets["After a conference"]["fields"])


def test_conference_fieldset_is_collapsed_with_help(
    admin_client: Client, conference_factory: Factory
) -> None:
    conf = conference_factory()
    resp = admin_client.get(reverse("admin:raffle_conference_change", args=[conf.pk]))
    html = resp.content.decode()
    assert "After a conference" in html
    assert "collapse" in html
    assert "Leave blank to use the site-wide settings" in html


def test_site_fieldset_is_not_collapsed(admin_client: Client) -> None:
    html = admin_client.get(_site_url()).content.decode()
    assert "After a conference" in html
    assert "Leave blank to use the site-wide settings" not in html


def test_logo_thumbnail_shows_once_a_logo_exists(
    admin_client: Client, media: Path
) -> None:
    html = admin_client.get(_site_url()).content.decode()
    assert "after-logo-thumb" not in html
    admin_client.post(_site_url(), _site_post(after_logo=_png()))
    html = admin_client.get(_site_url()).content.decode()
    assert re.search(r'<img [^>]*class="after-logo-thumb"', html)
    assert "style=" not in re.search(r"<img [^>]*after-logo-thumb[^>]*>", html)[0]  # type: ignore[index]


def test_admin_loads_the_thumbnail_css(admin_client: Client) -> None:
    html = admin_client.get(_site_url()).content.decode()
    assert "raffle/admin.css" in html


def test_site_redirect_without_url_is_a_form_error(admin_client: Client) -> None:
    resp = admin_client.post(_site_url(), _site_post(after_mode="redirect"))
    assert resp.status_code == 200
    assert "Redirect mode needs a URL" in resp.content.decode()
    assert InstallSettings.get().after_mode == "page"


def test_conference_redirect_without_any_url_is_a_form_error(
    admin_client: Client, conference_factory: Factory
) -> None:
    conf = conference_factory()
    data = _conference_post(slug=conf.slug, after_mode="redirect")
    resp = admin_client.post(
        reverse("admin:raffle_conference_change", args=[conf.pk]), data
    )
    assert resp.status_code == 200
    assert "Redirect mode needs a URL" in resp.content.decode()


def test_conference_redirect_may_use_the_sites_url(
    admin_client: Client, conference_factory: Factory
) -> None:
    site = InstallSettings.get()
    site.after_redirect_url = "https://example.com/next"
    site.save()
    conf = conference_factory()
    data = _conference_post(slug=conf.slug, after_mode="redirect")
    resp = admin_client.post(
        reverse("admin:raffle_conference_change", args=[conf.pk]), data
    )
    assert resp.status_code == 302


def test_replacing_a_logo_deletes_the_old_file(
    admin_client: Client,
    media: Path,
    django_capture_on_commit_callbacks: Any,
) -> None:
    with django_capture_on_commit_callbacks(execute=True):
        admin_client.post(_site_url(), _site_post(after_logo=_png()))
    (old,) = _logo_files(media)
    with django_capture_on_commit_callbacks(execute=True):
        resp = admin_client.post(_site_url(), _site_post(after_logo=_png()))
    assert resp.status_code == 302
    (new,) = _logo_files(media)
    assert new != old


def test_clearing_a_logo_deletes_the_file(
    admin_client: Client,
    media: Path,
    django_capture_on_commit_callbacks: Any,
) -> None:
    with django_capture_on_commit_callbacks(execute=True):
        admin_client.post(_site_url(), _site_post(after_logo=_png()))
    with django_capture_on_commit_callbacks(execute=True):
        admin_client.post(_site_url(), _site_post(**{"after_logo-clear": "on"}))
    assert _logo_files(media) == []
    assert InstallSettings.get().after_logo.name == ""


def test_saving_without_a_new_logo_keeps_the_file(
    admin_client: Client,
    media: Path,
    django_capture_on_commit_callbacks: Any,
) -> None:
    with django_capture_on_commit_callbacks(execute=True):
        admin_client.post(_site_url(), _site_post(after_logo=_png()))
    with django_capture_on_commit_callbacks(execute=True):
        admin_client.post(_site_url(), _site_post(after_markdown="hello"))
    assert len(_logo_files(media)) == 1


def test_old_file_survives_until_commit(
    admin_client: Client,
    media: Path,
    django_capture_on_commit_callbacks: Any,
) -> None:
    with django_capture_on_commit_callbacks(execute=True):
        admin_client.post(_site_url(), _site_post(after_logo=_png()))
    with django_capture_on_commit_callbacks(execute=False):
        admin_client.post(_site_url(), _site_post(**{"after_logo-clear": "on"}))
    assert len(_logo_files(media)) == 1


def test_replacing_a_logo_whose_file_is_missing_saves(
    admin_client: Client,
    media: Path,
    django_capture_on_commit_callbacks: Any,
) -> None:
    with django_capture_on_commit_callbacks(execute=True):
        admin_client.post(_site_url(), _site_post(after_logo=_png()))
    for path in (media / "logos").glob("*"):
        path.unlink()
    with django_capture_on_commit_callbacks(execute=True):
        resp = admin_client.post(_site_url(), _site_post(after_logo=_png()))
    assert resp.status_code == 302
    assert len(_logo_files(media)) == 1


def test_conference_logo_change_leaves_the_sites_logo(
    admin_client: Client,
    media: Path,
    conference_factory: Factory,
    django_capture_on_commit_callbacks: Any,
) -> None:
    with django_capture_on_commit_callbacks(execute=True):
        admin_client.post(_site_url(), _site_post(after_logo=_png()))
    site_logo = str(InstallSettings.get().after_logo.name)
    conf = conference_factory()
    url = reverse("admin:raffle_conference_change", args=[conf.pk])
    with django_capture_on_commit_callbacks(execute=True):
        admin_client.post(url, _conference_post(slug=conf.slug, after_logo=_png()))
    with django_capture_on_commit_callbacks(execute=True):
        admin_client.post(url, _conference_post(slug=conf.slug, after_logo=_png()))
    assert len(_logo_files(media)) == 2
    assert InstallSettings.get().after_logo.name == site_logo
    assert (media / site_logo).exists()


def test_deleting_a_conference_deletes_its_logo(
    admin_client: Client,
    media: Path,
    conference_factory: Factory,
    django_capture_on_commit_callbacks: Any,
) -> None:
    conf = conference_factory()
    url = reverse("admin:raffle_conference_change", args=[conf.pk])
    with django_capture_on_commit_callbacks(execute=True):
        admin_client.post(url, _conference_post(slug=conf.slug, after_logo=_png()))
    assert len(_logo_files(media)) == 1
    with django_capture_on_commit_callbacks(execute=True):
        Conference.objects.get(pk=conf.pk).delete()
    assert _logo_files(media) == []


def test_deleting_a_conference_without_a_logo_is_fine(
    conference_factory: Factory,
    django_capture_on_commit_callbacks: Any,
) -> None:
    conf = conference_factory()
    with django_capture_on_commit_callbacks(execute=True):
        conf.delete()
    assert not Conference.objects.filter(pk=conf.pk).exists()


def test_save_with_update_fields_elsewhere_keeps_the_logo(
    admin_client: Client,
    media: Path,
    conference_factory: Factory,
    django_capture_on_commit_callbacks: Any,
) -> None:
    conf = conference_factory()
    url = reverse("admin:raffle_conference_change", args=[conf.pk])
    with django_capture_on_commit_callbacks(execute=True):
        admin_client.post(url, _conference_post(slug=conf.slug, after_logo=_png()))
    stale = Conference.objects.get(pk=conf.pk)
    stale.after_logo = ""
    with django_capture_on_commit_callbacks(execute=True):
        stale.save(update_fields=["wrapped_up_at"])
    assert len(_logo_files(media)) == 1


# --- Review follow-ups ------------------------------------------------------


def _unlink_logos(root: Path) -> None:
    for path in (root / "logos").glob("*"):
        path.unlink()


def test_resaving_with_a_missing_logo_file_saves(
    admin_client: Client,
    media: Path,
    conference_factory: Factory,
    django_capture_on_commit_callbacks: Any,
) -> None:
    conf = conference_factory()
    url = reverse("admin:raffle_conference_change", args=[conf.pk])
    with django_capture_on_commit_callbacks(execute=True):
        admin_client.post(_site_url(), _site_post(after_logo=_png()))
        admin_client.post(url, _conference_post(slug=conf.slug, after_logo=_png()))
    _unlink_logos(media)
    site = admin_client.post(_site_url(), _site_post(after_markdown="hi"))
    assert site.status_code == 302
    resp = admin_client.post(url, _conference_post(slug=conf.slug, after_markdown="x"))
    assert resp.status_code == 302


def test_a_stored_logo_is_not_decoded_again_on_save(
    admin_client: Client,
    media: Path,
    monkeypatch: pytest.MonkeyPatch,
    django_capture_on_commit_callbacks: Any,
) -> None:
    with django_capture_on_commit_callbacks(execute=True):
        admin_client.post(_site_url(), _site_post(after_logo=_png()))

    def boom(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("decoded again")

    monkeypatch.setattr(Image, "open", boom)
    resp = admin_client.post(_site_url(), _site_post(after_markdown="hi"))
    assert resp.status_code == 302


def test_callback_keeps_a_file_another_row_still_uses(
    admin_client: Client,
    media: Path,
    conference_factory: Factory,
    django_capture_on_commit_callbacks: Any,
) -> None:
    conf = conference_factory()
    url = reverse("admin:raffle_conference_change", args=[conf.pk])
    with django_capture_on_commit_callbacks(execute=True):
        admin_client.post(url, _conference_post(slug=conf.slug, after_logo=_png()))
    held = str(Conference.objects.get(pk=conf.pk).after_logo.name)
    site = InstallSettings.get()
    site.after_logo.name = held  # the site row also points at the file
    site.save()
    with django_capture_on_commit_callbacks(execute=True):
        admin_client.post(url, _conference_post(slug=conf.slug, after_logo=_png()))
    assert (media / held).exists()


def test_storage_error_on_delete_does_not_break_the_save(
    admin_client: Client,
    media: Path,
    monkeypatch: pytest.MonkeyPatch,
    django_capture_on_commit_callbacks: Any,
) -> None:
    with django_capture_on_commit_callbacks(execute=True):
        admin_client.post(_site_url(), _site_post(after_logo=_png()))

    def fail(self: Any, name: str) -> None:
        raise OSError("disk")

    monkeypatch.setattr("django.core.files.storage.FileSystemStorage.delete", fail)
    with django_capture_on_commit_callbacks(execute=True):
        resp = admin_client.post(_site_url(), _site_post(**{"after_logo-clear": "on"}))
    assert resp.status_code == 302
    assert InstallSettings.get().after_logo.name == ""


@pytest.mark.parametrize(
    "payload",
    [
        b'<svg xmlns="http://www.w3.org/2000/svg"><script>1</script></svg>',
        b"not an image at all",
    ],
)
def test_bad_logo_upload_is_a_form_error_and_leaves_no_file(
    admin_client: Client, media: Path, payload: bytes
) -> None:
    bad = SimpleUploadedFile("logo.png", payload, content_type="image/png")
    resp = admin_client.post(_site_url(), _site_post(after_logo=bad))
    assert resp.status_code == 200
    assert "errorlist" in resp.content.decode()
    assert _logo_files(media) == []


def test_conference_after_fieldset_classes(rf: Any) -> None:
    request = rf.get("/")
    fieldsets = dict(ConferenceAdmin(Conference, admin.site).get_fieldsets(request))
    assert fieldsets["After a conference"]["classes"] == ["collapse"]
    assert (
        "classes"
        not in dict(
            InstallSettingsAdmin(InstallSettings, admin.site).get_fieldsets(request)
        )["After a conference"]
    )


def test_deleting_a_conference_whose_logo_file_is_missing_succeeds(
    admin_client: Client,
    media: Path,
    conference_factory: Factory,
    django_capture_on_commit_callbacks: Any,
) -> None:
    conf = conference_factory()
    url = reverse("admin:raffle_conference_change", args=[conf.pk])
    with django_capture_on_commit_callbacks(execute=True):
        admin_client.post(url, _conference_post(slug=conf.slug, after_logo=_png()))
    _unlink_logos(media)
    with django_capture_on_commit_callbacks(execute=True):
        Conference.objects.get(pk=conf.pk).delete()
    assert not Conference.objects.filter(pk=conf.pk).exists()


def test_site_settings_page_explains_the_fallback_and_field_formats(
    admin_client: Client,
) -> None:
    html = admin_client.get(_site_url()).content.decode()
    assert "Conferences use these unless they set their own, field by field." in html
    assert "Links (https, http, mailto) work" in html
    assert "https only." in html
    assert "PNG, JPEG, WebP or GIF, up to 2 MB." in html


def test_conference_time_zone_is_a_dropdown_by_region_and_city(
    admin_client: Client,
) -> None:
    html = admin_client.get(reverse("admin:raffle_conference_add")).content.decode()
    assert '<select name="time_zone"' in html
    assert '<optgroup label="Asia">' in html
    assert '<option value="Asia/Jerusalem">Jerusalem</option>' in html
    assert 'value="US/Eastern"' not in html


def test_a_bad_time_zone_is_refused_and_the_rest_of_the_form_kept(
    admin_client: Client,
) -> None:
    data = _conference_post(time_zone="Jerusalem", name="Kept Conf")
    resp = admin_client.post(reverse("admin:raffle_conference_add"), data)
    assert resp.status_code == 200
    html = resp.content.decode()
    assert "Select a valid choice" in html
    assert 'value="Kept Conf"' in html
    assert not Conference.objects.filter(name="Kept Conf").exists()


def test_admin_pages_load_the_form_helper_and_keep_their_header(
    admin_client: Client,
) -> None:
    html = admin_client.get(reverse("admin:raffle_conference_add")).content.decode()
    assert 'src="/static/raffle/admin-forms.js" defer' in html
    assert 'id="site-name"' in html  # Django's own base_site is still extended


FILE_AGAIN = "If a save fails, choose the file again."


@pytest.mark.parametrize(
    "url_name", ["admin:raffle_conference_add", "admin:raffle_book_add"]
)
def test_file_fields_say_to_choose_the_file_again_after_a_failed_save(
    admin_client: Client, url_name: str
) -> None:
    # Browsers always clear a file picker when a form comes back with errors.
    assert FILE_AGAIN in admin_client.get(reverse(url_name)).content.decode()


def test_install_settings_logo_says_to_choose_the_file_again(
    admin_client: Client,
) -> None:
    install = InstallSettings.get()
    url = reverse("admin:raffle_installsettings_change", args=[install.pk])
    assert FILE_AGAIN in admin_client.get(url).content.decode()


def test_conference_admin_links_to_its_booth_flyer_and_entry_pages(
    admin_client: Client, conference_factory: Factory
) -> None:
    conf = conference_factory()
    url = reverse("admin:raffle_conference_change", args=[conf.pk])
    html = admin_client.get(url).content.decode()
    for name, target in (
        ("Booth page", reverse("booth-conference", args=[conf.slug])),
        ("Printable flyer (QR code)", reverse("booth-flyer", args=[conf.slug])),
        ("Entry page", reverse("entry", args=[conf.slug])),
    ):
        assert f'<a href="{target}">{name}</a>' in html


def test_new_conference_says_links_come_after_saving(admin_client: Client) -> None:
    html = admin_client.get(reverse("admin:raffle_conference_add")).content.decode()
    assert "Save the conference to get its booth, flyer and entry links." in html


def test_admin_header_links_to_the_booth(admin_client: Client) -> None:
    html = admin_client.get(reverse("admin:index")).content.decode()
    assert f'<a href="{reverse("booth-home")}">Booth</a>' in html


def test_conference_admin_offers_only_active_non_superusers_as_helpers(
    admin_client: Client,
) -> None:
    user_model = get_user_model()
    user_model.objects.create_user("plain", "plain@example.com", "pw")
    user_model.objects.create_user(
        "staffer", "staffer@example.com", "pw", is_staff=True
    )
    user_model.objects.create_user("gone", "gone@example.com", "pw", is_active=False)
    resp = admin_client.get(reverse("admin:raffle_conference_add"))
    offered = [
        u.username for u in resp.context["adminform"].form.fields["helpers"].queryset
    ]
    assert offered == ["plain", "staffer"]


def test_saving_helpers_on_a_conference(admin_client: Client) -> None:
    helper = get_user_model().objects.create_user("plain", "plain@example.com", "pw")
    resp = admin_client.post(
        reverse("admin:raffle_conference_add"), _conference_post(helpers=[helper.pk])
    )
    assert resp.status_code == 302
    assert list(Conference.objects.get(slug="pycon").helpers.all()) == [helper]


def test_other_many_to_many_fields_keep_their_full_choice_list(
    rf: Any, admin_user: Any
) -> None:
    """Only `helpers` is narrowed; any other many-to-many field is left alone."""
    request = rf.get("/")
    request.user = admin_user
    model_admin = ConferenceAdmin(Conference, admin.site)
    groups = get_user_model()._meta.get_field("groups")
    field = model_admin.formfield_for_manytomany(groups, request)
    assert field.queryset.model is groups.related_model


def test_audit_events_admin_is_read_only(
    admin_client: Client, drawing_factory: Factory, conference_factory: Factory
) -> None:
    drawing = drawing_factory(conference_factory())
    event = AuditEvent.objects.create(drawing=drawing, kind=AuditEvent.Kind.STARTED)
    changelist = reverse("admin:raffle_auditevent_changelist")
    assert admin_client.get(changelist).status_code == 200
    assert admin_client.get(reverse("admin:raffle_auditevent_add")).status_code == 403
    change_url = reverse("admin:raffle_auditevent_change", args=[event.pk])
    page = admin_client.get(change_url)
    assert page.status_code == 200
    assert 'name="_save"' not in page.content.decode()
    assert admin_client.post(change_url, {"kind": "void"}).status_code == 403
    delete_url = reverse("admin:raffle_auditevent_delete", args=[event.pk])
    assert admin_client.post(delete_url, {"post": "yes"}).status_code == 403
    admin_client.post(
        changelist, {"action": "delete_selected", "_selected_action": [event.pk]}
    )
    assert AuditEvent.objects.filter(pk=event.pk).exists()


def test_audit_events_admin_lists_ids_only_newest_first() -> None:
    model_admin = AuditEventAdmin(AuditEvent, admin.site)
    assert model_admin.ordering == ("-at", "-id")
    assert model_admin.list_display == ("at", "drawing", "kind", "pick_id")
    assert model_admin.list_filter == ("drawing", "kind")
