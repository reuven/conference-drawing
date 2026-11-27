"""The Drip outbox: enqueue inside the booth's transaction, send from a worker.

A Drip outage must never touch the booth, so nothing here is called inline from
a request except `enqueue*`, which only writes a row. The worker (`drip_worker`)
runs `process_due` in three phases: claim jobs in one short transaction, send
over HTTP with no transaction open, then record each outcome.

There is no "in flight" status. Claiming instead pushes `next_attempt_at` out by
`LEASE` and counts the attempt, so a worker that dies mid-send simply has its
jobs come due again once the lease runs out. Recording an outcome is
conditional on still holding that lease, so a slow worker cannot overwrite a
newer worker's result.

Delivery is at-least-once: a read timeout after Drip accepted an event is
retried, so the event can arrive twice.
"""

import datetime
import logging
from collections.abc import Callable
from typing import NamedTuple

import httpx
from django.db import transaction
from django.db.models import F, QuerySet
from django.utils import timezone
from pydantic import ValidationError

from ..models import Conference, OutboxJob, Pick
from .client import DripClient, DripError
from .config import drip_settings
from .payloads import EventPayload, SubscriberPayload, won_prize_event

logger = logging.getLogger(__name__)

BACKOFF_SECONDS = (60, 300, 1800, 7200)
MAX_ATTEMPTS = 8
# httpx timeouts apply per phase (connect/write/read/pool), so one request can
# block for about 4 x 10 s; jobs are claimed one at a time, so this covers it.
LEASE = datetime.timedelta(minutes=2)


class ProcessResult(NamedTuple):
    """What one `process_due` pass did, as counts."""

    sent: int
    retried: int
    failed: int
    retry_after: float | None = None  # set when Drip throttled: wait this long


def enqueue(
    conference: Conference,
    kind: OutboxJob.Kind,
    payload: dict[str, object],
    *,
    now: datetime.datetime,
) -> OutboxJob:
    """Queue one Drip call, due immediately."""
    return OutboxJob.objects.create(
        conference=conference, kind=kind, payload=payload, next_attempt_at=now
    )


def enqueue_won_prize(pick: Pick, *, now: datetime.datetime) -> OutboxJob | None:
    """Queue the `Won raffle prize` event for a digital pick; None without Drip.

    Call this inside the pick's transaction so the pick and its job commit or
    roll back together.
    """
    if drip_settings() is None:
        return None
    conference = pick.prize_line.drawing.conference
    try:
        payload = won_prize_event(pick).to_json()
    except ValidationError:
        # Entry validates emails the same way, so this should not happen; if it
        # does, the drawing must still stand. Log the pick id only.
        logger.warning("outbox: could not build the event for pick %s", pick.pk)
        return OutboxJob.objects.create(
            conference=conference,
            kind=OutboxJob.Kind.EVENT,
            payload={},
            status=OutboxJob.Status.FAILED,
            last_error_class="ValidationError",
            next_attempt_at=now,
        )
    return enqueue(conference, OutboxJob.Kind.EVENT, payload, now=now)


def pending_count(conference: Conference | None = None) -> int:
    """Count jobs still waiting to be sent, for one conference or all."""
    return _count(OutboxJob.Status.PENDING, conference)


def failed_count(conference: Conference | None = None) -> int:
    """Count jobs that gave up, for one conference or all."""
    return _count(OutboxJob.Status.FAILED, conference)


def _count(status: OutboxJob.Status, conference: Conference | None) -> int:
    jobs = OutboxJob.objects.filter(status=status)
    if conference is not None:
        jobs = jobs.filter(conference=conference)
    return jobs.count()


def requeue_failed(jobs: QuerySet[OutboxJob], *, now: datetime.datetime) -> int:
    """Put the failed jobs in `jobs` back in line with a fresh attempt budget.

    The one place that knows how to requeue; the admin action and the booth's
    retry button both call it.
    """
    return jobs.filter(status=OutboxJob.Status.FAILED).update(
        status=OutboxJob.Status.PENDING, attempts=0, next_attempt_at=now
    )


def claim_due(*, now: datetime.datetime, limit: int) -> list[int]:
    """Lease up to `limit` due jobs and return their ids.

    One short transaction: lock the due pending rows, then push each job's
    `next_attempt_at` out by `LEASE` and count the attempt. `skip_locked`
    passes over rows another worker is claiming right now, so concurrent
    workers never wait on each other or claim the same job.
    A job whose lease lapsed after its last attempt (a crash mid-send) is
    retired as failed rather than claimed again, so a poison job cannot loop.
    """
    with transaction.atomic():
        OutboxJob.objects.filter(
            status=OutboxJob.Status.PENDING,
            next_attempt_at__lte=now,
            attempts__gte=MAX_ATTEMPTS,
        ).update(status=OutboxJob.Status.FAILED, last_error_class="LeaseExpired")
        ids = list(
            OutboxJob.objects.filter(
                status=OutboxJob.Status.PENDING, next_attempt_at__lte=now
            )
            .select_for_update(skip_locked=True)
            .order_by("next_attempt_at", "id")
            .values_list("pk", flat=True)[:limit]
        )
        OutboxJob.objects.filter(pk__in=ids).update(
            attempts=F("attempts") + 1, next_attempt_at=now + LEASE
        )
    return ids


def _send(client: DripClient, job: OutboxJob) -> None:
    if job.kind == OutboxJob.Kind.EVENT:
        client.record_event(EventPayload.model_validate(job.payload))
    else:
        client.upsert_subscribers(
            [SubscriberPayload.model_validate(s) for s in job.payload["subscribers"]]
        )


def _record_failure(
    job: OutboxJob, exc: Exception, *, permanent: bool, now: datetime.datetime
) -> bool | None:
    """Save a failed attempt, if this worker still holds the lease.

    Returns True if the job is now permanently failed, False if it will be
    retried, and None if the lease was lost (another worker owns the job).
    """
    give_up = permanent or job.attempts >= MAX_ATTEMPTS
    fields: dict[str, object] = {"last_error_class": type(exc).__name__}
    if give_up:
        fields["status"] = OutboxJob.Status.FAILED
    else:
        wait = BACKOFF_SECONDS[min(job.attempts, len(BACKOFF_SECONDS)) - 1]
        fields["next_attempt_at"] = now + datetime.timedelta(seconds=wait)
    if not _still_leased(job).update(**fields):
        return None
    logger.warning(
        "outbox job %s attempt %s failed: %s (%s)",
        job.pk,
        job.attempts,
        type(exc).__name__,
        "giving up" if give_up else "will retry",
    )
    return give_up


def _record_throttled(
    job: OutboxJob, retry_after: float, *, now: datetime.datetime
) -> None:
    """Hand a throttled job back without charging an attempt.

    Drip said "later", not "no", so the claim's attempt is given back and the
    job waits out `retry_after`. Conditional on still holding the lease.
    """
    _still_leased(job).update(
        attempts=job.attempts - 1,
        next_attempt_at=now + datetime.timedelta(seconds=retry_after),
        last_error_class="DripError",
    )
    logger.warning(
        "outbox job %s throttled by Drip; waiting %.0f s", job.pk, retry_after
    )


def _still_leased(job: OutboxJob) -> QuerySet[OutboxJob]:
    """Select the job only if it is exactly as this worker's claim left it.

    `job` was read right after the claim, so its `next_attempt_at` is the lease
    this worker was given; a re-claim by another worker changes it.
    """
    return OutboxJob.objects.filter(
        pk=job.pk,
        status=OutboxJob.Status.PENDING,
        attempts=job.attempts,
        next_attempt_at=job.next_attempt_at,
    )


def process_due(
    client: DripClient,
    *,
    clock: Callable[[], datetime.datetime] = timezone.now,
    limit: int = 20,
    should_stop: Callable[[], bool] = lambda: False,
) -> ProcessResult:
    """Send due jobs and record each outcome; never holds a transaction over HTTP.

    Jobs are claimed one at a time, so a stop request (`should_stop`) between
    jobs leaves the rest untouched and due, and each lease only has to outlast
    one request. A 429 stops the pass and is reported (the worker waits it out)
    without costing the job an attempt. A 4xx other than 429 is permanent, as
    is any unexpected error (a payload that no longer validates, a bug): those
    must not loop forever.
    Throttling, 5xx and network errors back off, and fail after `MAX_ATTEMPTS`.
    Only exception class names are recorded and logged.

    A pass can outlast the lease when Drip is slow, so the time is read fresh
    from `clock` at every claim and every outcome, never once per pass.
    """
    sent = retried = failed = 0
    error: Exception
    for _ in range(limit):
        if should_stop():
            break
        ids = claim_due(now=clock(), limit=1)
        if not ids:
            break
        job = OutboxJob.objects.get(pk=ids[0])
        try:
            _send(client, job)
        except DripError as exc:
            if exc.status_code == 429 and exc.retry_after is not None:
                _record_throttled(job, exc.retry_after, now=clock())
                return ProcessResult(sent, retried, failed, retry_after=exc.retry_after)
            permanent, error = not exc.retryable, exc
        except httpx.HTTPError as exc:
            permanent, error = False, exc
        except Exception as exc:  # noqa: BLE001 - retire poison jobs, never loop
            permanent, error = True, exc
        else:
            if _still_leased(job).update(status=OutboxJob.Status.SENT, sent_at=clock()):
                sent += 1
            continue
        outcome = _record_failure(job, error, permanent=permanent, now=clock())
        if outcome is True:
            failed += 1
        elif outcome is False:
            retried += 1
    return ProcessResult(sent=sent, retried=retried, failed=failed)
