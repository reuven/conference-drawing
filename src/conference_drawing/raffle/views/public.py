"""Public, unauthenticated pages: the entry form, thanks, closed and privacy."""

import ipaddress
import logging
from collections.abc import Callable

from django.http import (
    HttpRequest,
    HttpResponse,
    HttpResponseBase,
    HttpResponseRedirect,
)
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods
from django_ratelimit.decorators import ratelimit
from django_ratelimit.exceptions import Ratelimited

from ..forms import EntryForm
from ..models import Book, Conference, Drawing, InstallSettings, PrizeLine
from ..timezones import to_local

logger = logging.getLogger(__name__)


def _canonical_ip(raw: str) -> str | None:
    """Return a rate-limit key for `raw`, or None if it is not an IP address.

    IPv6 clients get a whole /64 (one household or one SLAAC subnet can mint
    billions of addresses, so per-address buckets would be no limit at all).
    """
    try:
        addr = ipaddress.ip_address(raw.strip())
    except ValueError:
        return None
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.ipv4_mapped is not None:
            return str(addr.ipv4_mapped)
        return str(ipaddress.ip_network(f"{addr}/64", strict=False))
    return str(addr)


def client_ip(group: str, request: HttpRequest) -> str:
    """Rate-limit key: the real client address as seen by our reverse proxy.

    The app listens only on a unix socket behind Caddy, which appends the
    connecting peer to X-Forwarded-For. So the rightmost entry is the one we
    can trust; anything to its left was supplied by the client and could be
    forged to get a fresh bucket per request. Unparseable values fall back to
    REMOTE_ADDR, and a request with nothing usable shares the "unknown" bucket.
    """
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    candidates = [forwarded.rsplit(",", 1)[-1], request.META.get("REMOTE_ADDR", "")]
    for candidate in candidates:
        key = _canonical_ip(candidate)
        if key is not None:
            return key
    return "unknown"


def rate_limited(request: HttpRequest, exception: Ratelimited) -> HttpResponse:
    """Answer an over-limit POST with a friendly 429 (set as RATELIMIT_VIEW)."""
    return render(request, "raffle/rate_limited.html", status=429)


def _list_join(parts: list[str]) -> str:
    if len(parts) <= 1:
        return "".join(parts)
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def _schedule_context(conference: Conference) -> dict[str, object]:
    """Drawing times (in the conference's zone) and books for the entry/thanks pages."""
    drawings = Drawing.objects.filter(
        conference=conference,
        status__in=[Drawing.Status.SCHEDULED, Drawing.Status.OPEN],
    )
    times = []
    for scheduled_at in drawings.values_list("scheduled_at", flat=True):
        local = to_local(scheduled_at, conference.time_zone)
        times.append(local.strftime("%a %I:%M %p").replace(" 0", " "))
    book_ids = (
        PrizeLine.objects.filter(drawing__in=drawings)
        .order_by("drawing__scheduled_at", "drawing_id", "position", "id")
        .values_list("book_id", flat=True)
    )
    ordered_ids = list(dict.fromkeys(book_ids))  # each book once, first seen first
    books_by_id = Book.objects.in_bulk(ordered_ids)
    return {
        "conference": conference,
        "drawing_times": _list_join(times),
        "books": [books_by_id[i] for i in ordered_ids],
    }


# django-ratelimit is untyped; give the decorator a real signature so mypy
# --strict still checks the view it wraps.
_limit_posts: Callable[
    [Callable[..., HttpResponseBase]], Callable[..., HttpResponseBase]
]
_limit_posts = ratelimit(key=client_ip, rate="10/m", method="POST", block=True)


@require_http_methods(["GET", "HEAD", "POST"])
@_limit_posts
def entry(request: HttpRequest, slug: str) -> HttpResponseBase:
    """Show the entry form, or process a submission.

    Every accepted-looking submission -- new entry, repeat email or honeypot --
    redirects to the same thanks page, so nothing here reveals whether an
    address has already entered.

    The limiter's DatabaseCache increments are not atomic across gunicorn
    workers, so the limit is approximate. That looseness is accepted: it still
    stops a script from hammering the form.
    """
    conference = get_object_or_404(Conference, slug=slug)
    if conference.wrapped_up_at is not None:
        return render(request, "raffle/closed.html", {"conference": conference})

    if request.method == "POST":
        form = EntryForm(conference, request.POST)
        thanks_redirect = HttpResponseRedirect(reverse("thanks", args=[slug]))
        thanks_redirect.status_code = 303
        if form.honeypot_filled():
            return thanks_redirect
        if form.is_valid():
            entrant = form.save(timezone.now())
            if entrant is not None:
                logger.info(
                    "entry created conference=%s entrant=%s", conference.pk, entrant.pk
                )
            return thanks_redirect
    else:
        form = EntryForm(conference)
    return render(
        request,
        "raffle/entry.html",
        {**_schedule_context(conference), "form": form},
    )


@require_GET
def thanks(request: HttpRequest, slug: str) -> HttpResponse:
    """Confirm the entry (identical for new and repeat entries)."""
    conference = get_object_or_404(Conference, slug=slug)
    return render(request, "raffle/thanks.html", _schedule_context(conference))


@require_GET
def privacy(request: HttpRequest) -> HttpResponse:
    """Show the install's privacy note as escaped text."""
    return render(request, "raffle/privacy.html", {"install": InstallSettings.get()})
