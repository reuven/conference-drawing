"""The booth monitor: next drawing and entry count, then winners as they happen.

Read-only and unauthenticated by design: the monitor can't sign in, so access is
a long random token in the URL. The page polls a small partial every 2 seconds.
Names are shown as entered (the owner reads them aloud anyway); emails, codes
and prize URLs never appear here, and nothing here is logged.
"""

import datetime
import secrets

from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.http import require_GET

from ..models import Conference, Drawing, Entrant, Pick, PrizeLine
from ..timezones import to_local

RECENT = datetime.timedelta(minutes=30)
_SHOWN = (Pick.Status.PENDING, Pick.Status.CLAIMED, Pick.Status.DIGITAL)


def _now() -> datetime.datetime:
    """The clock; tests patch this so nothing mixes a fixed time with the real one."""
    return timezone.now()


def _conference_for(token: str) -> Conference:
    """Match the token in constant time per comparison; no match is a plain 404."""
    found: Conference | None = None
    for conf in Conference.objects.all():
        if secrets.compare_digest(conf.display_token.encode(), token.encode()):
            found = conf
    if found is None:
        raise Http404
    return found


def _winners(drawing: Drawing) -> list[dict[str, object]]:
    picks = (
        Pick.objects.filter(prize_line__drawing=drawing, status__in=_SHOWN)
        .select_related("entrant")
        .order_by("drawn_at", "id")
    )
    names: dict[int, list[str]] = {}
    for pick in picks:
        names.setdefault(pick.prize_line_id, []).append(pick.entrant.name)
    lines = PrizeLine.objects.filter(drawing=drawing).select_related("book")
    return [
        {"title": line.book.title, "names": names.get(line.pk, [])}
        for line in lines.order_by("position", "id")
    ]


def _context(conf: Conference, token: str) -> dict[str, object]:
    now = _now()
    drawings = Drawing.objects.filter(conference=conf)
    shown = drawings.filter(status=Drawing.Status.OPEN).order_by("-started_at").first()
    is_open = shown is not None
    if shown is None:
        shown = (
            drawings.filter(status=Drawing.Status.FINISHED, closed_at__gte=now - RECENT)
            .order_by("-closed_at")
            .first()
        )
    context: dict[str, object] = {"conference": conf, "token": token}
    if shown is not None:
        return {
            **context,
            "mode": "winners",
            "is_open": is_open,
            "lines": _winners(shown),
        }
    upcoming = (
        drawings.filter(status=Drawing.Status.SCHEDULED)
        .order_by("scheduled_at")
        .first()
    )
    label = ""
    if upcoming is not None:
        local = to_local(upcoming.scheduled_at, conf.time_zone)
        label = local.strftime("%a %-I:%M %p")
    return {
        **context,
        "mode": "idle",
        "next_label": label,
        "entry_count": Entrant.objects.filter(conference=conf).count(),
    }


@require_GET
def display(request: HttpRequest, token: str) -> HttpResponse:
    """Full-screen page that then keeps itself current via the panel."""
    conf = _conference_for(token)
    return render(request, "raffle/display/display.html", _context(conf, token))


@require_GET
def panel(request: HttpRequest, token: str) -> HttpResponse:
    """The swappable part: winners by book, or next drawing and entry count."""
    conf = _conference_for(token)
    response = render(request, "raffle/display/_panel.html", _context(conf, token))
    response["Cache-Control"] = "private, no-store"
    response["X-Robots-Tag"] = "noindex, nofollow"
    return response
