"""One keyed hash for every cache key that stands for a person or a network."""

import hashlib
import hmac

from django.conf import settings


def keyed_digest(value: str) -> str:
    """Return HMAC-SHA256(SECRET_KEY, value) as hex.

    Why: cache keys for rate limits, entry caps and sign-in lockouts must not
    hold an address or account name, and an unkeyed hash of an IPv4 address
    can be brute-forced from a dump of the cache. Keying with the secret
    makes the stored digest useless without it. Callers prefix `value` with
    a purpose so different counters never share a digest.
    """
    return hmac.new(
        settings.SECRET_KEY.encode(), value.encode(), hashlib.sha256
    ).hexdigest()
