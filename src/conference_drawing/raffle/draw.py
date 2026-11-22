"""The drawing service: every state change a drawing goes through at the booth.

Each operation runs in one transaction that first locks the drawing row and
re-reads the pick, so two taps (or two phones) on the same button serialize and
the second sees the first's result. Randomness comes only from the injected
`rng`, applied to the sorted id list from `eligible_entrant_ids`, so a seeded
`random.Random` reproduces a drawing exactly. Audit details carry ids and
counts only.
"""

import datetime
import random
import secrets
from collections.abc import Callable

from django.db import transaction
from django.db.models import Sum

from .eligibility import eligible_entrant_ids
from .models import AuditEvent, CouponCode, Drawing, Pick, PrizeLine

_SYSTEM_RNG = secrets.SystemRandom()
_WON = (Pick.Status.CLAIMED, Pick.Status.DIGITAL)


class DrawingError(Exception):
    """A refused drawing operation; the message is shown to the organizer as is."""


def _ignore_digital_pick(pick: Pick) -> None:
    """Default hand-off for digital picks until the Drip outbox is wired in."""


def _lock(drawing_id: int) -> Drawing:
    return Drawing.objects.select_for_update().get(pk=drawing_id)


def _lock_for_pick(pick: Pick) -> tuple[Drawing, Pick]:
    """Lock the pick's drawing, then re-read the pick so a stale copy can't act."""
    drawing = _lock(pick.prize_line.drawing_id)
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
    """Pick one eligible entrant for `slot`, or audit `exhausted` if nobody is left."""
    pool = eligible_entrant_ids(drawing)
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
        prize_line__drawing=drawing, status__in=_WON
    ).count()
    drawing.save(update_fields=["status", "closed_at", "awarded_count"])


@transaction.atomic
def start_drawing(
    drawing: Drawing,
    *,
    now: datetime.datetime,
    rng: random.Random = _SYSTEM_RNG,
    on_digital_pick: Callable[[Pick], None] = _ignore_digital_pick,
) -> list[Pick]:
    """Open a scheduled drawing and pick a winner for every copy, line by line.

    The cutoff is the earlier of the scheduled time and now, so starting late
    never admits entries made after the announced time. `on_digital_pick` runs
    inside this transaction, so whatever it writes commits or rolls back with
    the pick.
    """
    drawing = _lock(drawing.pk)
    if drawing.status != Drawing.Status.SCHEDULED:
        raise DrawingError("This drawing has already started.")
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
            CouponCode.objects.filter(prize_line=line, pick__isnull=True).order_by("id")
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


def _allow_undo(drawing: Drawing, pick: Pick) -> None:
    """Undo works while open, or to reverse the claim that auto-finished it."""
    if drawing.status == Drawing.Status.OPEN:
        return
    latest = AuditEvent.objects.filter(drawing=drawing).latest("id")
    auto_finished = (
        drawing.status == Drawing.Status.FINISHED
        and latest.kind != AuditEvent.Kind.CLOSED
        and pick.status == Pick.Status.CLAIMED
    )
    if not auto_finished:
        raise DrawingError("This drawing isn't open.")
    if _another_open(drawing):
        raise DrawingError("Another drawing is open. Close it first.")
    drawing.status = Drawing.Status.OPEN
    drawing.closed_at = None
    drawing.awarded_count = 0
    drawing.save(update_fields=["status", "closed_at", "awarded_count"])


@transaction.atomic
def undo_last(pick: Pick, *, now: datetime.datetime) -> Pick:
    """Reverse a mis-tapped Claimed or Not here, recording the reversal.

    Undoing Not here voids its replacement, so long as nobody has marked the
    replacement yet. History is never deleted: the void and the undo are audited.
    """
    drawing, pick = _lock_for_pick(pick)
    _allow_undo(drawing, pick)
    previous = pick.status
    if previous == Pick.Status.NOT_HERE:
        replacement = (
            Pick.objects.filter(replaces=pick).exclude(status=Pick.Status.VOID).first()
        )
        if replacement is not None:
            if replacement.status != Pick.Status.PENDING:
                raise DrawingError("The replacement has already been marked.")
            replacement.status = Pick.Status.VOID
            replacement.marked_at = now
            replacement.save(update_fields=["status", "marked_at"])
            _audit(drawing, AuditEvent.Kind.VOID, now, replacement)
    elif previous != Pick.Status.CLAIMED:
        raise DrawingError("Nothing to undo.")
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
    _set_finished(drawing, now)
    _audit(drawing, AuditEvent.Kind.CLOSED, now, awarded_count=drawing.awarded_count)
