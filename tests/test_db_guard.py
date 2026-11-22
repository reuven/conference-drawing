"""The conftest guard: tests may only ever reach a PostgreSQL on this machine."""

from typing import Any

import pytest

from tests.conftest import check_test_database


def _db(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": "conference_drawing",
        "HOST": "",
        "PORT": "",
        "OPTIONS": {},
    }
    return base | overrides


@pytest.mark.parametrize(
    "host", ["", "localhost", "127.0.0.1", "/opt/pgsocket", "/var/run/postgresql"]
)
def test_local_hosts_are_allowed(host: str) -> None:
    check_test_database(_db(HOST=host), {})


@pytest.mark.parametrize(
    "host",
    [
        "db.example.invalid",
        "10.0.0.5",
        "localhost.example.invalid",
        "127.0.0.1,db.example.invalid",
        "localhost,db.example.invalid",
        "/opt/pgsocket,db.example.invalid",
        "::1x",
    ],
)
def test_remote_hosts_are_refused(host: str) -> None:
    with pytest.raises(pytest.UsageError, match="HOST"):
        check_test_database(_db(HOST=host), {})


@pytest.mark.parametrize(
    "options",
    [
        {"host": "db.example.invalid"},
        {"hostaddr": "10.0.0.5"},
        {"service": "prod"},
        {"dsn": "postgresql://db.example.invalid/x"},
        {"conninfo": "host=db.example.invalid"},
        {"options": "-c search_path=prod"},
        {"passfile": "/elsewhere/.pgpass"},
    ],
)
def test_options_that_could_redirect_are_refused(options: dict[str, str]) -> None:
    with pytest.raises(pytest.UsageError, match="OPTIONS"):
        check_test_database(_db(OPTIONS=options), {})


def test_harmless_options_are_allowed() -> None:
    check_test_database(_db(OPTIONS={"isolation_level": 1}), {})


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("PGHOST", "db.example.invalid"),
        ("PGHOSTADDR", "10.0.0.5"),
        ("PGSERVICE", "prod"),
        ("PGSERVICEFILE", "/elsewhere/pg_service.conf"),
    ],
)
def test_libpq_environment_that_could_redirect_is_refused(
    name: str, value: str
) -> None:
    with pytest.raises(pytest.UsageError, match=name):
        check_test_database(_db(), {name: value})


@pytest.mark.parametrize("value", ["localhost", "/opt/pgsocket"])
def test_local_pghost_is_allowed(value: str) -> None:
    check_test_database(_db(), {"PGHOST": value})


def test_test_database_name_must_look_like_a_test_database() -> None:
    check_test_database(_db(TEST={"NAME": "test_raffle"}), {})
    with pytest.raises(pytest.UsageError, match="TEST NAME"):
        check_test_database(_db(TEST={"NAME": "conference_drawing"}), {})


def test_non_postgres_engine_is_refused() -> None:
    with pytest.raises(pytest.UsageError, match="ENGINE"):
        check_test_database(_db(ENGINE="django.db.backends.sqlite3"), {})


def test_the_running_session_passed_the_guard(db: None) -> None:
    from django.db import connection

    assert connection.vendor == "postgresql"
    assert connection.settings_dict["NAME"] == "test_conference_drawing"
