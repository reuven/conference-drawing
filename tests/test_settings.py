import importlib
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from django.core.exceptions import ImproperlyConfigured

from conference_drawing import settings as cd_settings


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


def test_data_dir_drives_database_and_media(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CD_DATA_DIR", str(tmp_path))
    importlib.reload(cd_settings)
    assert Path(str(cd_settings.DATABASES["default"]["NAME"])) == (
        tmp_path / "db.sqlite3"
    )
    assert Path(cd_settings.MEDIA_ROOT) == tmp_path / "media"


def test_cache_is_database_backed() -> None:
    cache = cd_settings.CACHES["default"]
    assert cache["BACKEND"] == "django.core.cache.backends.db.DatabaseCache"
    assert cache["LOCATION"] == "cd_cache"


def test_cache_table_exists_and_round_trips(db: None) -> None:
    from django.core.cache import cache

    cache.set("k", "v")
    assert cache.get("k") == "v"
