"""The per-network cap on created entries (fixed one-hour buckets in the cache)."""

import datetime
import hashlib
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest
from django.core.cache import cache
from django.core.cache.backends import db as db_cache
from django.db import connection
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from conference_drawing.raffle import entry_cap
from conference_drawing.raffle.entry_cap import (
    cache_key,
    network_entries_allowed,
    record_network_entry,
)
from conference_drawing.raffle.models import Conference, Entrant

NOW = datetime.datetime(2027, 5, 11, 15, 30, tzinfo=datetime.UTC)
NET = "203.0.113.5"


@pytest.fixture
def conference(conference_factory: Callable[..., Conference]) -> Conference:
    return conference_factory(slug="pycon", entries_per_network_per_hour=3)


def test_allows_until_cap_then_refuses(conference: Conference) -> None:
    for _ in range(3):
        assert network_entries_allowed(conference, NET, now=NOW)
        record_network_entry(conference, NET, now=NOW)
    assert not network_entries_allowed(conference, NET, now=NOW)


def test_networks_and_conferences_are_counted_apart(
    conference: Conference, conference_factory: Callable[..., Conference]
) -> None:
    other = conference_factory(slug="other", entries_per_network_per_hour=3)
    for _ in range(3):
        record_network_entry(conference, NET, now=NOW)
    assert network_entries_allowed(conference, "203.0.113.6", now=NOW)
    assert network_entries_allowed(other, NET, now=NOW)


def test_next_hour_starts_a_fresh_count(conference: Conference) -> None:
    for _ in range(3):
        record_network_entry(conference, NET, now=NOW)
    later = NOW + datetime.timedelta(hours=1)
    assert network_entries_allowed(conference, NET, now=later)


def test_counter_outlives_the_cache_default_timeout(
    conference: Conference, monkeypatch: pytest.MonkeyPatch
) -> None:
    """incr rewrites the row with the backend's 300 s default unless we touch it."""
    for _ in range(3):
        record_network_entry(conference, NET, now=NOW)
    later = timezone.now() + datetime.timedelta(seconds=600)
    monkeypatch.setattr(db_cache, "tz_now", lambda: later)
    assert not network_entries_allowed(conference, NET, now=NOW)


def test_cache_key_is_keyed_to_the_secret_key(
    conference: Conference, settings: Any
) -> None:
    plain = hashlib.sha256(NET.encode()).hexdigest()
    assert plain not in cache_key(conference, NET, NOW)
    before = cache_key(conference, NET, NOW)
    settings.SECRET_KEY = "another-secret-key"  # noqa: S105
    assert cache_key(conference, NET, NOW) != before


def test_hour_bucket_is_utc_whatever_the_zone_of_now(conference: Conference) -> None:
    la = datetime.timezone(datetime.timedelta(hours=-7))
    assert cache_key(conference, NET, NOW) == cache_key(
        conference, NET, NOW.astimezone(la)
    )


def test_zero_means_no_cap_and_never_touches_the_cache(
    conference: Conference, monkeypatch: pytest.MonkeyPatch
) -> None:
    conference.entries_per_network_per_hour = 0

    def boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("cache touched")

    monkeypatch.setattr(cache, "get", boom)
    monkeypatch.setattr(cache, "add", boom)
    monkeypatch.setattr(cache, "incr", boom)
    assert network_entries_allowed(conference, NET, now=NOW)
    record_network_entry(conference, NET, now=NOW)


def test_cache_key_hides_the_address(conference: Conference) -> None:
    key = cache_key(conference, "2001:db8:1:2::/64", NOW)
    assert "2001" not in key
    assert "db8" not in key
    assert key.startswith(f"entrycap:{conference.pk}:")


def test_record_survives_the_counter_expiring_between_add_and_incr(
    conference: Conference, monkeypatch: pytest.MonkeyPatch
) -> None:
    def expired(name: str) -> int:
        cache.delete(name)
        raise ValueError(f"Key '{name}' not found")

    flaky = SimpleNamespace(get=cache.get, add=cache.add, incr=expired)
    monkeypatch.setattr(entry_cap, "cache", flaky)
    record_network_entry(conference, NET, now=NOW)
    assert cache.get(cache_key(conference, NET, NOW)) == 1


# --- through the entry view ------------------------------------------------


def _enter(client: Client, conference: Conference, n: int, addr: str = NET) -> int:
    data = {
        "name": f"Person {n}",
        "email": f"person{n}@example.com",
        "followup_ack": "on",
    }
    got = client.post(reverse("entry", args=[conference.slug]), data, REMOTE_ADDR=addr)
    return got.status_code


@pytest.mark.usefixtures("frozen_ratelimit_clock")
def test_over_the_cap_gets_the_cap_page_and_no_row(
    client: Client, conference: Conference
) -> None:
    assert [_enter(client, conference, n) for n in range(3)] == [303, 303, 303]
    blocked = client.post(
        reverse("entry", args=[conference.slug]),
        {"name": "P", "email": "p4@example.com", "followup_ack": "on"},
        REMOTE_ADDR=NET,
    )
    body = blocked.content.decode().lower()
    assert blocked.status_code == 429
    assert "too many people" in body
    assert "booth" in body
    assert Entrant.objects.count() == 3
    assert blocked["Cache-Control"] == "private, no-store"


@pytest.mark.usefixtures("frozen_ratelimit_clock")
def test_only_created_entries_count(client: Client, conference: Conference) -> None:
    url = reverse("entry", args=[conference.slug])
    good = {"name": "Ada", "email": "ada@example.com", "followup_ack": "on"}
    for _ in range(5):
        assert client.post(url, good, REMOTE_ADDR=NET).status_code == 303
        assert (
            client.post(url, {**good, "leave_blank": "x"}, REMOTE_ADDR=NET).status_code
            == 303
        )
        assert client.post(url, {}, REMOTE_ADDR=NET).status_code == 200
    assert Entrant.objects.count() == 1
    assert [_enter(client, conference, n) for n in range(10, 12)] == [303, 303]
    assert _enter(client, conference, 99) == 429


@pytest.mark.usefixtures("frozen_ratelimit_clock")
def test_other_networks_are_unaffected(client: Client, conference: Conference) -> None:
    for n in range(3):
        _enter(client, conference, n)
    assert _enter(client, conference, 50) == 429
    assert _enter(client, conference, 51, addr="203.0.113.6") == 303
    # Same IPv6 /64 shares a bucket; a different /64 does not.
    for n in range(3):
        _enter(client, conference, 60 + n, addr=f"2001:db8:1:2::{n + 1}")
    assert _enter(client, conference, 70, addr="2001:db8:1:2:ffff::1") == 429
    assert _enter(client, conference, 71, addr="2001:db8:1:3::1") == 303


@pytest.mark.usefixtures("frozen_ratelimit_clock")
def test_cap_zero_is_unlimited(client: Client, conference: Conference) -> None:
    conference.entries_per_network_per_hour = 0
    conference.save()
    assert [_enter(client, conference, n) for n in range(8)] == [303] * 8


@pytest.mark.usefixtures("frozen_ratelimit_clock")
def test_no_raw_address_in_cache_or_logs(
    client: Client, conference: Conference, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("DEBUG")
    _enter(client, conference, 1)
    with connection.cursor() as cur:
        cur.execute(
            "SELECT cache_key FROM cd_cache WHERE cache_key LIKE %s", ["%entrycap%"]
        )
        keys = [row[0] for row in cur.fetchall()]
    assert keys
    assert all(NET not in key for key in keys)
    assert NET not in caplog.text
