"""Shared test harness: environment, Hypothesis profiles, DB guards."""

import os
import types
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any

import django_ratelimit.core
import pytest
from django.apps import apps
from django.conf import settings as django_settings
from django.core.management import call_command
from hypothesis import HealthCheck, settings

from conference_drawing.raffle.models import (
    Book,
    Conference,
    Drawing,
    Entrant,
    Pick,
    PrizeLine,
)
from tests import factories

# Every profile is built on "default": without a parent, register_profile copies
# whatever profile is loaded at the time, and mutmut imports this file twice in
# one process. Hypothesis's built-in "ci" resolves the same way, so it can't be
# the parent either. "ci" replaces that built-in profile, so it restates its
# settings (derandomize, no example database, print_blob).
_DEFAULT = settings.get_profile("default")
settings.register_profile(
    "dev",
    parent=_DEFAULT,
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.register_profile(
    "ci",
    parent=_DEFAULT,
    max_examples=200,
    deadline=None,
    derandomize=True,
    database=None,
    print_blob=True,
)
# mutmut runs pytest more than once in one process (stats, then the clean run),
# so a hypothesis TestCase method sees a second executor instance. That is an
# artifact of the runner, not a flaky test.
settings.register_profile(
    "mutmut",
    parent=_DEFAULT,
    max_examples=10,
    deadline=None,
    suppress_health_check=[HealthCheck.differing_executors],
)

_explicit = os.environ.get("HYPOTHESIS_PROFILE")
if _explicit:
    settings.load_profile(_explicit)
elif Path.cwd().name == "mutants":
    settings.load_profile("mutmut")
elif os.environ.get("CI"):
    settings.load_profile("ci")
else:
    settings.load_profile("dev")


_LOCAL_HOSTS = {"", "localhost", "127.0.0.1"}
# Connection parameters that cannot point libpq at another server.
_SAFE_OPTIONS = {"isolation_level", "server_side_binding", "client_encoding"}
# libpq reads these when Django leaves a parameter empty.
_REDIRECTING_ENV = ("PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE")


def _is_local_host(host: str) -> bool:
    """A loopback name, or one unix-socket directory (no comma-separated list)."""
    return host in _LOCAL_HOSTS or (host.startswith("/") and "," not in host)


def check_test_database(
    settings_dict: Mapping[str, Any], environ: Mapping[str, str]
) -> None:
    """Refuse to run tests unless they can only reach a PostgreSQL on this machine.

    Why: pytest-django creates and drops ``test_<NAME>`` on whatever server the
    settings name, so a stray CD_DB_HOST, OPTIONS entry or libpq variable must
    never aim the test run at a real database (testing-lessons: guard the test
    database). Raises ``pytest.UsageError`` naming the offending setting.
    """
    if settings_dict["ENGINE"] != "django.db.backends.postgresql":
        raise pytest.UsageError(
            f"tests need PostgreSQL, ENGINE is {settings_dict['ENGINE']!r}"
        )
    host = str(settings_dict.get("HOST") or "")
    if not _is_local_host(host):
        raise pytest.UsageError(f"test database HOST {host!r} is not local")
    unsafe = sorted(set(settings_dict.get("OPTIONS") or {}) - _SAFE_OPTIONS)
    if unsafe:
        raise pytest.UsageError(f"test database OPTIONS may redirect: {unsafe}")
    for name in _REDIRECTING_ENV:
        if environ.get(name):
            raise pytest.UsageError(f"unset {name} before running the tests")
    if not _is_local_host(environ.get("PGHOST", "")):
        raise pytest.UsageError("PGHOST is not local")
    test_name = (settings_dict.get("TEST") or {}).get("NAME")
    if test_name and not str(test_name).startswith("test_"):
        raise pytest.UsageError(f"TEST NAME {test_name!r} must start with test_")


@pytest.fixture(scope="session")
def django_db_modify_db_settings(django_db_modify_db_settings: None) -> None:
    """Check the database settings before pytest-django creates the test DB."""
    try:
        check_test_database(django_settings.DATABASES["default"], os.environ)
    except pytest.UsageError as exc:
        pytest.exit(f"refusing to run: {exc}", returncode=4)


@pytest.fixture(scope="session")
def django_db_setup(django_db_setup: None, django_db_blocker: Any) -> None:
    """Create the DatabaseCache table in the test database (rate limiting uses it)."""
    with django_db_blocker.unblock():
        call_command("createcachetable", verbosity=0)


# A fixed instant well inside one rate-limit window (its jitter is under 60 s).
FROZEN_EPOCH = 1_800_000_000.0


@pytest.fixture
def frozen_ratelimit_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin django-ratelimit's clock so a test never straddles a minute boundary.

    The limiter keys its counters by time window; a test that crosses into the
    next window would start a fresh count and flake. Only the limiter's own
    `time` reference is replaced, never the global `time.time`.
    """
    monkeypatch.setattr(
        django_ratelimit.core, "time", types.SimpleNamespace(time=lambda: FROZEN_EPOCH)
    )


@pytest.fixture(autouse=True)
def db_is_empty(request: pytest.FixtureRequest) -> Iterator[None]:
    """Fail fast, naming the model, if a db-using test starts with leaked rows."""
    if {"db", "transactional_db"} & set(request.fixturenames):
        request.getfixturevalue("db")
        for model in apps.get_app_config("raffle").get_models():
            if model._default_manager.exists():
                pytest.fail(
                    f"{model.__name__} is not empty at test start (leaked rows)"
                )
    yield


@pytest.fixture
def book_factory(db: None) -> Callable[..., Book]:
    return factories.make_book


@pytest.fixture
def conference_factory(db: None) -> Callable[..., Conference]:
    return factories.make_conference


@pytest.fixture
def drawing_factory(db: None) -> Callable[..., Drawing]:
    return factories.make_drawing


@pytest.fixture
def prize_line_factory(db: None) -> Callable[..., PrizeLine]:
    return factories.make_prize_line


@pytest.fixture
def entrant_factory(db: None) -> Callable[..., Entrant]:
    return factories.make_entrant


@pytest.fixture
def pick_factory(db: None) -> Callable[..., Pick]:
    return factories.make_pick
