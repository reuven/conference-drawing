"""The console entry point and the ``init`` command (first-run setup)."""

import io
import os
import sqlite3
import stat
import subprocess
import sys
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import connection
from dotenv import dotenv_values
from hypothesis import example, given
from hypothesis import strategies as st

from conference_drawing import cli
from conference_drawing.cli import main
from conference_drawing.raffle.models import InstallSettings

SUPERUSER_PASSWORD = "pw-correct-horse-battery"  # noqa: S105


@pytest.fixture(autouse=True)
def env_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point init at a tmp env file; never the repo .env or the home directory."""
    path = tmp_path / "install.env"
    monkeypatch.setenv("CD_ENV_FILE", str(path))  # restored after the test
    monkeypatch.setenv("CD_DATA_DIR", str(tmp_path / "data"))
    for name in ("USERNAME", "EMAIL", "PASSWORD"):
        monkeypatch.delenv(f"DJANGO_SUPERUSER_{name}", raising=False)
    return path


@pytest.fixture
def superuser_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DJANGO_SUPERUSER_USERNAME", "admin")
    monkeypatch.setenv("DJANGO_SUPERUSER_EMAIL", "admin@example.com")
    monkeypatch.setenv("DJANGO_SUPERUSER_PASSWORD", SUPERUSER_PASSWORD)


def _init(env_file: Path, *extra: str) -> int:
    return main(["init", "--env-file", str(env_file), "--non-interactive", *extra])


def test_main_passes_arguments_to_django_management(db: None) -> None:
    # `check` now inspects JSONField support, which touches the database.
    assert main(["check"]) == 0


def test_passthrough_to_manage(db: None, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["showmigrations", "raffle"]) == 0
    assert "raffle" in capsys.readouterr().out


@pytest.mark.django_db
def test_init_writes_env_0600(
    env_file: Path, superuser_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _init(env_file, "--domain", "raffle.example.com", "booth.example.org") == 0
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    values = dotenv_values(env_file)
    assert len(values["DJANGO_SECRET_KEY"] or "") >= 50
    assert values["DJANGO_ALLOWED_HOSTS"] == "raffle.example.com,booth.example.org"
    out = capsys.readouterr()
    assert "wrote DJANGO_SECRET_KEY" in out.out
    assert "wrote DJANGO_ALLOWED_HOSTS" in out.out


@pytest.mark.django_db
def test_init_never_prints_secrets(
    env_file: Path, superuser_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _init(env_file, "--domain", "raffle.example.com") == 0
    secret = dotenv_values(env_file)["DJANGO_SECRET_KEY"] or ""
    assert secret
    out = capsys.readouterr()
    for text in (out.out, out.err):
        assert secret not in text
        assert SUPERUSER_PASSWORD not in text


@pytest.mark.django_db
def test_init_keeps_existing_secret(env_file: Path, superuser_env: None) -> None:
    env_file.write_text("DJANGO_SECRET_KEY=keep-me\nDRIP_API_KEY=abc\n")
    env_file.chmod(0o600)
    assert _init(env_file, "--domain", "raffle.example.com") == 0
    values = dotenv_values(env_file)
    assert values["DJANGO_SECRET_KEY"] == "keep-me"  # noqa: S105
    assert values["DRIP_API_KEY"] == "abc"
    assert values["DJANGO_ALLOWED_HOSTS"] == "raffle.example.com"


@pytest.mark.django_db
def test_init_tightens_loose_permissions(
    env_file: Path, superuser_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    env_file.write_text("DJANGO_SECRET_KEY=keep-me")  # no trailing newline
    env_file.chmod(0o644)
    assert _init(env_file, "--domain", "raffle.example.com") == 0
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    assert "tightened" in capsys.readouterr().out
    assert dotenv_values(env_file)["DJANGO_ALLOWED_HOSTS"] == "raffle.example.com"


@pytest.mark.django_db
def test_init_idempotent(env_file: Path, superuser_env: None) -> None:
    assert _init(env_file, "--domain", "raffle.example.com") == 0
    first = env_file.read_text()
    assert _init(env_file) == 0  # domain already recorded; superuser exists
    assert env_file.read_text() == first
    assert get_user_model().objects.filter(is_superuser=True).count() == 1
    assert InstallSettings.objects.count() == 1


@pytest.mark.django_db
def test_init_keeps_existing_hosts_over_domain_flag(
    env_file: Path, superuser_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    env_file.write_text("DJANGO_ALLOWED_HOSTS=old.example.com\n")
    assert _init(env_file, "--domain", "new.example.com") == 0
    assert dotenv_values(env_file)["DJANGO_ALLOWED_HOSTS"] == "old.example.com"
    assert "kept DJANGO_ALLOWED_HOSTS" in capsys.readouterr().out


@pytest.mark.django_db
def test_init_env_file_defaults_to_cd_env_file(
    env_file: Path, superuser_env: None
) -> None:
    assert main(["init", "--non-interactive", "--domain", "a.example.com"]) == 0
    assert env_file.exists()


def test_env_file_default_is_project_dotenv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CD_ENV_FILE")
    path = cli.resolve_env_file(None)
    assert path == cli.BASE_DIR / ".env"
    assert path != Path.home() / ".env"


@pytest.mark.django_db
def test_init_creates_install_settings_and_cache_table(
    env_file: Path, superuser_env: None
) -> None:
    assert not InstallSettings.objects.exists()
    assert _init(env_file, "--domain", "raffle.example.com") == 0
    assert InstallSettings.objects.count() == 1
    assert "cd_cache" in connection.introspection.table_names()


@pytest.mark.django_db
def test_non_interactive_superuser_from_env(
    env_file: Path, superuser_env: None
) -> None:
    assert _init(env_file, "--domain", "raffle.example.com") == 0
    user = get_user_model().objects.get(username="admin")
    assert user.is_superuser
    assert user.email == "admin@example.com"
    assert user.check_password(SUPERUSER_PASSWORD)


@pytest.mark.django_db
def test_non_interactive_missing_superuser_env_fails(
    env_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _init(env_file, "--domain", "raffle.example.com") == 2
    err = capsys.readouterr().err
    assert "DJANGO_SUPERUSER_USERNAME" in err
    assert "DJANGO_SUPERUSER_PASSWORD" in err
    assert not get_user_model().objects.exists()


@pytest.mark.django_db
def test_existing_superuser_makes_missing_env_a_noop(env_file: Path) -> None:
    get_user_model().objects.create_superuser("boss", "b@example.com", "x" * 12)
    assert _init(env_file, "--domain", "raffle.example.com") == 0
    assert get_user_model().objects.count() == 1


@pytest.mark.django_db
def test_existing_username_is_a_noop(env_file: Path, superuser_env: None) -> None:
    get_user_model().objects.create_user("admin", "a@example.com", "x" * 12)
    assert _init(env_file, "--domain", "raffle.example.com") == 0
    user = get_user_model().objects.get(username="admin")
    assert not user.is_superuser  # untouched, not overwritten, not an error


def test_non_interactive_without_domain_exits_2(
    env_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _init(env_file) == 2
    assert "--domain" in capsys.readouterr().err
    assert not env_file.exists()


@pytest.mark.django_db
def test_interactive_prompts_for_domains(
    env_file: Path, superuser_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "builtins.input", lambda _prompt="": "a.example.com b.example.com"
    )
    assert main(["init", "--env-file", str(env_file)]) == 0
    assert (
        dotenv_values(env_file)["DJANGO_ALLOWED_HOSTS"] == "a.example.com,b.example.com"
    )


def test_interactive_rejects_bad_domain_prompt(
    env_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("builtins.input", lambda _prompt="": "https://x.example.com")
    assert main(["init", "--env-file", str(env_file)]) == 2
    assert "hostname" in capsys.readouterr().err
    assert not env_file.exists()


def test_interactive_empty_domain_prompt_exits_2(
    env_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("builtins.input", lambda _prompt="": "  ")
    assert main(["init", "--env-file", str(env_file)]) == 2


@pytest.mark.django_db
def test_interactive_superuser_uses_createsuperuser_prompts(
    env_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_call_command(name: str, *args: Any, **kwargs: Any) -> None:
        if name == "createsuperuser":
            calls.append((name, kwargs))
        else:
            call_command(name, *args, **kwargs)

    monkeypatch.setattr(cli, "call_command", fake_call_command)
    assert main(["init", "--env-file", str(env_file), "--domain", "a.example.com"]) == 0
    assert calls == [("createsuperuser", {"interactive": True})]


@pytest.mark.parametrize(
    "bad",
    [
        "https://x.example.com",
        "x.example.com/path",
        "x.example.com:8000",
        "x.example.com,y.example.com",
        "x example.com",
        "x.example.com\n",
        "",
        "-bad.example.com",
        "bad-.example.com",
        "a..example.com",
        "under_score.example.com",
        "*.example.com",
    ],
)
def test_domain_validation_rejects(bad: str, env_file: Path) -> None:
    with pytest.raises(SystemExit) as exc:
        _init(env_file, "--domain", bad)
    assert exc.value.code == 2
    assert not env_file.exists()


@pytest.mark.parametrize(
    "good", ["raffle.example.com", "localhost", "192.0.2.10", "EXAMPLE.com", "a-b.io"]
)
def test_domain_validation_accepts(good: str) -> None:
    assert cli.parse_hostname(good) == good.lower()


# --- env writer ---------------------------------------------------------------

# python-dotenv interpolates ${NAME} even inside single quotes and offers no way
# to escape it, so the writer refuses it; everything else must round-trip.
_values = st.text(
    alphabet=st.characters(blacklist_categories=["Cs"], blacklist_characters="\x00\r"),
    max_size=60,
).filter(lambda v: "${" not in v)


@given(value=_values)
@example(value="a#b $c 'q' \"dq\" \\ back")
@example(value="  padded  ")
@example(value="line1\nline2")
@example(value="")
def test_env_value_round_trips_through_dotenv(value: str) -> None:
    line = cli.format_env_line("KEY", value)
    assert dotenv_values(stream=io.StringIO(line))["KEY"] == value


def test_env_writer_rejects_interpolation_syntax() -> None:
    with pytest.raises(ValueError, match="interpolation"):
        cli.format_env_line("KEY", "pre${HOME}post")


def test_env_writer_rejects_bad_key() -> None:
    with pytest.raises(ValueError, match="key"):
        cli.format_env_line("bad key", "v")


def test_env_file_is_created_0600_from_the_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No write-then-chmod window: the file is created with mode 0600."""
    seen: list[int] = []
    real_open = os.open

    def spy(path: Any, flags: int, mode: int = 0o777, **kw: Any) -> int:
        seen.append(mode)
        return real_open(path, flags, mode, **kw)

    monkeypatch.setattr(os, "open", spy)
    cli.write_env_values(tmp_path / "fresh.env", {"A": "1"})
    assert seen == [0o600]


# --- real bootstrap: no .env, no secret key, fresh data dir --------------------


def test_init_bootstraps_from_nothing_in_a_fresh_process(tmp_path: Path) -> None:
    exe = Path(sys.executable).parent / "conference-drawing"
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("DJANGO_", "CD_", "DRIP_"))
    }
    env |= {
        "CD_DATA_DIR": str(tmp_path / "fresh-data"),
        "CD_ENV_FILE": str(tmp_path / "fresh.env"),
        "HOME": str(tmp_path / "home"),
        "DJANGO_SUPERUSER_USERNAME": "admin",
        "DJANGO_SUPERUSER_EMAIL": "admin@example.com",
        "DJANGO_SUPERUSER_PASSWORD": SUPERUSER_PASSWORD,
    }

    def run(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603
            [str(exe), "init", "--non-interactive", *args],
            env=env,
            capture_output=True,
            text=True,
            cwd=tmp_path,
            timeout=50,
            check=False,
        )

    result = run("--domain", "raffle.example.com")
    assert result.returncode == 0, result.stderr
    secret = dotenv_values(tmp_path / "fresh.env")["DJANGO_SECRET_KEY"] or ""
    assert secret not in result.stdout + result.stderr
    assert SUPERUSER_PASSWORD not in result.stdout + result.stderr
    with closing(sqlite3.connect(tmp_path / "fresh-data" / "db.sqlite3")) as db:
        tables = {r[0] for r in db.execute("select name from sqlite_master")}
        assert {"cd_cache", "raffle_installsettings"} <= tables
        count = db.execute("select count(*) from raffle_installsettings").fetchone()
        assert count == (1,)
        assert db.execute("select username from auth_user").fetchall() == [("admin",)]
    again = run()
    assert again.returncode == 0, again.stderr


@pytest.mark.django_db
def test_complete_env_file_with_loose_mode_is_tightened(
    env_file: Path, superuser_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    env_file.write_text("DJANGO_SECRET_KEY=k\nDJANGO_ALLOWED_HOSTS=a.example.com\n")
    env_file.chmod(0o640)
    assert _init(env_file) == 0
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    assert "tightened" in capsys.readouterr().out


@pytest.mark.django_db
def test_complete_private_env_file_is_left_untouched(
    env_file: Path, superuser_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    env_file.write_text("DJANGO_SECRET_KEY=k\nDJANGO_ALLOWED_HOSTS=a.example.com\n")
    env_file.chmod(0o600)
    assert _init(env_file) == 0
    assert "tightened" not in capsys.readouterr().out


@pytest.mark.django_db
def test_interactive_existing_superuser_is_a_noop(
    env_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    get_user_model().objects.create_superuser("boss", "b@example.com", "x" * 12)

    def refuse(name: str, *args: Any, **kwargs: Any) -> None:
        assert name != "createsuperuser"
        call_command(name, *args, **kwargs)

    monkeypatch.setattr(cli, "call_command", refuse)
    assert main(["init", "--env-file", str(env_file), "--domain", "a.example.com"]) == 0
