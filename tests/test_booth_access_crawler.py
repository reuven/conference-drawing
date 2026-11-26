"""Route crawler: request every /booth/ URL as every kind of user.

This is the main guard of the access rule. It walks the real URL table, so a
new booth route that isn't listed in ROUTES below fails loudly instead of
shipping unchecked. Each request runs inside a rolled-back savepoint, so the
state-changing POSTs can't affect one another and the result never depends on
request order.
"""

import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

import pytest
from django.contrib.auth.models import User
from django.db import transaction
from django.test import Client
from django.urls import URLPattern, URLResolver, get_resolver

from conference_drawing.raffle.models import Conference, Drawing, Pick

HELPER = "helper"
OWNER = "owner"
# Route name -> who may reach it (for a conference they can see). Every booth
# route must appear here; the sign-in routes are public (ruling R1).
ROUTES: dict[str, str] = {
    "booth-home": HELPER,
    "booth-conference": HELPER,
    "booth-flyer": HELPER,
    "booth-drawing": HELPER,
    "booth-start": HELPER,
    "booth-close": HELPER,
    "booth-claim": HELPER,
    "booth-not-here": HELPER,
    "booth-undo": HELPER,
    "booth-export": OWNER,
    "booth-wrapup": OWNER,
    "booth-delete-entrants": OWNER,
    "booth-retry-failed": OWNER,
}
PUBLIC_ROUTES = {"booth-login", "booth-logout"}
METHODS = ("get", "head", "post")
PARAM = re.compile(r"<(?:\w+:)?(\w+)>")


@dataclass(frozen=True)
class Seed:
    """The ids one conference contributes to URL building."""

    slug: str
    drawing: int
    pick: int
    name: str


@dataclass(frozen=True)
class Target:
    """One concrete URL for a route, and which conference it belongs to."""

    route: str
    url: str
    conference: str  # "A", "B", or "-" for URLs that name no conference


def _booth_routes(
    patterns: list[Any], prefix: str = ""
) -> Iterator[tuple[str, str | None]]:
    """Yield (route text, name) for every URL pattern, flattening include()s."""
    for p in patterns:
        route = prefix + str(p.pattern)
        if isinstance(p, URLResolver):
            yield from _booth_routes(p.url_patterns, route)
        elif isinstance(p, URLPattern):
            yield route, p.name


def _fill(route: str, seed: Seed) -> str:
    """Substitute a conference's slug / drawing pk / pick pk into a route."""
    values = {"slug": seed.slug}
    if route.startswith("booth/drawing/"):
        values["pk"] = str(seed.drawing)
    elif route.startswith("booth/pick/"):
        values["pk"] = str(seed.pick)

    def sub(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in values:
            raise AssertionError(f"crawler can't fill <{name}> in {route!r}")
        return values[name]

    return "/" + PARAM.sub(sub, route)


def _targets(seeds: dict[str, Seed]) -> list[Target]:
    """Build every booth URL, failing on any route the crawler doesn't know."""
    out: list[Target] = []
    for route, name in _booth_routes(get_resolver().url_patterns):
        if not route.startswith("booth/") or name in PUBLIC_ROUTES:
            continue
        assert name in ROUTES, f"unknown booth route {name!r} ({route}): add it above"
        if PARAM.search(route):
            out += [Target(name, _fill(route, s), k) for k, s in seeds.items()]
        else:
            out.append(Target(name, "/" + route, "-"))
    missing = set(ROUTES) - {t.route for t in out}
    assert not missing, f"ROUTES lists routes that no longer exist: {missing}"
    return out


class _Rollback(Exception):
    """Raised to undo one request's database changes."""


def _request(client: Client, method: str, url: str) -> tuple[int, str]:
    """Send one request and roll its writes back; return (status, body)."""
    try:
        with transaction.atomic():
            response = getattr(client, method)(url)
            result = (response.status_code, response.content.decode(errors="ignore"))
            raise _Rollback
    except _Rollback:
        return result


@pytest.fixture
def seeds(
    conference_factory: Callable[..., Conference],
    drawing_factory: Callable[..., Drawing],
    prize_line_factory: Callable[..., Any],
    entrant_factory: Callable[..., Any],
    pick_factory: Callable[..., Pick],
) -> dict[str, Seed]:
    out = {}
    for key, slug in (("A", "alpha-conf"), ("B", "beta-conf")):
        conf = conference_factory(slug=slug, name=f"Conf {key} Name")
        drawing = drawing_factory(conf)
        pick = pick_factory(prize_line_factory(drawing), entrant_factory(conf))
        out[key] = Seed(slug, drawing.pk, pick.pk, conf.name)
    return out


def _login(username: str, **fields: Any) -> tuple[User, Client]:
    user = User.objects.create_user(username, password="pw", **fields)
    client = Client()
    client.force_login(user)
    return user, client


def test_every_booth_url_for_every_role(seeds: dict[str, Seed], settings: Any) -> None:
    # Drip must be on, or retry-failed 404s for the owner too and hides the rule.
    settings.DRIP_ACCOUNT_ID = "1234567"
    settings.DRIP_API_KEY = "fake-key"
    _, owner = _login("owner", is_superuser=True)
    helper_user, helper_a = _login("helper-a")
    _, nobody = _login("nobody")
    _, staff = _login("staff", is_staff=True)
    inactive_user, inactive = _login("inactive-helper", is_active=False)
    Conference.objects.get(slug=seeds["A"].slug).helpers.add(helper_user, inactive_user)
    users: dict[str, Client] = {
        "owner": owner,
        "helper-a": helper_a,
        "nobody": nobody,
        "staff": staff,
        "inactive": inactive,
        "anonymous": Client(),
    }
    targets = _targets(seeds)
    failures: list[str] = []

    for who, client in users.items():
        for t in targets:
            for method in METHODS:
                status, body = _request(client, method, t.url)
                if who == "owner":
                    ok = status != 404
                elif who in ("anonymous", "inactive"):
                    # An inactive account's session is dropped: same as anonymous.
                    # Task 4 tightens this to the booth login URL.
                    ok = status == 302
                elif who == "helper-a" and t.conference in ("A", "-"):
                    if ROUTES[t.route] == HELPER:
                        ok = status != 404
                    else:
                        ok = status in (404, 405)
                elif t.route == "booth-home":
                    # Reached, but an empty list: neither conference is named.
                    ok = status in (200, 405) and not any(
                        s.name in body for s in seeds.values()
                    )
                else:
                    ok = status in (404, 405)
                if not ok:
                    failures.append(
                        f"{who} {method.upper()} {t.route} {t.url}: {status}"
                    )

    assert not failures, "\n".join(failures)


def test_crawler_rejects_unknown_booth_route(monkeypatch: pytest.MonkeyPatch) -> None:
    """A booth route missing from ROUTES must fail the crawl by name."""
    monkeypatch.delitem(ROUTES, "booth-undo")
    seed = Seed("s", 1, 1, "n")
    with pytest.raises(AssertionError, match="booth-undo"):
        _targets({"A": seed, "B": seed})
