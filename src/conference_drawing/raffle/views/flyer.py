"""The printable booth flyer: QR code to the entry page, books, schedule, rule."""

import segno
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.http import require_GET

from ..access import conference_or_404
from ..schedule import MUST_BE_PRESENT, books_in, days, upcoming_drawings


@login_required
@require_GET
def flyer(request: HttpRequest, slug: str) -> HttpResponse:
    """Render a one-page flyer to print or save as PDF and tape to the booth.

    The QR encodes the absolute entry URL built from the request host (which
    ALLOWED_HOSTS has already vetted), at error-correction level M so a
    slightly creased or low-quality printout still scans. The SVG carries no
    style attributes, so the page's CSP needs no exception, and it needs no JS.
    """
    conference = conference_or_404(request.user, slug)
    entry_url = request.build_absolute_uri(reverse("entry", args=[slug]))
    qr = segno.make(entry_url, error="m").svg_inline(scale=10)
    return render(
        request,
        "raffle/booth/flyer.html",
        {
            "conference": conference,
            "qr_svg": qr,
            "short_url": entry_url.split("://", 1)[-1],
            "books": books_in(upcoming_drawings(conference)),
            "days": days(conference),
            "must_be_present": MUST_BE_PRESENT,
        },
    )
