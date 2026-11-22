"""The Drip outbox: enqueue inside the booth's transaction, send from a worker.

A Drip outage must never touch the booth, so nothing here is called inline from
a request except `enqueue*`, which only writes a row. The worker (`drip_worker`)
runs `process_due` in three phases: claim jobs in one short transaction, send
over HTTP with no transaction open, then record each outcome.

There is no "in flight" status. Claiming instead pushes `next_attempt_at` out by
`LEASE` and counts the attempt, so a worker that dies mid-send simply has its
jobs come due again once the lease runs out.
"""

import datetime
import logging
from typing import NamedTuple

import httpx
from django.db import transaction
from django.db.models import F, QuerySet
from pydantic import ValidationError

from ..models import Conference, OutboxJob, Pick
from .client import DripClient, DripError
from .config import drip_settings
from .payloads import EventPayload, SubscriberPayload, won_prize_event

logger = logging.getLogger(__name__)

BACKOFF_SECONDS = (60, 300, 1800, 7200)
MAX_ATTEMPTS = 8
LEASE = datetime.timedelta(minutes=2)


class ProcessResult(NamedTuple):
    """What one `process_due` pass did, as counts."""

    sent: int
    retried: int
    failed: int


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
    return enqueue(
        conference, OutboxJob.Kind.EVENT, won_prize_event(pick).to_json(), now=now
    )


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

    One short transaction: select the due pending ids, then push each job's
    `next_attempt_at` out by `LEASE` and count the attempt. SQLite's IMMEDIATE
    transactions serialize this against the booth's writes and other workers.
    """
    with transaction.atomic():
        ids = list(
            OutboxJob.objects.filter(
                status=OutboxJob.Status.PENDING, next_attempt_at__lte=now
            )
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
) -> bool:
    """Save a failed attempt; return True if the job is now permanently failed."""
    give_up = permanent or job.attempts >= MAX_ATTEMPTS
    fields: dict[str, object] = {"last_error_class": type(exc).__name__}
    if give_up:
        fields["status"] = OutboxJob.Status.FAILED
    else:
        wait = BACKOFF_SECONDS[min(job.attempts, len(BACKOFF_SECONDS)) - 1]
        fields["next_attempt_at"] = now + datetime.timedelta(seconds=wait)
    OutboxJob.objects.filter(pk=job.pk).update(**fields)
    logger.warning(
        "outbox job %s attempt %s failed: %s (%s)",
        job.pk,
        job.attempts,
        type(exc).__name__,
        "giving up" if give_up else "will retry",
    )
    return give_up


def process_due(
    client: DripClient, *, now: datetime.datetime, limit: int = 20
) -> ProcessResult:
    """Send due jobs and record each outcome; never holds a transaction over HTTP.

    A 4xx other than 429 (and a payload that no longer validates) is permanent.
    Throttling, 5xx and network errors back off, and fail after `MAX_ATTEMPTS`.
    Only exception class names are recorded and logged.
    """
    sent = retried = failed = 0
    error: Exception
    permanent: bool
    for job in OutboxJob.objects.filter(pk__in=claim_due(now=now, limit=limit)):
        try:
            _send(client, job)
        except DripError as exc:
            permanent, error = not exc.retryable, exc
        except httpx.HTTPError as exc:
            permanent, error = False, exc
        except (ValidationError, KeyError) as exc:
            permanent, error = True, exc
        else:
            OutboxJob.objects.filter(pk=job.pk).update(
                status=OutboxJob.Status.SENT, sent_at=now
            )
            sent += 1
            continue
        if _record_failure(job, error, permanent=permanent, now=now):
            failed += 1
        else:
            retried += 1
    return ProcessResult(sent=sent, retried=retried, failed=failed)
