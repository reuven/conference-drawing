"""Wrap-up: lock the conference and hand every entrant to Drip via the outbox.

After the last drawing the owner wraps up once. In a single transaction we
check nothing is still in play, stamp `wrapped_up_at` (which closes the entry
form and stops new drawings), and, when Drip is configured, queue one batch of
tagged subscriber upserts per 1000 entrants plus an `Entered raffle` event per
entrant. The worker sends them later, so a Drip outage never blocks the booth.

Only opt-ins carry EU consent. Everyone else carries no consent fields at all:
sending `denied` would overwrite a grant they gave Drip some other way.

Logs carry ids and counts only.
"""

import datetime
import logging
from typing import NamedTuple

from django.db import transaction
from pydantic import ValidationError

from .draw import DrawingError
from .drip.config import drip_settings
from .drip.outbox import enqueue
from .drip.payloads import EventPayload, SubscriberPayload
from .eligibility import winning_picks
from .models import Conference, Drawing, Entrant, InstallSettings, OutboxJob, Pick

logger = logging.getLogger(__name__)

BATCH_SIZE = 1000


class WrapUpResult(NamedTuple):
    """What a wrap-up did, as counts.

    `skipped` entrants could not be turned into a valid Drip payload (for
    example an old row with an address the payload rejects); they are left out
    of the batch rather than failing the wrap-up. `excluded` entrants are on the
    conference's exclusion list (the owner's and staff's own test entries) and
    are never sent to Drip.
    """

    entrants: int
    winners: int
    jobs: int
    skipped: int = 0
    excluded: int = 0


class WrapUpCounts(NamedTuple):
    """The numbers shown on the confirmation page."""

    entrants: int
    winners: int
    nonwinners: int
    optins: int
    excluded: int


def counts(conference: Conference) -> WrapUpCounts:
    """Count entrants, winners, nonwinners and Better Developers opt-ins.

    Read-only, so the confirmation page can show what wrap-up will do without
    doing any of it.
    """
    entrants = Entrant.objects.filter(conference=conference)
    total = entrants.count()
    winners = entrants.filter(
        id__in=winning_picks(conference).values("entrant_id")
    ).count()
    optins = entrants.filter(bd_optin_at__isnull=False).count()
    excluded = entrants.filter(email__in=conference.excluded_emails).count()
    return WrapUpCounts(total, winners, total - winners, optins, excluded)


def _refuse_unless_ready(conference: Conference) -> None:
    if conference.wrapped_up_at is not None:
        raise DrawingError("This conference is already wrapped up.")
    if Drawing.objects.filter(
        conference=conference, status=Drawing.Status.OPEN
    ).exists():
        raise DrawingError("A drawing is still open. Close it before wrapping up.")
    if Pick.objects.filter(
        prize_line__drawing__conference=conference, status=Pick.Status.PENDING
    ).exists():
        raise DrawingError("Some winners have not been claimed or marked not here yet.")


def wrap_up(conference: Conference, *, now: datetime.datetime) -> WrapUpResult:
    """Lock `conference` and queue its entrants for Drip; refuse with `DrawingError`.

    Refuses if it is already wrapped up, a drawing is open, or any pick is still
    pending. The checks, the `wrapped_up_at` stamp and every enqueue share one
    transaction, so a refusal or a crash leaves nothing half done. Without Drip
    configured it only locks the conference. Entrants on the exclusion list are
    never sent. The caller's object is stamped only after the commit.
    """
    with transaction.atomic():
        locked = Conference.objects.select_for_update().get(pk=conference.pk)
        _refuse_unless_ready(locked)
        locked.wrapped_up_at = now
        locked.save(update_fields=["wrapped_up_at"])
        summary = counts(locked)
        jobs = skipped = 0
        if drip_settings() is not None:
            jobs, skipped = _enqueue_entrants(locked, now)
    conference.wrapped_up_at = now
    logger.info(
        "wrapup conference=%s entrants=%s winners=%s jobs=%s skipped=%s excluded=%s",
        locked.pk,
        summary.entrants,
        summary.winners,
        jobs,
        skipped,
        summary.excluded,
    )
    return WrapUpResult(
        summary.entrants, summary.winners, jobs, skipped, summary.excluded
    )


def _answers(entrant: Entrant) -> dict[str, str]:
    """The entrant's answers by question key, tolerating a malformed JSON value."""
    raw = entrant.answers if isinstance(entrant.answers, dict) else {}
    return {str(k): str(v) for k, v in raw.items()}


def _enqueue_entrants(
    conference: Conference, now: datetime.datetime
) -> tuple[int, int]:
    """Queue subscriber batches and `Entered raffle` events; return (jobs, skipped).

    An entrant whose payload fails validation is skipped and logged by id, the
    way `enqueue_won_prize` treats a bad pick, so one old row cannot block the
    whole wrap-up.
    """
    tags = conference.tags()
    winner_ids = set(winning_picks(conference).values_list("entrant_id", flat=True))
    install_text = InstallSettings.get().bd_checkbox_text
    subscribers: list[dict[str, object]] = []
    events: list[dict[str, object]] = []
    skipped = 0
    sendable = Entrant.objects.filter(conference=conference).exclude(
        email__in=conference.excluded_emails
    )
    for entrant in sendable.order_by("id"):
        entrant_tags = [
            tags.all,
            tags.winner if entrant.pk in winner_ids else tags.nonwinner,
        ]
        optin = entrant.bd_optin_at is not None
        if optin:
            entrant_tags.append(tags.bd_optin)
        try:
            subscriber = SubscriberPayload.from_entrant(
                entrant,
                tags=entrant_tags,
                # The wording this person actually saw; the install's current
                # text only if the record somehow has none.
                consent_message=(entrant.bd_optin_text or install_text)
                if optin
                else None,
            )
            event = EventPayload(
                email=entrant.email,
                action="Entered raffle",
                properties={**_answers(entrant), "conference": conference.name},
                occurred_at=entrant.created_at,
            )
        except ValidationError:
            logger.warning("wrapup: skipped entrant %s (invalid payload)", entrant.pk)
            skipped += 1
            continue
        subscribers.append(subscriber.to_json())
        events.append(event.to_json())
    jobs = 0
    for start in range(0, len(subscribers), BATCH_SIZE):
        batch: dict[str, object] = {
            "subscribers": subscribers[start : start + BATCH_SIZE]
        }
        enqueue(conference, OutboxJob.Kind.SUBSCRIBERS, batch, now=now)
        jobs += 1
    for event_json in events:
        enqueue(conference, OutboxJob.Kind.EVENT, event_json, now=now)
        jobs += 1
    return jobs, skipped
