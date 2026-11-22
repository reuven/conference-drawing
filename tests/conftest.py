"""Shared test harness: environment, Hypothesis profiles, DB guards."""

import os
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from django.apps import apps
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

settings.register_profile(
    "dev", max_examples=50, deadline=None, suppress_health_check=[HealthCheck.too_slow]
)
settings.register_profile("ci", max_examples=200, deadline=None)
settings.register_profile("mutmut", max_examples=10, deadline=None)

_explicit = os.environ.get("HYPOTHESIS_PROFILE")
if _explicit:
    settings.load_profile(_explicit)
elif Path.cwd().name == "mutants":
    settings.load_profile("mutmut")
elif os.environ.get("CI"):
    settings.load_profile("ci")
else:
    settings.load_profile("dev")


@pytest.fixture(scope="session")
def django_db_setup(django_db_setup: None, django_db_blocker: Any) -> None:
    """Create the DatabaseCache table in the test database (rate limiting uses it)."""
    with django_db_blocker.unblock():
        call_command("createcachetable", verbosity=0)


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
