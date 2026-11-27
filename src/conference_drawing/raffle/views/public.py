"""Public, unauthenticated pages: the entry form, thanks, closed and privacy."""

import ipaddress
import logging
from collections.abc import Callable
from zoneinfo import ZoneInfo

from django.conf import settings
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

from ..after import resolve_after
from ..entry_cap import network_entries_allowed, record_network_entry
from ..forms import EntriesClosed, EntryForm
from ..models import Conference, InstallSettings
from ..schedule import schedule_context

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

    The app listens on a unix socket behind Caddy, which appends the connecting
    peer to X-Forwarded-For, so the rightmost entry is the one we can trust;
    anything to its left was supplied by the client. The header is honored only
    when the request has no peer address (the unix socket) or its peer is in
    ``settings.TRUSTED_PROXIES``; from anyone else the header is forged input
    and REMOTE_ADDR is the key. Unparseable values fall back to REMOTE_ADDR,
    and a request with nothing usable shares the "unknown" bucket.

    Trust assumption: exactly one trusted proxy (Caddy) sits in front, and it
    drops any client-sent X-Forwarded-For (deploy/caddy/conference-drawing.caddy.tmpl).
    If a CDN or second proxy is ever added in front of Caddy, the rightmost
    entry becomes that proxy's address and this must take the entry before it.
    """
    remote = request.META.get("REMOTE_ADDR", "")
    candidates = [remote]
    if _peer_is_proxy(remote):
        forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
        candidates.insert(0, forwarded.rsplit(",", 1)[-1])
    for candidate in candidates:
        key = _canonical_ip(candidate)
        if key is not None:
            return key
    return "unknown"


def _peer_is_proxy(remote: str) -> bool:
    """True for the unix socket (no peer address) or a listed trusted proxy."""
    if not remote:
        return True
    try:
        return str(ipaddress.ip_address(remote.strip())) in settings.TRUSTED_PROXIES
    except ValueError:
        return False


def rate_limited(request: HttpRequest, exception: Ratelimited) -> HttpResponse:
    """Answer an over-limit POST with a friendly 429 (set as RATELIMIT_VIEW)."""
    return render(request, "raffle/rate_limited.html", status=429)


# django-ratelimit is untyped; give the decorator a real signature so mypy
# --strict still checks the view it wraps.
_limit_posts: Callable[
    [Callable[..., HttpResponseBase]], Callable[..., HttpResponseBase]
]
_limit_posts = ratelimit(key=client_ip, rate="300/m", method="POST", block=True)


@require_http_methods(["GET", "HEAD", "POST"])
@_limit_posts
def entry(request: HttpRequest, slug: str) -> HttpResponseBase:
    """Show the entry form, or process a submission.

    Entries are accepted only on the conference's dates (today in its time
    zone); before them a "not open yet" page shows, after them the closed page.
    Within the dates, every accepted-looking submission -- new entry, repeat
    email or honeypot -- redirects to the same thanks page, so nothing here
    reveals whether an address has already entered. The exception is a valid
    post from a network that has used up its hourly cap: it gets a 429 page,
    the same for everyone on that network, whether or not the address is new.

    The limiter's DatabaseCache increments are not atomic across gunicorn
    workers, so the limit is approximate. That looseness is accepted: it still
    stops a script from hammering the form.
    """
    conference = get_object_or_404(Conference, slug=slug)
    if conference.wrapped_up_at is not None:
        install = InstallSettings.get()
        after = resolve_after(install, conference)
        if after.redirect_url is not None and request.method in ("GET", "HEAD"):
            return HttpResponseRedirect(after.redirect_url)
        return render(
            request,
            "raffle/closed.html",
            {
                "conference": conference,
                "after": after,
                "install": install,
            },
        )

    today = timezone.localdate(timezone=ZoneInfo(conference.time_zone))
    window = conference.entry_window(today)
    if window == "after":
        return render(request, "raffle/closed.html", {"conference": conference})
    if window == "before":
        return render(
            request,
            "raffle/not_open.html",
            {**schedule_context(conference), "opens_on": conference.starts_on},
        )

    if request.method == "POST":
        form = EntryForm(conference, request.POST)
        thanks_redirect = HttpResponseRedirect(reverse("thanks", args=[slug]))
        thanks_redirect.status_code = 303
        if form.honeypot_filled():
            return thanks_redirect
        if form.is_valid():
            now = timezone.now()
            network = client_ip("entry", request)
            if not network_entries_allowed(conference, network, now=now):
                return render(
                    request,
                    "raffle/network_cap.html",
                    {"conference": conference},
                    status=429,
                )
            try:
                entrant = form.save(now)
            except EntriesClosed:
                return render(request, "raffle/closed.html", {"conference": conference})
            if entrant is not None:
                record_network_entry(conference, network, now=now)
                logger.info(
                    "entry created conference=%s entrant=%s", conference.pk, entrant.pk
                )
            return thanks_redirect
    else:
        form = EntryForm(conference)
    return render(
        request,
        "raffle/entry.html",
        {**schedule_context(conference), "form": form},
    )


@require_GET
def thanks(request: HttpRequest, slug: str) -> HttpResponse:
    """Confirm the entry (identical for new and repeat entries)."""
    conference = get_object_or_404(Conference, slug=slug)
    return render(request, "raffle/thanks.html", schedule_context(conference))


@require_http_methods(["GET", "HEAD"])
def home(request: HttpRequest) -> HttpResponseBase:
    """Show the site-wide after-conference page at `/`, or redirect.

    Old flyers and a bare domain land here. With nothing configured, the
    template shows a short built-in page instead of a bare 404, so a visitor
    still learns what the site is for.
    """
    install = InstallSettings.get()
    after = resolve_after(install, None)
    if after.redirect_url is not None:
        return HttpResponseRedirect(after.redirect_url)
    return render(request, "raffle/home.html", {"after": after, "install": install})


@require_GET
def privacy(request: HttpRequest) -> HttpResponse:
    """Show the install's privacy note as escaped text."""
    return render(request, "raffle/privacy.html", {"install": InstallSettings.get()})
