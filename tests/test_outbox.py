"""The Drip outbox: enqueueing, the worker's claim/send/record cycle, and the booth."""

import datetime
import logging
import os
import random
import signal
import threading
import time
from collections.abc import Callable, Iterator
from io import StringIO
from typing import Any

import httpx
import pytest
import respx
from django.contrib.auth.models import User
from django.core.management import call_command
from django.db import connection, transaction
from django.test import Client
from django.urls import reverse
from pydantic import SecretStr

from conference_drawing.raffle import wrapup
from conference_drawing.raffle.drip import outbox
from conference_drawing.raffle.drip.client import (
    BATCH_LIMIT,
    DEADLINE_SECONDS,
    DripClient,
    build_http_client,
)
from conference_drawing.raffle.drip.config import DripSettings
from conference_drawing.raffle.drip.payloads import EventPayload, SubscriberPayload
from conference_drawing.raffle.management.commands import drip_worker
from conference_drawing.raffle.models import (
    Conference,
    CouponCode,
    Drawing,
    Entrant,
    OutboxJob,
    Pick,
    PrizeLine,
)
from conference_drawing.raffle.views import booth

NOW = datetime.datetime(2027, 5, 11, 21, 0, tzinfo=datetime.UTC)
EVENTS = "https://api.getdrip.com/v2/1234567/events"
BATCHES = "https://api.getdrip.com/v2/1234567/subscribers/batches"
J = OutboxJob


@pytest.fixture(autouse=True)
def _fake_drip(settings: Any) -> None:
    """Every test gets fake credentials; none can reach the real Drip API."""
    settings.DRIP_ACCOUNT_ID = "1234567"
    settings.DRIP_API_KEY = "fake-key"


@pytest.fixture
def http() -> Iterator[httpx.Client]:
    with httpx.Client() as client:
        yield client


@pytest.fixture
def drip(http: httpx.Client) -> DripClient:
    # No deadline thread: several tests change rows from inside the mocked
    # response, which must run on the test's own database connection.
    return DripClient(
        DripSettings(account_id="1234567", api_key=SecretStr("fake-key")),
        http,
        deadline=None,
    )


@pytest.fixture
def conference(conference_factory: Callable[..., Conference]) -> Conference:
    return conference_factory(slug="pycon")


def _at(moment: datetime.datetime) -> Callable[[], datetime.datetime]:
    """A clock frozen at `moment`."""
    return lambda: moment


def _event(email: str = "winner@example.com") -> dict[str, Any]:
    return EventPayload(
        email=email, action="Won raffle prize", properties={"k": "v"}, occurred_at=NOW
    ).to_json()


def _job(conference: Conference, **fields: Any) -> OutboxJob:
    return OutboxJob.objects.create(
        conference=conference,
        kind=J.Kind.EVENT,
        payload=_event(),
        next_attempt_at=NOW,
        **fields,
    )


def _digital_pick(
    conference: Conference,
    drawing_factory: Callable[..., Drawing],
    prize_line_factory: Callable[..., PrizeLine],
    entrant_factory: Callable[..., Entrant],
    pick_factory: Callable[..., Pick],
) -> Pick:
    line = prize_line_factory(
        drawing_factory(conference),
        kind=PrizeLine.Kind.DIGITAL,
        url_template="https://example.com/r?c={code}",
        code_mode=PrizeLine.CodeMode.SHARED,
        shared_code="S1",
    )
    return pick_factory(
        line, entrant_factory(conference), status=Pick.Status.DIGITAL, drawn_at=NOW
    )


# --- enqueueing -------------------------------------------------------------


def test_unconfigured_drip_enqueues_nothing(
    conference: Conference,
    drawing_factory: Callable[..., Drawing],
    prize_line_factory: Callable[..., PrizeLine],
    entrant_factory: Callable[..., Entrant],
    pick_factory: Callable[..., Pick],
    settings: Any,
) -> None:
    settings.DRIP_ACCOUNT_ID = ""
    settings.DRIP_API_KEY = ""
    pick = _digital_pick(
        conference, drawing_factory, prize_line_factory, entrant_factory, pick_factory
    )
    assert outbox.enqueue_won_prize(pick, now=NOW) is None
    assert OutboxJob.objects.count() == 0


def test_enqueue_won_prize_stores_the_event(
    conference: Conference,
    drawing_factory: Callable[..., Drawing],
    prize_line_factory: Callable[..., PrizeLine],
    entrant_factory: Callable[..., Entrant],
    pick_factory: Callable[..., Pick],
) -> None:
    pick = _digital_pick(
        conference, drawing_factory, prize_line_factory, entrant_factory, pick_factory
    )
    job = outbox.enqueue_won_prize(pick, now=NOW)
    assert job is not None
    job.refresh_from_db()
    assert (job.kind, job.status, job.attempts) == (J.Kind.EVENT, J.Status.PENDING, 0)
    assert job.next_attempt_at == NOW
    assert job.conference == conference
    assert job.payload["action"] == "Won raffle prize"
    assert job.payload["properties"]["coupon_code"] == "S1"


# --- processing -------------------------------------------------------------


def test_process_due_marks_sent(
    conference: Conference, drip: DripClient, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.post(EVENTS).mock(return_value=httpx.Response(204))
    job = _job(conference)
    result = outbox.process_due(drip, clock=_at(NOW))
    assert result == outbox.ProcessResult(sent=1, retried=0, failed=0)
    job.refresh_from_db()
    assert (job.status, job.attempts, job.sent_at) == (J.Status.SENT, 1, NOW)
    assert route.call_count == 1


def test_process_due_sends_subscriber_jobs(
    conference: Conference, drip: DripClient, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.post(BATCHES).mock(return_value=httpx.Response(201))
    sub = SubscriberPayload(email="a@example.com", first_name="A", tags=["t"])
    outbox.enqueue(
        conference, J.Kind.SUBSCRIBERS, {"subscribers": [sub.to_json()]}, now=NOW
    )
    assert outbox.process_due(drip, clock=_at(NOW)).sent == 1
    assert route.call_count == 1


def test_only_due_pending_jobs_are_sent_up_to_the_limit(
    conference: Conference, drip: DripClient, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.post(EVENTS).mock(return_value=httpx.Response(204))
    for _ in range(3):
        _job(conference)
    _job(conference, status=J.Status.SENT)
    _job(conference, status=J.Status.FAILED)
    later = OutboxJob.objects.create(
        conference=conference,
        kind=J.Kind.EVENT,
        payload=_event(),
        next_attempt_at=NOW + datetime.timedelta(seconds=1),
    )
    assert outbox.process_due(drip, clock=_at(NOW), limit=2).sent == 2
    assert route.call_count == 2
    assert outbox.process_due(drip, clock=_at(NOW)).sent == 1
    later.refresh_from_db()
    assert later.status == J.Status.PENDING


def test_process_due_backoff_schedule(
    conference: Conference, drip: DripClient, respx_mock: respx.MockRouter
) -> None:
    respx_mock.post(EVENTS).mock(return_value=httpx.Response(503))
    job = _job(conference)
    offsets = []
    for attempt in range(1, 8):
        now = NOW + datetime.timedelta(days=attempt)
        result = outbox.process_due(drip, clock=_at(now))
        assert result == outbox.ProcessResult(sent=0, retried=1, failed=0)
        job.refresh_from_db()
        assert job.attempts == attempt
        offsets.append((job.next_attempt_at - now).total_seconds())
    assert offsets == [60, 300, 1800, 7200, 7200, 7200, 7200]


def test_job_fails_after_8_attempts(
    conference: Conference, drip: DripClient, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.post(EVENTS).mock(return_value=httpx.Response(500))
    job = _job(conference)
    results = [
        outbox.process_due(drip, clock=_at(NOW + datetime.timedelta(days=n)))
        for n in range(1, 10)
    ]
    assert [r.failed for r in results] == [0] * 7 + [1, 0]
    job.refresh_from_db()
    assert (job.status, job.attempts) == (J.Status.FAILED, 8)
    assert route.call_count == 8


def test_429_is_throttled_but_other_4xx_is_permanent(
    conference: Conference, drip: DripClient, respx_mock: respx.MockRouter
) -> None:
    respx_mock.post(EVENTS).mock(side_effect=[httpx.Response(429), httpx.Response(422)])
    throttled = _job(conference)
    assert outbox.process_due(drip, clock=_at(NOW)).retry_after == 60
    throttled.refresh_from_db()
    assert throttled.status == J.Status.PENDING
    throttled.delete()
    bad = _job(conference)
    assert outbox.process_due(drip, clock=_at(NOW)).failed == 1
    bad.refresh_from_db()
    assert (bad.status, bad.attempts, bad.last_error_class) == (
        J.Status.FAILED,
        1,
        "DripError",
    )


def test_network_error_is_retried_with_class_name_only(
    conference: Conference, drip: DripClient, respx_mock: respx.MockRouter
) -> None:
    respx_mock.post(EVENTS).mock(
        side_effect=httpx.ConnectTimeout("timed out talking to winner@example.com")
    )
    job = _job(conference)
    assert outbox.process_due(drip, clock=_at(NOW)).retried == 1
    job.refresh_from_db()
    assert job.status == J.Status.PENDING
    assert job.last_error_class == "ConnectTimeout"


def test_error_record_and_logs_hold_no_personal_data(
    conference: Conference,
    drip: DripClient,
    respx_mock: respx.MockRouter,
    caplog: pytest.LogCaptureFixture,
) -> None:
    respx_mock.post(EVENTS).mock(
        return_value=httpx.Response(422, json={"errors": ["winner@example.com bad"]})
    )
    job = _job(conference)
    with caplog.at_level(logging.DEBUG):
        outbox.process_due(drip, clock=_at(NOW))
    job.refresh_from_db()
    fields = [
        str(getattr(job, f.name)) for f in OutboxJob._meta.fields if f.name != "payload"
    ]
    assert all("winner@example.com" not in value for value in fields)
    assert "winner@example.com" not in caplog.text
    assert "DripError" in caplog.text
    assert str(job.pk) in caplog.text


def test_malformed_payload_fails_permanently(
    conference: Conference, drip: DripClient
) -> None:
    job = OutboxJob.objects.create(
        conference=conference,
        kind=J.Kind.EVENT,
        payload={"nope": 1},
        next_attempt_at=NOW,
    )
    assert outbox.process_due(drip, clock=_at(NOW)).failed == 1
    job.refresh_from_db()
    assert (job.status, job.last_error_class) == (J.Status.FAILED, "ValidationError")


# --- claiming ---------------------------------------------------------------


def test_claimed_job_is_leased_and_due_again_if_the_worker_dies(
    conference: Conference, drip: DripClient, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.post(EVENTS).mock(return_value=httpx.Response(204))
    job = _job(conference)
    assert outbox.claim_due(now=NOW, limit=20) == [job.pk]  # worker "crashes" here
    job.refresh_from_db()
    assert job.status == J.Status.PENDING
    assert job.attempts == 1
    assert job.next_attempt_at == NOW + outbox.LEASE
    assert outbox.claim_due(now=NOW, limit=20) == []
    assert outbox.process_due(drip, clock=_at(NOW)).sent == 0
    assert route.call_count == 0
    later = NOW + outbox.LEASE + datetime.timedelta(seconds=1)
    assert outbox.process_due(drip, clock=_at(later)).sent == 1
    job.refresh_from_db()
    assert (job.status, job.attempts) == (J.Status.SENT, 2)


@pytest.mark.django_db(transaction=True)
def test_claim_skips_a_job_another_worker_has_locked(
    conference_factory: Callable[..., Conference],
) -> None:
    """A second worker takes the next job at once instead of waiting its turn.

    Another connection holds a row lock on the first job (as a concurrent
    claim would); `skip_locked` makes this claim pass over it without blocking.
    """
    conference = conference_factory()
    first, second = _job(conference), _job(conference)
    locked, release = threading.Event(), threading.Event()

    def hold_lock() -> None:
        try:
            with transaction.atomic():
                OutboxJob.objects.select_for_update().get(pk=first.pk)
                locked.set()
                release.wait(timeout=5)
        finally:
            connection.close()

    holder = threading.Thread(target=hold_lock)
    holder.start()
    try:
        assert locked.wait(timeout=5)
        started = time.monotonic()
        claimed = outbox.claim_due(now=NOW, limit=20)
        waited = time.monotonic() - started
    finally:
        release.set()
        holder.join()
    assert claimed == [second.pk]
    assert waited < 2
    first.refresh_from_db()
    assert first.attempts == 0  # untouched: still due for whoever comes next


@pytest.mark.django_db(transaction=True)
def test_http_runs_outside_any_transaction(
    conference_factory: Callable[..., Conference], drip: DripClient
) -> None:
    conference = conference_factory()
    seen: list[bool] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(connection.in_atomic_block)
        return httpx.Response(204)

    try:
        with respx.mock() as router:
            router.post(EVENTS).mock(side_effect=respond)
            _job(conference)
            outbox.process_due(drip, clock=_at(NOW))
        assert seen == [False]
    finally:
        OutboxJob.objects.all().delete()
        Conference.objects.all().delete()


# --- counts and requeue -----------------------------------------------------


def test_counts_and_requeue_failed(
    conference: Conference, conference_factory: Callable[..., Conference]
) -> None:
    other = conference_factory()
    _job(conference)
    _job(conference, status=J.Status.SENT)
    failed = _job(conference, status=J.Status.FAILED, attempts=8)
    _job(other, status=J.Status.FAILED, attempts=3)
    assert outbox.pending_count() == 1
    assert outbox.pending_count(conference) == 1
    assert outbox.pending_count(other) == 0
    assert outbox.failed_count() == 2
    assert outbox.failed_count(conference) == 1
    count = outbox.requeue_failed(
        OutboxJob.objects.filter(conference=conference), now=NOW
    )
    assert count == 1
    failed.refresh_from_db()
    assert (failed.status, failed.attempts, failed.next_attempt_at) == (
        J.Status.PENDING,
        0,
        NOW,
    )
    assert outbox.failed_count() == 1


# --- wiring into the drawing -------------------------------------------------


@pytest.fixture
def owner(client: Client, db: None) -> Client:
    user = User.objects.create_user("owner", password="pw", is_superuser=True)
    client.force_login(user)
    return client


@pytest.fixture(autouse=True)
def _seeded_rng(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(booth, "_rng", lambda: random.Random(0))


def _start_digital_drawing(
    owner: Client,
    conference: Conference,
    drawing_factory: Callable[..., Drawing],
    prize_line_factory: Callable[..., PrizeLine],
    entrant_factory: Callable[..., Entrant],
    quantity: int = 1,
) -> Drawing:
    drawing = drawing_factory(conference)
    line = prize_line_factory(
        drawing,
        kind=PrizeLine.Kind.DIGITAL,
        quantity=quantity,
        url_template="https://example.com/r?c={code}",
        code_mode=PrizeLine.CodeMode.POOL,
    )
    for n in range(quantity):
        CouponCode.objects.create(prize_line=line, code=f"CODE{n}")
    for _ in range(quantity + 2):
        entrant_factory(conference)
    owner.post(reverse("booth-start", args=[drawing.pk]))
    return drawing


def test_digital_pick_from_booth_enqueues_one_event(
    owner: Client,
    conference: Conference,
    drawing_factory: Callable[..., Drawing],
    prize_line_factory: Callable[..., PrizeLine],
    entrant_factory: Callable[..., Entrant],
) -> None:
    _start_digital_drawing(
        owner, conference, drawing_factory, prize_line_factory, entrant_factory
    )
    (job,) = OutboxJob.objects.all()
    pick = Pick.objects.get(status=Pick.Status.DIGITAL)
    assert job.conference == conference
    assert job.payload["email"] == pick.entrant.email
    assert job.payload["properties"]["coupon_code"] == "CODE0"


def test_drawing_never_calls_drip_inline(
    owner: Client,
    conference: Conference,
    drawing_factory: Callable[..., Drawing],
    prize_line_factory: Callable[..., PrizeLine],
    entrant_factory: Callable[..., Entrant],
) -> None:
    with respx.mock(assert_all_called=False) as router:
        events = router.post(EVENTS)
        _start_digital_drawing(
            owner,
            conference,
            drawing_factory,
            prize_line_factory,
            entrant_factory,
            quantity=2,
        )
    assert events.call_count == 0
    assert router.calls.call_count == 0
    assert OutboxJob.objects.count() == 2


def test_enqueue_failure_rolls_back_the_pick(
    owner: Client,
    conference: Conference,
    drawing_factory: Callable[..., Drawing],
    prize_line_factory: Callable[..., PrizeLine],
    entrant_factory: Callable[..., Entrant],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(pick: Pick, *, now: datetime.datetime) -> None:
        raise RuntimeError("outbox down")

    monkeypatch.setattr(booth, "enqueue_won_prize", boom)
    owner.raise_request_exception = False
    drawing = _start_digital_drawing(
        owner, conference, drawing_factory, prize_line_factory, entrant_factory
    )
    drawing.refresh_from_db()
    assert drawing.status == Drawing.Status.SCHEDULED
    assert Pick.objects.count() == 0


# --- worker and status commands ---------------------------------------------


def test_worker_once_processes_and_exits(
    conference: Conference, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.post(EVENTS).mock(return_value=httpx.Response(204))
    job = OutboxJob.objects.create(
        conference=conference, kind=J.Kind.EVENT, payload=_event()
    )
    out = StringIO()
    call_command("drip_worker", "--once", stdout=out)
    job.refresh_from_db()
    assert job.status == J.Status.SENT
    assert route.call_count == 1
    assert "sent=1 retried=0 failed=0" in out.getvalue()


def test_worker_says_nothing_on_an_idle_pass(conference: Conference) -> None:
    """Every 5 s of "sent=0 retried=0 failed=0" would bury the journal."""
    out = StringIO()
    call_command("drip_worker", "--once", stdout=out)
    assert out.getvalue() == ""


def test_worker_idles_when_drip_is_unconfigured(
    conference: Conference, settings: Any
) -> None:
    settings.DRIP_ACCOUNT_ID = ""
    settings.DRIP_API_KEY = ""
    job = OutboxJob.objects.create(
        conference=conference, kind=J.Kind.EVENT, payload=_event()
    )
    out = StringIO()
    call_command("drip_worker", "--once", stdout=out)
    job.refresh_from_db()
    assert job.status == J.Status.PENDING
    assert "not configured" in out.getvalue()


def _stepping_clock(step: datetime.timedelta) -> Callable[[], datetime.datetime]:
    """A clock that advances `step` every time it is read, starting at NOW."""
    ticks = iter(range(10_000))
    return lambda: NOW + step * next(ticks)


def test_lease_runs_from_each_claim_so_a_second_worker_cannot_double_send(
    conference: Conference, drip: DripClient, respx_mock: respx.MockRouter
) -> None:
    """A slow pass must not lease later jobs from the pass's start time."""
    other_worker: list[list[int]] = []
    seen: list[int] = []

    def slow_drip(request: httpx.Request) -> httpx.Response:
        seen.append(1)
        if len(seen) == 2:
            # Clock reads: claim 1 (0 min), outcome 1 (5), claim 2 (10). One
            # minute later is inside job 2's lease but past one from the start.
            late = NOW + datetime.timedelta(minutes=11)
            other_worker.append(outbox.claim_due(now=late, limit=20))
        return httpx.Response(204)

    respx_mock.post(EVENTS).mock(side_effect=slow_drip)
    _job(conference)
    _job(conference)
    result = outbox.process_due(
        drip, clock=_stepping_clock(datetime.timedelta(minutes=5))
    )
    assert result.sent == 2
    assert other_worker == [[]]


def test_backoff_runs_from_the_failure_time_not_the_pass_start(
    conference: Conference, drip: DripClient, respx_mock: respx.MockRouter
) -> None:
    respx_mock.post(EVENTS).mock(return_value=httpx.Response(503))
    job = _job(conference)
    outbox.process_due(
        drip, clock=_stepping_clock(datetime.timedelta(minutes=5)), limit=1
    )
    job.refresh_from_db()
    # clock reads: claim (NOW), failure (NOW + 5 min); backoff starts at the latter.
    assert job.next_attempt_at == NOW + datetime.timedelta(minutes=5, seconds=60)


def test_sent_at_is_the_time_of_the_send(
    conference: Conference, drip: DripClient, respx_mock: respx.MockRouter
) -> None:
    respx_mock.post(EVENTS).mock(return_value=httpx.Response(204))
    job = _job(conference)
    outbox.process_due(drip, clock=_stepping_clock(datetime.timedelta(minutes=5)))
    job.refresh_from_db()
    assert job.sent_at == NOW + datetime.timedelta(minutes=5)


def test_429_does_not_charge_an_attempt_and_stops_the_pass(
    conference: Conference, drip: DripClient, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.post(EVENTS).mock(
        return_value=httpx.Response(429, headers={"Retry-After": "120"})
    )
    first, second = _job(conference), _job(conference)
    result = outbox.process_due(drip, clock=_at(NOW))
    assert result == outbox.ProcessResult(0, 0, 0, retry_after=120)
    assert route.call_count == 1
    first.refresh_from_db()
    second.refresh_from_db()
    assert (first.status, first.attempts) == (J.Status.PENDING, 0)
    assert first.next_attempt_at == NOW + datetime.timedelta(seconds=120)
    assert first.last_error_class == "DripError"
    assert (second.attempts, second.next_attempt_at) == (0, NOW)


def test_429_after_earlier_attempts_keeps_the_attempt_count(
    conference: Conference, drip: DripClient, respx_mock: respx.MockRouter
) -> None:
    respx_mock.post(EVENTS).mock(return_value=httpx.Response(429))
    job = _job(conference, attempts=7)
    result = outbox.process_due(drip, clock=_at(NOW))
    assert result.retry_after == 60
    job.refresh_from_db()
    assert (job.status, job.attempts) == (J.Status.PENDING, 7)


def test_429_from_a_stale_worker_changes_nothing_but_still_stops(
    conference: Conference, drip: DripClient, respx_mock: respx.MockRouter
) -> None:
    def newer_worker_takes_over(request: httpx.Request) -> httpx.Response:
        OutboxJob.objects.update(
            attempts=5, next_attempt_at=NOW + datetime.timedelta(hours=1)
        )
        return httpx.Response(429, headers={"Retry-After": "10"})

    respx_mock.post(EVENTS).mock(side_effect=newer_worker_takes_over)
    job = _job(conference)
    result = outbox.process_due(drip, clock=_at(NOW))
    assert result.retry_after == 10
    job.refresh_from_db()
    assert job.attempts == 5


def test_a_clean_pass_reports_no_retry_after(
    conference: Conference, drip: DripClient, respx_mock: respx.MockRouter
) -> None:
    respx_mock.post(EVENTS).mock(return_value=httpx.Response(204))
    _job(conference)
    assert outbox.process_due(drip, clock=_at(NOW)).retry_after is None


def test_worker_loop_exits_on_sigterm_without_sleeping_out_the_interval(
    conference: Conference,
    monkeypatch: pytest.MonkeyPatch,
    respx_mock: respx.MockRouter,
) -> None:
    passes: list[int] = []
    real = outbox.process_due

    def one_pass_then_term(*args: Any, **kwargs: Any) -> outbox.ProcessResult:
        passes.append(1)
        os.kill(os.getpid(), signal.SIGTERM)
        return real(*args, **kwargs)

    monkeypatch.setattr(outbox, "process_due", one_pass_then_term)
    before = signal.getsignal(signal.SIGTERM)
    call_command("drip_worker", stdout=StringIO())
    assert passes == [1]
    assert signal.getsignal(signal.SIGTERM) is before


def test_outbox_status_exit_codes(conference: Conference) -> None:
    out = StringIO()
    call_command("outbox_status", stdout=out)
    assert out.getvalue().strip() == "pending=0 failed=0"
    _job(conference, status=J.Status.FAILED)
    out = StringIO()
    call_command("outbox_status", stdout=out)
    assert out.getvalue().strip() == "pending=0 failed=1"
    _job(conference)
    out = StringIO()
    with pytest.raises(SystemExit) as caught:
        call_command("outbox_status", stdout=out)
    assert caught.value.code == 1
    assert out.getvalue().strip() == "pending=1 failed=1"


# --- booth ------------------------------------------------------------------


def test_booth_shows_outbox_counts_and_retry_requeues(
    owner: Client, conference: Conference
) -> None:
    _job(conference)
    _job(conference, status=J.Status.FAILED, attempts=8)
    url = reverse("booth-conference", args=[conference.slug])
    page = owner.get(url).content.decode()
    assert "Drip: 1 pending, 1 failed" in page
    assert reverse("booth-retry-failed", args=[conference.slug]) in page
    response = owner.post(reverse("booth-retry-failed", args=[conference.slug]))
    assert response.status_code == 302
    assert response.headers["Location"] == url
    assert "Drip: 2 pending, 0 failed" in owner.get(url).content.decode()
    assert OutboxJob.objects.filter(attempts=0, status=J.Status.PENDING).count() == 2


def test_booth_hides_outbox_when_drip_unconfigured(
    owner: Client, conference: Conference, settings: Any
) -> None:
    settings.DRIP_ACCOUNT_ID = ""
    settings.DRIP_API_KEY = ""
    response = owner.get(reverse("booth-conference", args=[conference.slug]))
    assert response.status_code == 200
    page = response.content.decode()
    assert "Drip:" not in page
    assert "retry-failed" not in page
    response = owner.post(reverse("booth-retry-failed", args=[conference.slug]))
    assert response.status_code == 404


def test_retry_failed_is_login_required_post_only_and_csrf_protected(
    client: Client, conference: Conference, db: None
) -> None:
    failed = _job(conference, status=J.Status.FAILED)
    url = reverse("booth-retry-failed", args=[conference.slug])
    assert client.post(url).status_code == 302  # to login
    user = User.objects.create_user("o2", password="pw", is_superuser=True)
    strict = Client(enforce_csrf_checks=True)
    strict.force_login(user)
    assert strict.post(url).status_code == 403
    assert strict.get(url).status_code == 405
    failed.refresh_from_db()
    assert failed.status == J.Status.FAILED


def test_worker_loop_runs_again_after_the_wait(
    conference: Conference, monkeypatch: pytest.MonkeyPatch
) -> None:
    passes: list[int] = []
    closed: list[int] = []

    def term_on_second_pass(*args: Any, **kwargs: Any) -> outbox.ProcessResult:
        passes.append(1)
        if len(passes) == 2:
            os.kill(os.getpid(), signal.SIGTERM)
        return outbox.ProcessResult(0, 0, 0)

    monkeypatch.setattr(drip_worker, "INTERVAL_SECONDS", 0)
    monkeypatch.setattr(outbox, "process_due", term_on_second_pass)
    monkeypatch.setattr(drip_worker, "close_old_connections", lambda: closed.append(1))
    call_command("drip_worker", stdout=StringIO())
    assert (len(passes), len(closed)) == (2, 1)


@pytest.mark.parametrize(
    ("retry_after", "expected_wait"),
    [
        (120.0, 120.0),
        (99999.0, 3600.0),
        (0.0, drip_worker.INTERVAL_SECONDS),
        (None, drip_worker.INTERVAL_SECONDS),
    ],
)
def test_worker_sleeps_for_retry_after_before_the_next_pass(
    conference: Conference,
    monkeypatch: pytest.MonkeyPatch,
    retry_after: float | None,
    expected_wait: float,
) -> None:
    waits: list[float | None] = []

    class RecordingEvent(threading.Event):
        def wait(self, timeout: float | None = None) -> bool:
            waits.append(timeout)
            return True  # as if a signal woke us: the loop ends

    monkeypatch.setattr(threading, "Event", RecordingEvent)
    monkeypatch.setattr(
        outbox,
        "process_due",
        lambda *a, **k: outbox.ProcessResult(0, 0, 0, retry_after=retry_after),
    )
    call_command("drip_worker", stdout=StringIO())
    assert waits == [expected_wait]


def test_worker_builds_its_http_client_without_trusting_the_environment(
    conference: Conference, monkeypatch: pytest.MonkeyPatch
) -> None:
    built: list[httpx.Client] = []
    real = build_http_client

    def recording() -> httpx.Client:
        built.append(real())
        return built[-1]

    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.setattr(drip_worker, "build_http_client", recording)
    call_command("drip_worker", "--once", stdout=StringIO())
    assert len(built) == 1
    assert not built[0]._mounts
    assert built[0].is_closed


def test_malformed_subscriber_job_fails_permanently(
    conference: Conference, drip: DripClient
) -> None:
    job = OutboxJob.objects.create(
        conference=conference,
        kind=J.Kind.SUBSCRIBERS,
        payload={},
        next_attempt_at=NOW,
    )
    assert outbox.process_due(drip, clock=_at(NOW)).failed == 1
    job.refresh_from_db()
    assert job.last_error_class == "KeyError"


def test_worker_loop_idles_when_unconfigured_until_sigterm(
    conference: Conference, monkeypatch: pytest.MonkeyPatch, settings: Any
) -> None:
    settings.DRIP_ACCOUNT_ID = ""
    settings.DRIP_API_KEY = ""
    job = OutboxJob.objects.create(
        conference=conference, kind=J.Kind.EVENT, payload=_event()
    )
    monkeypatch.setattr(drip_worker, "INTERVAL_SECONDS", 0)
    monkeypatch.setattr(
        drip_worker,
        "close_old_connections",
        lambda: os.kill(os.getpid(), signal.SIGTERM),
    )
    call_command("drip_worker", stdout=StringIO())
    job.refresh_from_db()
    assert job.status == J.Status.PENDING


# --- fix round 1 --------------------------------------------------------------


def test_winner_with_an_address_drip_rejects_does_not_break_the_drawing(
    owner: Client,
    conference: Conference,
    drawing_factory: Callable[..., Drawing],
    prize_line_factory: Callable[..., PrizeLine],
    entrant_factory: Callable[..., Entrant],
    caplog: pytest.LogCaptureFixture,
) -> None:
    drawing = drawing_factory(conference)
    prize_line_factory(
        drawing, kind=PrizeLine.Kind.DIGITAL, url_template="https://example.com/s"
    )
    entrant_factory(conference, email="a@localhost")
    with caplog.at_level(logging.DEBUG):
        response = owner.post(reverse("booth-start", args=[drawing.pk]))
    assert response.status_code == 302
    drawing.refresh_from_db()
    assert drawing.status != Drawing.Status.SCHEDULED
    pick = Pick.objects.get(status=Pick.Status.DIGITAL)
    (job,) = OutboxJob.objects.all()
    assert (job.status, job.last_error_class) == (J.Status.FAILED, "ValidationError")
    assert "localhost" not in caplog.text
    assert str(pick.pk) in caplog.text


def test_stale_worker_cannot_overwrite_a_newer_failure_result(
    conference: Conference, drip: DripClient, respx_mock: respx.MockRouter
) -> None:
    job = _job(conference)

    def other_worker_wins(request: httpx.Request) -> httpx.Response:
        OutboxJob.objects.filter(pk=job.pk).update(
            status=J.Status.SENT, attempts=2, sent_at=NOW
        )
        return httpx.Response(500)

    respx_mock.post(EVENTS).mock(side_effect=other_worker_wins)
    assert outbox.process_due(drip, clock=_at(NOW)) == outbox.ProcessResult(0, 0, 0)
    job.refresh_from_db()
    assert (job.status, job.attempts) == (J.Status.SENT, 2)
    assert job.last_error_class == ""


def test_stale_worker_cannot_overwrite_a_newer_success_result(
    conference: Conference, drip: DripClient, respx_mock: respx.MockRouter
) -> None:
    job = _job(conference)

    def other_worker_reclaims(request: httpx.Request) -> httpx.Response:
        OutboxJob.objects.filter(pk=job.pk).update(
            attempts=2, next_attempt_at=NOW + datetime.timedelta(hours=1)
        )
        return httpx.Response(204)

    respx_mock.post(EVENTS).mock(side_effect=other_worker_reclaims)
    assert outbox.process_due(drip, clock=_at(NOW)).sent == 0
    job.refresh_from_db()
    assert (job.status, job.attempts) == (J.Status.PENDING, 2)


def test_lease_outlasts_one_requests_worst_case() -> None:
    # Every request is cut off at DEADLINE_SECONDS, and a job makes one request.
    assert outbox.LEASE.total_seconds() > DEADLINE_SECONDS
    assert wrapup.BATCH_SIZE <= BATCH_LIMIT  # so one subscriber job = one request


def test_default_client_has_the_overall_deadline() -> None:
    settings = DripSettings(account_id="1", api_key=SecretStr("k"))
    with build_http_client() as http:
        assert DripClient(settings, http)._deadline == DEADLINE_SECONDS


def test_worker_gives_its_client_the_default_deadline(
    conference: Conference, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[float | None] = []
    real = DripClient.__init__

    def spy(self: DripClient, *args: Any, **kwargs: Any) -> None:
        real(self, *args, **kwargs)
        seen.append(self._deadline)

    monkeypatch.setattr(DripClient, "__init__", spy)
    call_command("drip_worker", "--once", stdout=StringIO())
    assert seen == [DEADLINE_SECONDS]


def test_throttle_floor_keeps_a_zero_retry_after_from_hot_looping(
    conference: Conference, drip: DripClient, respx_mock: respx.MockRouter
) -> None:
    respx_mock.post(EVENTS).mock(
        return_value=httpx.Response(429, headers={"Retry-After": "0"})
    )
    job = _job(conference)
    outbox.process_due(drip, clock=_at(NOW))
    job.refresh_from_db()
    assert job.next_attempt_at == NOW + datetime.timedelta(
        seconds=outbox.MIN_THROTTLE_WAIT
    )


def test_stop_request_mid_pass_leaves_unreached_jobs_untouched(
    conference: Conference, drip: DripClient, respx_mock: respx.MockRouter
) -> None:
    stop = {"now": False}

    def send_then_stop(request: httpx.Request) -> httpx.Response:
        stop["now"] = True
        return httpx.Response(204)

    route = respx_mock.post(EVENTS).mock(side_effect=send_then_stop)
    jobs = [_job(conference) for _ in range(3)]
    result = outbox.process_due(drip, clock=_at(NOW), should_stop=lambda: stop["now"])
    assert result.sent == 1
    assert route.call_count == 1
    rest = OutboxJob.objects.filter(pk__in=[j.pk for j in jobs[1:]])
    assert {(j.status, j.attempts, j.next_attempt_at) for j in rest} == {
        (J.Status.PENDING, 0, NOW)
    }


def test_unexpected_error_retires_the_job_with_its_class_name(
    conference: Conference, drip: DripClient, respx_mock: respx.MockRouter
) -> None:
    respx_mock.post(EVENTS).mock(side_effect=RuntimeError("boom winner@example.com"))
    job = _job(conference)
    assert outbox.process_due(drip, clock=_at(NOW)).failed == 1
    job.refresh_from_db()
    assert (job.status, job.last_error_class) == (J.Status.FAILED, "RuntimeError")


def test_non_list_subscribers_payload_is_a_permanent_failure(
    conference: Conference, drip: DripClient
) -> None:
    job = OutboxJob.objects.create(
        conference=conference,
        kind=J.Kind.SUBSCRIBERS,
        payload={"subscribers": 5},
        next_attempt_at=NOW,
    )
    assert outbox.process_due(drip, clock=_at(NOW)).failed == 1
    job.refresh_from_db()
    assert (job.status, job.last_error_class) == (J.Status.FAILED, "TypeError")


def test_jobs_out_of_attempts_are_never_claimed(conference: Conference) -> None:
    job = _job(conference, attempts=outbox.MAX_ATTEMPTS)
    assert outbox.claim_due(now=NOW, limit=20) == []
    job.refresh_from_db()  # retired, so it cannot sit pending forever
    assert (job.status, job.last_error_class) == (J.Status.FAILED, "LeaseExpired")


def test_worker_survives_a_failing_pass_and_logs_only_the_class(
    conference: Conference,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    passes: list[int] = []

    def flaky(*args: Any, **kwargs: Any) -> outbox.ProcessResult:
        passes.append(1)
        if len(passes) == 1:
            raise RuntimeError("secret@example.com")
        os.kill(os.getpid(), signal.SIGTERM)
        return outbox.ProcessResult(0, 0, 0)

    monkeypatch.setattr(drip_worker, "INTERVAL_SECONDS", 0)
    monkeypatch.setattr(outbox, "process_due", flaky)
    monkeypatch.setattr(drip_worker, "close_old_connections", lambda: None)
    with caplog.at_level(logging.DEBUG):
        call_command("drip_worker", stdout=StringIO())
    assert len(passes) == 2
    assert "RuntimeError" in caplog.text
    assert "secret@example.com" not in caplog.text
