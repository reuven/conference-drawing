import importlib
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from django.core.exceptions import ImproperlyConfigured

from conference_drawing import settings as cd_settings

_DB_VARS = ("CD_DB_NAME", "CD_DB_USER", "CD_DB_HOST", "CD_DB_PORT", "CD_DB_PASSWORD")


@pytest.fixture(autouse=True)
def _restore_settings_module() -> Iterator[None]:
    yield
    # Reload against a known-good environment, whatever the test left behind.
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("DJANGO_SECRET_KEY", "test-only-secret-key-not-for-production")
        mp.delenv("CD_ENV_FILE", raising=False)
        mp.delenv("CD_DATA_DIR", raising=False)
        mp.delenv("DJANGO_DEBUG", raising=False)
        mp.delenv("DJANGO_ALLOWED_HOSTS", raising=False)
        importlib.reload(cd_settings)


def test_settings_refuse_missing_secret_key_when_not_debug(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("DJANGO_SECRET_KEY", raising=False)
    monkeypatch.delenv("DJANGO_DEBUG", raising=False)
    monkeypatch.setenv("CD_ENV_FILE", str(tmp_path / "absent.env"))
    with pytest.raises(ImproperlyConfigured):
        importlib.reload(cd_settings)


def test_debug_mode_allows_missing_secret_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("DJANGO_SECRET_KEY", raising=False)
    monkeypatch.delenv("DJANGO_ALLOWED_HOSTS", raising=False)
    monkeypatch.setenv("DJANGO_DEBUG", "1")
    monkeypatch.setenv("CD_ENV_FILE", str(tmp_path / "absent.env"))
    importlib.reload(cd_settings)
    assert cd_settings.DEBUG is True
    assert cd_settings.SECRET_KEY
    assert cd_settings.ALLOWED_HOSTS == ["localhost", "127.0.0.1", "[::1]"]
    assert cd_settings.SESSION_COOKIE_SECURE is False


def test_settings_never_read_home_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / ".env").write_text("DJANGO_SECRET_KEY=from-home\n")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("DJANGO_SECRET_KEY", raising=False)
    monkeypatch.delenv("DJANGO_DEBUG", raising=False)
    monkeypatch.setenv("CD_ENV_FILE", str(tmp_path / "absent.env"))
    with pytest.raises(ImproperlyConfigured, match="DJANGO_SECRET_KEY"):
        importlib.reload(cd_settings)


def test_env_file_is_read_without_polluting_environ(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env = tmp_path / "x.env"
    env.write_text(
        "DJANGO_SECRET_KEY=from-file\nDJANGO_ALLOWED_HOSTS=a.example, b.example\n"
    )
    monkeypatch.delenv("DJANGO_SECRET_KEY", raising=False)
    monkeypatch.delenv("DJANGO_ALLOWED_HOSTS", raising=False)
    monkeypatch.setenv("CD_ENV_FILE", str(env))
    importlib.reload(cd_settings)
    assert cd_settings.SECRET_KEY == "from-file"  # noqa: S105
    assert cd_settings.ALLOWED_HOSTS == ["a.example", "b.example"]
    assert "DJANGO_SECRET_KEY" not in os.environ


def test_data_dir_drives_media_and_static(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CD_DATA_DIR", str(tmp_path))
    importlib.reload(cd_settings)
    assert Path(cd_settings.MEDIA_ROOT) == tmp_path / "media"
    assert Path(cd_settings.STATIC_ROOT) == tmp_path / "static"


def test_database_defaults_to_peer_auth_over_the_local_socket(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for name in _DB_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CD_ENV_FILE", str(tmp_path / "absent.env"))
    monkeypatch.setenv("DJANGO_SECRET_KEY", "k")
    importlib.reload(cd_settings)
    db = cd_settings.DATABASES["default"]
    assert db["ENGINE"] == "django.db.backends.postgresql"
    assert db["NAME"] == "conference_drawing"
    assert db["USER"] == "conference-drawing"
    assert db["HOST"] == ""  # empty: the local unix socket
    assert db["PORT"] == ""
    assert db["PASSWORD"] == ""  # empty: peer auth, no password anywhere
    assert db["CONN_MAX_AGE"] == 60
    assert db["CONN_HEALTH_CHECKS"] is True
    assert "OPTIONS" not in db
    assert cd_settings.DATABASES["default"].get("ATOMIC_REQUESTS", False) is False


def test_database_comes_from_env_and_env_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env = tmp_path / "x.env"
    env.write_text("DJANGO_SECRET_KEY=k\nCD_DB_PASSWORD=from-file\n")
    monkeypatch.setenv("CD_ENV_FILE", str(env))
    monkeypatch.delenv("CD_DB_PASSWORD", raising=False)
    monkeypatch.setenv("CD_DB_NAME", "raffle")
    monkeypatch.setenv("CD_DB_USER", "booth")
    monkeypatch.setenv("CD_DB_HOST", "/var/run/postgresql")
    monkeypatch.setenv("CD_DB_PORT", "5433")
    importlib.reload(cd_settings)
    db = cd_settings.DATABASES["default"]
    assert (db["NAME"], db["USER"], db["HOST"], db["PORT"]) == (
        "raffle",
        "booth",
        "/var/run/postgresql",
        "5433",
    )
    assert db["PASSWORD"] == "from-file"  # noqa: S105
    assert "CD_DB_PASSWORD" not in os.environ


def test_cache_is_database_backed() -> None:
    cache = cd_settings.CACHES["default"]
    assert cache["BACKEND"] == "django.core.cache.backends.db.DatabaseCache"
    assert cache["LOCATION"] == "cd_cache"


def test_cache_table_exists_and_round_trips(db: None) -> None:
    from django.core.cache import cache

    cache.set("k", "v")
    assert cache.get("k") == "v"


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        ("localhost", ["localhost", "127.0.0.1", "[::1]"]),
        ("raffle.example", ["raffle.example", "localhost", "127.0.0.1", "[::1]"]),
    ],
)
def test_debug_mode_always_accepts_the_loopback_names(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    configured: str,
    expected: list[str],
) -> None:
    # `init --domain localhost` writes only "localhost", but runserver prints
    # http://127.0.0.1:8000/; in debug both must work.
    monkeypatch.setenv("DJANGO_DEBUG", "1")
    monkeypatch.setenv("DJANGO_ALLOWED_HOSTS", configured)
    monkeypatch.setenv("CD_ENV_FILE", str(tmp_path / "absent.env"))
    importlib.reload(cd_settings)
    assert cd_settings.ALLOWED_HOSTS == expected


def test_production_accepts_only_the_configured_hosts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("DJANGO_DEBUG", raising=False)
    monkeypatch.setenv("DJANGO_ALLOWED_HOSTS", "localhost")
    monkeypatch.setenv("CD_ENV_FILE", str(tmp_path / "absent.env"))
    importlib.reload(cd_settings)
    assert cd_settings.ALLOWED_HOSTS == ["localhost"]
