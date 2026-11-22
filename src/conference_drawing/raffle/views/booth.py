"""The owner's booth screens: start a drawing, mark each winner, undo, close.

Phone-first and one-handed. Every state change goes through `draw.py`, which is
the authority on what's allowed; these views only translate its refusals
(`DrawingError`) into messages and re-read the database before rendering,
because the services don't refresh the caller's objects. With HTMX a tap swaps
one copy's rows in place; without JavaScript the same forms POST and redirect.
"""

import datetime
import random
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from ..codes import render_prize_url
from ..draw import (
    DrawingError,
    close_drawing,
    mark_claimed,
    mark_not_here,
    start_drawing,
    undo_last,
)
from ..drip.config import drip_settings
from ..drip.outbox import enqueue_won_prize, failed_count, pending_count, requeue_failed
from ..models import Conference, CouponCode, Drawing, OutboxJob, Pick, PrizeLine
from ..timezones import to_local

_ACTIVE = (Pick.Status.PENDING, Pick.Status.CLAIMED, Pick.Status.DIGITAL)


def _rng() -> random.Random:
    """Production randomness; tests replace this with a seeded `random.Random`."""
    return secrets.SystemRandom()


def _enqueue(pick: Pick, now: datetime.datetime) -> None:
    """Hand a digital pick to the Drip outbox inside the drawing's transaction."""
    enqueue_won_prize(pick, now=now)


@dataclass
class RowView:
    """One pick as shown on screen, with what the owner may do to it."""

    pick: Pick
    can_undo: bool
    code: str = ""
    url: str = ""


@dataclass
class SlotView:
    """One copy of a prize: its picks in draw order, and whether it ran dry."""

    line: PrizeLine
    number: int
    rows: list[RowView] = field(default_factory=list)
    exhausted: bool = False


@dataclass
class LineView:
    """A prize line (one book) and its copies."""

    line: PrizeLine
    slots: list[SlotView]


def _is_htmx(request: HttpRequest) -> bool:
    return request.headers.get("HX-Request") == "true"


def _local_label(drawing: Drawing) -> str:
    local = to_local(drawing.scheduled_at, drawing.conference.time_zone)
    return local.strftime("%a %b %-d, %-I:%M %p")


def _auto_finished_undo_allowed(drawing: Drawing) -> bool:
    """Best-effort mirror of the service's rule for undoing a claim after finish."""
    if drawing.status != Drawing.Status.FINISHED or drawing.closed_by_owner:
        return False
    others = Drawing.objects.filter(conference_id=drawing.conference_id).exclude(
        pk=drawing.pk
    )
    if others.filter(status=Drawing.Status.OPEN).exists():
        return False
    return not (
        drawing.closed_at is not None
        and others.filter(started_at__gte=drawing.closed_at).exists()
    )


def _can_undo(
    pick: Pick, replacement: Pick | None, *, is_open: bool, reopenable: bool
) -> bool:
    if pick.status == Pick.Status.CLAIMED:
        return is_open or reopenable
    if pick.status == Pick.Status.NOT_HERE:
        return is_open and (
            replacement is None or replacement.status == Pick.Status.PENDING
        )
    return False


def _row(
    pick: Pick,
    line: PrizeLine,
    *,
    can_undo: bool,
    codes: dict[int, str],
    show_codes: bool,
) -> RowView:
    row = RowView(pick=pick, can_undo=can_undo)
    if show_codes and pick.status == Pick.Status.DIGITAL:
        shared = line.shared_code if line.code_mode == PrizeLine.CodeMode.SHARED else ""
        row.code = codes.get(pick.pk) or shared
        if row.code and line.url_template:
            row.url = render_prize_url(line.url_template, row.code)
    return row


def _build(drawing: Drawing) -> list[LineView]:
    """Read the drawing's lines, copies and picks fresh from the database."""
    show_codes = drip_settings() is None
    lines = PrizeLine.objects.filter(drawing=drawing).select_related("book")
    picks = list(
        Pick.objects.filter(prize_line__drawing=drawing)
        .exclude(status=Pick.Status.VOID)
        .select_related("entrant")
        .order_by("drawn_at", "id")
    )
    codes = dict(
        CouponCode.objects.filter(pick__prize_line__drawing=drawing).values_list(
            "pick_id", "code"
        )
    )
    replacement_of = {p.replaces_id: p for p in picks if p.replaces_id is not None}
    reopenable = _auto_finished_undo_allowed(drawing)
    is_open = drawing.status == Drawing.Status.OPEN
    started = drawing.status != Drawing.Status.SCHEDULED
    by_slot: dict[tuple[int, int], list[Pick]] = {}
    for pick in picks:
        by_slot.setdefault((pick.prize_line_id, pick.slot), []).append(pick)
    result: list[LineView] = []
    for line in lines.order_by("position", "id"):
        slots: list[SlotView] = []
        for number in range(1, line.quantity + 1):
            slot_picks = by_slot.get((line.pk, number), [])
            slots.append(
                SlotView(
                    line=line,
                    number=number,
                    exhausted=started
                    and not any(p.status in _ACTIVE for p in slot_picks),
                    rows=[
                        _row(
                            p,
                            line,
                            can_undo=_can_undo(
                                p,
                                replacement_of.get(p.pk),
                                is_open=is_open,
                                reopenable=reopenable,
                            ),
                            codes=codes,
                            show_codes=show_codes,
                        )
                        for p in slot_picks
                    ],
                )
            )
        result.append(LineView(line=line, slots=slots))
    return result


def _status_context(drawing: Drawing, error: str = "") -> dict[str, object]:
    pending = Pick.objects.filter(
        prize_line__drawing=drawing,
        prize_line__kind=PrizeLine.Kind.PHYSICAL,
        status=Pick.Status.PENDING,
    ).count()
    return {
        "drawing": drawing,
        "pending": pending,
        "can_close": drawing.status == Drawing.Status.OPEN and pending == 0,
        "error": error,
    }


def _back(drawing_id: int) -> HttpResponseRedirect:
    return HttpResponseRedirect(reverse("booth-drawing", args=[drawing_id]))


@login_required
@require_GET
def home(request: HttpRequest) -> HttpResponse:
    """List conferences, newest first, so the booth is one tap from the drawing."""
    conferences = Conference.objects.order_by("-starts_on", "name")
    return render(
        request, "raffle/booth/conference_list.html", {"conferences": conferences}
    )


@login_required
@require_GET
def conference_page(request: HttpRequest, slug: str) -> HttpResponse:
    """List a conference's drawings with their local times and states."""
    conf = get_object_or_404(Conference, slug=slug)
    drawings = [
        (d, _local_label(d))
        for d in Drawing.objects.filter(conference=conf).select_related("conference")
    ]
    drip_on = drip_settings() is not None
    return render(
        request,
        "raffle/booth/conference.html",
        {
            "conference": conf,
            "drawings": drawings,
            "drip_on": drip_on,
            "drip_pending": pending_count(conf) if drip_on else 0,
            "drip_failed": failed_count(conf) if drip_on else 0,
        },
    )


@login_required
@require_POST
def retry_failed(request: HttpRequest, slug: str) -> HttpResponse:
    """Requeue this conference's failed Drip jobs; 404 when Drip isn't in use."""
    conf = get_object_or_404(Conference, slug=slug)
    if drip_settings() is None:
        raise Http404
    count = requeue_failed(
        OutboxJob.objects.filter(conference=conf), now=timezone.now()
    )
    messages.success(request, f"Requeued {count} failed Drip job(s).")
    return HttpResponseRedirect(reverse("booth-conference", args=[conf.slug]))


@login_required
@require_GET
def drawing_page(request: HttpRequest, pk: int) -> HttpResponse:
    """Show one drawing: Start when scheduled, copies and buttons while open."""
    obj = get_object_or_404(Drawing.objects.select_related("conference"), pk=pk)
    context = {**_status_context(obj), "when": _local_label(obj), "lines": _build(obj)}
    return render(request, "raffle/booth/drawing.html", context)


def _redirect(request: HttpRequest, drawing_id: int) -> HttpResponse:
    """Whole-page actions: HTMX is told to navigate; plain forms get a redirect."""
    if _is_htmx(request):
        response = HttpResponse()
        response["HX-Redirect"] = reverse("booth-drawing", args=[drawing_id])
        return response
    return _back(drawing_id)


@login_required
@require_POST
def start(request: HttpRequest, pk: int) -> HttpResponse:
    """Run the drawing; refusals come back as a message on the drawing page."""
    obj = get_object_or_404(Drawing, pk=pk)
    now = timezone.now()
    try:
        start_drawing(
            obj,
            now=now,
            rng=_rng(),
            on_digital_pick=lambda pick: _enqueue(pick, now),
        )
    except DrawingError as exc:
        messages.error(request, str(exc))
    return _redirect(request, obj.pk)


@login_required
@require_POST
def close(request: HttpRequest, pk: int) -> HttpResponse:
    """Close an open drawing, leaving unclaimed copies unawarded."""
    obj = get_object_or_404(Drawing, pk=pk)
    try:
        close_drawing(obj, now=timezone.now())
    except DrawingError as exc:
        messages.error(request, str(exc))
    return _redirect(request, obj.pk)


def _act(
    request: HttpRequest, pk: int, operation: Callable[[Pick], object]
) -> HttpResponse:
    """Run one pick operation, then show the result.

    HTMX gets the copy's rows plus the status block (out of band); a plain form
    gets a redirect with any refusal as a message.
    """
    pick = get_object_or_404(Pick.objects.select_related("prize_line"), pk=pk)
    drawing_id = pick.prize_line.drawing_id
    error = ""
    try:
        operation(pick)
    except DrawingError as exc:
        error = str(exc)
    if not _is_htmx(request):
        if error:
            messages.error(request, error)
        return _back(drawing_id)
    obj = Drawing.objects.select_related("conference").get(pk=drawing_id)
    slot = next(
        s
        for lv in _build(obj)
        for s in lv.slots
        if lv.line.pk == pick.prize_line_id and s.number == pick.slot
    )
    body = render_to_string(
        "raffle/booth/_pick_row.html", {"slot": slot, "drawing": obj}, request
    ) + render_to_string(
        "raffle/booth/_drawing_status.html",
        {**_status_context(obj, error), "oob": True},
        request,
    )
    return HttpResponse(body)


@login_required
@require_POST
def claim(request: HttpRequest, pk: int) -> HttpResponse:
    """The winner came to the booth and took the book."""
    return _act(request, pk, lambda p: mark_claimed(p, now=timezone.now()))


@login_required
@require_POST
def not_here(request: HttpRequest, pk: int) -> HttpResponse:
    """The winner is absent: pass over them and draw a replacement at once."""
    return _act(request, pk, lambda p: mark_not_here(p, now=timezone.now(), rng=_rng()))


@login_required
@require_POST
def undo(request: HttpRequest, pk: int) -> HttpResponse:
    """Reverse a mis-tapped Claimed or Not here."""
    return _act(request, pk, lambda p: undo_last(p, now=timezone.now()))
