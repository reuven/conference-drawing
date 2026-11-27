"""A cap on how many entries one network may create per hour.

The per-address flood guard only protects the web workers; this is what stops
one network from stuffing the drawing with made-up people. A "network" is the
view's limiter key (an IPv4 address or an IPv6 /64), so a venue's Wi-Fi counts
as one. Counters live in Django's cache in fixed clock-hour buckets (UTC), so
a count simply expires; nothing is stored in the database or logged, and the
key carries a hash of the network, never the address.
"""

import datetime

from django.core.cache import cache

from ..keyed_hash import keyed_digest
from .models import Conference

# A bucket is one clock hour; outlive it a little so a slow clock can't cut it.
_BUCKET_SECONDS = 3700


def cache_key(conference: Conference, key: str, now: datetime.datetime) -> str:
    """Return the cache key for `key`'s counter in the hour containing `now`."""
    digest = keyed_digest(key)
    hour = now.astimezone(datetime.UTC).strftime("%Y%m%d%H")
    return f"entrycap:{conference.pk}:{digest}:{hour}"


def network_entries_allowed(
    conference: Conference, key: str, *, now: datetime.datetime
) -> bool:
    """Say whether `key`'s network may create another entry this hour.

    A cap of 0 means no limit, and the cache is not touched. The check and
    the later `record_network_entry` are separate steps, so the cap may
    overshoot by at most the number of concurrent requests; acceptable for a
    fairness cap.
    """
    cap = conference.entries_per_network_per_hour
    if cap == 0:
        return True
    return int(cache.get(cache_key(conference, key, now), 0)) < cap


def record_network_entry(
    conference: Conference, key: str, *, now: datetime.datetime
) -> None:
    """Count one created entry against `key`'s network for this hour.

    Call only after the entrant row exists, so invalid posts, repeats and
    honeypot posts never count. The count is `cache.add` then `cache.incr`; the
    cap may overshoot by at most the number of concurrent requests (see
    `network_entries_allowed`); acceptable for a fairness cap.
    """
    if conference.entries_per_network_per_hour == 0:
        return
    name = cache_key(conference, key, now)
    cache.add(name, 0, _BUCKET_SECONDS)
    try:
        cache.incr(name)
        # incr rewrites the row with the backend's default timeout (300 s),
        # which would let the count lapse mid-hour; restore the full bucket.
        cache.touch(name, _BUCKET_SECONDS)
    except ValueError:
        # The counter expired between add and incr; start it at this entry.
        cache.add(name, 1, _BUCKET_SECONDS)
