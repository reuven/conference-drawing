"""The drawing service: every state change a drawing goes through at the booth.

Each operation runs in one transaction that first locks the drawing row and
re-reads the pick, so two taps (or two phones) on the same button serialize and
the second sees the first's result. The two operations that can open a drawing
(start, and an undo that reopens an auto-finished one) first lock the
conference row FOR NO KEY UPDATE, which conflicts with wrap-up's FOR UPDATE and
with each other: a wrap-up never commits alongside a drawing being opened, and
two phones starting two drawings serialize. Lock order is always conference,
then drawing. Randomness comes only from the injected
`rng`, applied to the sorted id list from `eligible_entrant_ids`, so a seeded
`random.Random` reproduces a drawing exactly. Audit details carry ids and
counts only.
"""

import datetime
import random
import secrets
from collections.abc import Callable
from dataclasses import dataclass

from django.db import transaction
from django.db.models import Sum

from .eligibility import WINNING_STATUSES, eligible_entrant_ids
from .models import AuditEvent, Conference, CouponCode, Drawing, Pick, PrizeLine
from .timezones import to_local

_SYSTEM_RNG = secrets.SystemRandom()

# How long before its scheduled time a drawing may be started without the
# owner's say-so, so a helper can't run the drawing at the wrong moment.
EARLY_START_WINDOW = datetime.timedelta(minutes=15)


class DrawingError(Exception):
    """A refused drawing operation; the message is shown to the organizer as is."""


def _ignore_digital_pick(pick: Pick) -> None:
    """Default hand-off for digital picks until the Drip outbox is wired in."""


def _lock(drawing_id: int) -> Drawing:
    return Drawing.objects.select_for_update().get(pk=drawing_id)


_WRAPPED_UP = "This conference has been wrapped up."


def _lock_conference_then(drawing_id: int) -> Drawing:
    """Lock the drawing's conference (FOR NO KEY UPDATE), then the drawing.

    Refuses once the conference is wrapped up. A drawing never changes
    conference, so reading the id unlocked first is safe.
    """
    conference_id = Drawing.objects.values_list("conference_id", flat=True).get(
        pk=drawing_id
    )
    conference = Conference.objects.select_for_update(no_key=True).get(pk=conference_id)
    if conference.wrapped_up_at is not None:
        raise DrawingError(_WRAPPED_UP)
    return _lock(drawing_id)


def _lock_for_pick(pick: Pick, *, conference: bool = False) -> tuple[Drawing, Pick]:
    """Lock the pick's drawing, then re-read the pick so a stale copy can't act.

    With `conference`, lock (and check) the conference first, for operations
    that may reopen the drawing.
    """
    drawing_id = pick.prize_line.drawing_id
    drawing = _lock_conference_then(drawing_id) if conference else _lock(drawing_id)
    return drawing, Pick.objects.select_related("prize_line").get(pk=pick.pk)


def _another_open(drawing: Drawing) -> bool:
    return (
        Drawing.objects.filter(
            conference_id=drawing.conference_id, status=Drawing.Status.OPEN
        )
        .exclude(pk=drawing.pk)
        .exists()
    )


def _require_open(drawing: Drawing) -> None:
    if drawing.status != Drawing.Status.OPEN:
        raise DrawingError("This drawing isn't open.")


def _audit(
    drawing: Drawing,
    kind: AuditEvent.Kind,
    now: datetime.datetime,
    pick: Pick | None = None,
    **detail: int | str,
) -> None:
    AuditEvent.objects.create(
        drawing=drawing, kind=kind, pick=pick, at=now, detail=detail
    )


def _draw_slot(
    drawing: Drawing,
    line: PrizeLine,
    slot: int,
    *,
    now: datetime.datetime,
    rng: random.Random,
    replaces: Pick | None = None,
) -> Pick | None:
    """Pick one eligible entrant for `slot`, or audit `exhausted` if nobody is left.

    Anyone already voided for this same prize line and slot is left out, so
    Not here, Undo, Not here can't reroll a slot until a chosen person wins:
    each reroll costs a person. A voided person stays eligible for other slots.
    """
    voided_here = set(
        Pick.objects.filter(
            prize_line=line, slot=slot, status=Pick.Status.VOID
        ).values_list("entrant_id", flat=True)
    )
    pool = [e for e in eligible_entrant_ids(drawing) if e not in voided_here]
    if not pool:
        _audit(drawing, AuditEvent.Kind.EXHAUSTED, now, prize_line=line.pk, slot=slot)
        return None
    digital = line.kind == PrizeLine.Kind.DIGITAL
    pick = Pick.objects.create(
        prize_line=line,
        entrant_id=rng.choice(pool),
        slot=slot,
        status=Pick.Status.DIGITAL if digital else Pick.Status.PENDING,
        pool_size=len(pool),
        drawn_at=now,
        replaces=replaces,
    )
    _audit(
        drawing,
        AuditEvent.Kind.PICKED,
        now,
        pick,
        prize_line=line.pk,
        slot=slot,
        pool_size=len(pool),
    )
    return pick


def _finish_if_all_claimed(drawing: Drawing, now: datetime.datetime) -> None:
    """Finish the drawing once every physical copy has a claimed pick.

    With no physical lines this is true at once, which is how a digital-only
    drawing finishes as it starts. No `closed` audit: that marks a manual close,
    which `undo_last` treats as final.
    """
    copies = PrizeLine.objects.filter(
        drawing=drawing, kind=PrizeLine.Kind.PHYSICAL
    ).aggregate(total=Sum("quantity"))["total"]
    claimed = Pick.objects.filter(
        prize_line__drawing=drawing, status=Pick.Status.CLAIMED
    ).count()
    if claimed == (copies or 0):
        _set_finished(drawing, now)


def _set_finished(drawing: Drawing, now: datetime.datetime) -> None:
    drawing.status = Drawing.Status.FINISHED
    drawing.closed_at = now
    drawing.awarded_count = Pick.objects.filter(
        prize_line__drawing=drawing, status__in=WINNING_STATUSES
    ).count()
    drawing.save(update_fields=["status", "closed_at", "awarded_count"])


@transaction.atomic
def start_drawing(
    drawing: Drawing,
    *,
    now: datetime.datetime,
    rng: random.Random = _SYSTEM_RNG,
    on_digital_pick: Callable[[Pick], None] = _ignore_digital_pick,
    early_ok: bool = False,
) -> list[Pick]:
    """Open a scheduled drawing and pick a winner for every copy, line by line.

    The cutoff is the earlier of the scheduled time and now, so starting late
    never admits entries made after the announced time. `on_digital_pick` runs
    inside this transaction, so whatever it writes commits or rolls back with
    the pick.

    Starting more than `EARLY_START_WINDOW` before the scheduled time is
    refused unless `early_ok`, which only the owner's confirmed start sets.
    """
    drawing = _lock_conference_then(drawing.pk)
    if drawing.status != Drawing.Status.SCHEDULED:
        raise DrawingError("This drawing has already started.")
    if not early_ok and now < drawing.scheduled_at - EARLY_START_WINDOW:
        local = to_local(drawing.scheduled_at, drawing.conference.time_zone)
        when = local.strftime("%-I:%M %p")
        if local.date() != to_local(now, drawing.conference.time_zone).date():
            when += local.strftime(" on %b %-d")
        raise DrawingError(f"This drawing can start at {when}.")
    if _another_open(drawing):
        raise DrawingError("Another drawing is open. Close it first.")
    lines = list(
        PrizeLine.objects.filter(drawing=drawing)
        .select_related("book")
        .order_by("position", "id")
    )
    unused_codes: dict[int, list[CouponCode]] = {}
    for line in lines:
        if line.code_mode != PrizeLine.CodeMode.POOL:
            continue
        codes = list(
            CouponCode.objects.filter(prize_line=line, used=False).order_by("id")
        )
        if len(codes) < line.quantity:
            raise DrawingError(
                f"{line.book.title}: needs {line.quantity} codes, has {len(codes)}."
            )
        unused_codes[line.pk] = codes
    drawing.status = Drawing.Status.OPEN
    drawing.started_at = now
    drawing.cutoff_at = min(drawing.scheduled_at, now)
    drawing.save(update_fields=["status", "started_at", "cutoff_at"])
    _audit(drawing, AuditEvent.Kind.STARTED, now)
    picks: list[Pick] = []
    for line in lines:
        for slot in range(1, line.quantity + 1):
            pick = _draw_slot(drawing, line, slot, now=now, rng=rng)
            if pick is None:
                continue
            picks.append(pick)
            if pick.status != Pick.Status.DIGITAL:
                continue
            if line.pk in unused_codes:
                code = unused_codes[line.pk].pop(0)
                code.pick = pick
                code.save(update_fields=["pick"])
            on_digital_pick(pick)
    _finish_if_all_claimed(drawing, now)
    return picks


def _mark(
    pick: Pick, status: Pick.Status, now: datetime.datetime
) -> tuple[Drawing, Pick]:
    """Lock, verify the pick is still pending, and move it to `status`."""
    drawing, pick = _lock_for_pick(pick)
    _require_open(drawing)
    if pick.status in (Pick.Status.DIGITAL, Pick.Status.VOID):
        raise DrawingError("This pick can't be marked.")
    if pick.status != Pick.Status.PENDING:
        raise DrawingError("Already marked.")
    pick.status = status
    pick.marked_at = now
    pick.save(update_fields=["status", "marked_at"])
    return drawing, pick


@transaction.atomic
def mark_claimed(pick: Pick, *, now: datetime.datetime) -> Pick:
    """Record that a physical winner came to the booth and took the book."""
    drawing, pick = _mark(pick, Pick.Status.CLAIMED, now)
    _audit(drawing, AuditEvent.Kind.CLAIMED, now, pick)
    _finish_if_all_claimed(drawing, now)
    return pick


@transaction.atomic
def mark_not_here(
    pick: Pick, *, now: datetime.datetime, rng: random.Random = _SYSTEM_RNG
) -> Pick | None:
    """Pass over an absent winner and redraw that copy at once.

    Returns the replacement, or None when nobody eligible is left (audited as
    `exhausted`; the copy goes unawarded unless the mark is undone).
    """
    drawing, pick = _mark(pick, Pick.Status.NOT_HERE, now)
    line = pick.prize_line
    if line.kind != PrizeLine.Kind.PHYSICAL:  # digital picks are never pending
        raise AssertionError(f"Pick {pick.pk} is pending on a non-physical line.")
    _audit(drawing, AuditEvent.Kind.NOT_HERE, now, pick)
    return _draw_slot(drawing, line, pick.slot, now=now, rng=rng, replaces=pick)


@dataclass(frozen=True)
class UndoRule:
    """Which picks of one drawing may be undone, decided once per drawing.

    Undo works while the drawing is open, or to reverse the claim that
    auto-finished it. It never works after wrap-up (Drip already has the
    winners) or after the owner closed the drawing. Reopening is also refused
    while another drawing is open, or once a later drawing has started: that
    drawing's pool was computed with this winner already counted. The booth
    asks `can_undo` whether to show the button and `undo_last` raises
    `refusal`, so the two never disagree.
    """

    drawing_open: bool
    reopen_refusal: str | None

    def refusal(self, pick: Pick, replacement: Pick | None) -> str | None:
        """Why `pick` can't be undone, or None. `replacement` is its live redraw."""
        if not self.drawing_open:
            if self.reopen_refusal is not None:
                return self.reopen_refusal
            if pick.status != Pick.Status.CLAIMED:
                return "This drawing isn't open."
            return None
        if pick.status == Pick.Status.CLAIMED:
            return None
        if pick.status != Pick.Status.NOT_HERE:
            return "Nothing to undo."
        if replacement is not None and replacement.status != Pick.Status.PENDING:
            return "The replacement has already been marked."
        return None

    def can_undo(self, pick: Pick, replacement: Pick | None) -> bool:
        """Whether the booth should offer Undo on `pick`."""
        return self.refusal(pick, replacement) is None


def _reopen_refusal(drawing: Drawing) -> str | None:
    """Why an undo can't reopen this drawing (which isn't open), or None."""
    if Conference.objects.filter(
        pk=drawing.conference_id, wrapped_up_at__isnull=False
    ).exists():
        return _WRAPPED_UP
    if drawing.closed_by_owner:
        return "This drawing was closed."
    if drawing.status != Drawing.Status.FINISHED:
        return "This drawing isn't open."
    if _another_open(drawing):
        return "Another drawing is open. Close it first."
    later_started = (
        Drawing.objects.filter(
            conference_id=drawing.conference_id, started_at__gte=drawing.closed_at
        )
        .exclude(pk=drawing.pk)
        .exists()
    )
    if later_started:
        return "A later drawing has already started, so this one can't be reopened."
    return None


def undo_rule(drawing: Drawing) -> UndoRule:
    """The undo rule for `drawing` as the database has it now."""
    if drawing.status == Drawing.Status.OPEN:
        return UndoRule(drawing_open=True, reopen_refusal=None)
    return UndoRule(drawing_open=False, reopen_refusal=_reopen_refusal(drawing))


@transaction.atomic
def undo_last(pick: Pick, *, now: datetime.datetime) -> Pick:
    """Reverse a mis-tapped Claimed or Not here, recording the reversal.

    Undoing Not here voids its replacement, so long as nobody has marked the
    replacement yet. Undoing the claim that auto-finished the drawing reopens
    it. History is never deleted: the void and the undo are audited. The
    conference is locked first, so a concurrent wrap-up either waits for this
    undo to commit (and then sees the open drawing) or makes it refuse.
    """
    drawing, pick = _lock_for_pick(pick, conference=True)
    previous = pick.status
    replacement = (
        Pick.objects.filter(replaces=pick).exclude(status=Pick.Status.VOID).first()
        if previous == Pick.Status.NOT_HERE
        else None
    )
    refusal = undo_rule(drawing).refusal(pick, replacement)
    if refusal is not None:
        raise DrawingError(refusal)
    if drawing.status != Drawing.Status.OPEN:
        drawing.status = Drawing.Status.OPEN
        drawing.closed_at = None
        drawing.awarded_count = 0
        drawing.save(update_fields=["status", "closed_at", "awarded_count"])
    if replacement is not None:
        replacement.status = Pick.Status.VOID
        replacement.marked_at = now
        replacement.save(update_fields=["status", "marked_at"])
        _audit(drawing, AuditEvent.Kind.VOID, now, replacement)
    pick.status = Pick.Status.PENDING
    pick.marked_at = None
    pick.save(update_fields=["status", "marked_at"])
    _audit(drawing, AuditEvent.Kind.UNDO, now, pick, **{"from": previous})
    return pick


@transaction.atomic
def close_drawing(drawing: Drawing, *, now: datetime.datetime) -> None:
    """End an open drawing, leaving any copies nobody claimed unawarded."""
    drawing = _lock(drawing.pk)
    _require_open(drawing)
    pending = Pick.objects.filter(
        prize_line__drawing=drawing,
        prize_line__kind=PrizeLine.Kind.PHYSICAL,
        status=Pick.Status.PENDING,
    )
    if pending.exists():
        raise DrawingError("Mark every pick Claimed or Not here first.")
    drawing.closed_by_owner = True
    drawing.save(update_fields=["closed_by_owner"])
    _set_finished(drawing, now)
    _audit(drawing, AuditEvent.Kind.CLOSED, now, awarded_count=drawing.awarded_count)
