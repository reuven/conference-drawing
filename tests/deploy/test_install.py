"""remote/install.sh and remote/uninstall.sh, run whole in a temp root.

``CD_ROOT`` points every server path into a temp dir, and fakes stand in for
everything privileged: ``id`` says root, ``runuser`` runs shell helpers as the
current user (and fakes the app's own commands), ``chown``, ``useradd``,
``userdel``, ``systemctl``, ``caddy``, ``flock`` and ``uv`` are canned. No real
server step runs on this machine.
"""

import hashlib
import io
import json
import re
import shutil
import sqlite3
import subprocess
import tarfile
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.deploy.conftest import (
    COMMIT,
    DEPLOY,
    DOMAIN,
    FAKES,
    Entry,
    Fakes,
    make_backup_tarball,
    os_mode,
)

COMMON = str(DEPLOY / "remote" / "common.sh")
APP_UNITS = [
    "conference-drawing-web.service",
    "conference-drawing-drip.service",
    "conference-drawing-snapshot.timer",
]
ROOT_SCRIPTS = [
    "install.sh",
    "uninstall.sh",
    "backup.sh",
    "stop.sh",
    "outbox_status.sh",
]


@dataclass
class Server:
    fakes: Fakes
    root: Path
    release: Path

    @property
    def app(self) -> Path:
        return self.root / "srv" / "conference-drawing"

    @property
    def data(self) -> Path:
        return self.app / "data"

    def install(self, *args: str, stdin: str = "") -> subprocess.CompletedProcess[str]:
        script = str(self.release / "remote" / "install.sh")
        return self.fakes.run([script, DOMAIN, *args], stdin=stdin)

    def uninstall(self) -> subprocess.CompletedProcess[str]:
        return self.fakes.run([str(self.release / "remote" / "uninstall.sh")])

    def log(self) -> list[str]:
        """Every fake call as ``tool args``, in order."""
        return [f"{c.tool} {c.key}" for c in self.fakes.calls()]

    def index(self, needle: str) -> int:
        return next(i for i, line in enumerate(self.log()) if needle in line)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canned(active: bool) -> dict[str, list[Entry]]:
    return {
        "id": [
            {"match": "-u conference-drawing", "stdout": "999\n"},
            {"match": "-u", "stdout": "0\n"},
        ],
        "useradd": [{"match": "--system"}],
        "userdel": [{"match": "conference-drawing"}],
        "chown": [{"match": "-h"}],
        "flock": [{"match": "--wait"}],
        "caddy": [{"match": "validate"}],
        "uv": [
            {"match": "--version", "stdout": "uv 0.13.9 (0123abc 2026-11-01)\n"},
            {"match": "venv --quiet", "mkdir_last": True},
            {"match": "pip install"},
        ],
        "systemctl": [
            {"match": "is-active", "exit": 0 if active else 3},
            {"match": "stop"},
            {"match": "start"},
            {"match": "daemon-reload"},
            {"match": "enable --now"},
            {"match": "disable --now"},
            {"match": "reset-failed"},
            {"match": "reload caddy"},
        ],
        "runuser": [
            {"match": "/bin/conference-drawing init"},
            {"match": "/bin/conference-drawing collectstatic"},
            {"match": "", "passthrough": True},
        ],
    }


@pytest.fixture
def server(fakes: Fakes) -> Server:
    """A fresh server root with a staged release, as up.sh leaves it."""
    root = fakes.root / "root"
    release = root / "tmp" / f"conference-drawing-{COMMIT}-up-0a1b2c3d"
    release.mkdir(parents=True)
    for part in ("remote", "systemd", "caddy"):
        shutil.copytree(DEPLOY / part, release / part)
    wheel = release / "conference_drawing-0.1.0-py3-none-any.whl"
    wheel.write_text("fake wheel\n")
    (release / "wheel.sha256").write_text(f"{_sha256(wheel)}  {wheel.name}\n")
    (release / "requirements.txt").write_text("django==6.1.3 --hash=sha256:00\n")
    (release / "version.json").write_text(
        json.dumps({"commit": COMMIT, "built_at": "", "branch": "main"})
    )
    (root / "etc" / "caddy" / "sites").mkdir(parents=True)
    (root / "etc" / "caddy" / "Caddyfile").write_text(
        "import /etc/caddy/sites/*.caddy\n"
    )
    (root / "etc" / "systemd" / "system").mkdir(parents=True)
    (root / "usr" / "local" / "bin").mkdir(parents=True)
    (root / "usr" / "local" / "bin" / "uv").symlink_to(FAKES / "uv")
    fakes.extra_env["CD_ROOT"] = str(root)
    fakes.responses.update(_canned(active=False))
    return Server(fakes, root, release)


@pytest.fixture
def upgrade(server: Server) -> Server:
    """A running install: data, two old venvs, units active."""
    server.data.mkdir(parents=True)
    (server.data / ".env").write_text("DJANGO_SECRET_KEY='x'\n")
    (server.data / ".maintenance.lock").touch()
    with closing(sqlite3.connect(server.data / "db.sqlite3")) as db, db:
        db.execute("create table books (title text)")
    for name in (
        "venv-20261101T000000Z-aaaaaaaaaaaa",
        "venv-20261115T000000Z-bbbbbbbbbbbb",
    ):
        (server.app / name / "bin").mkdir(parents=True)
    (server.app / "venv").symlink_to("venv-20261115T000000Z-bbbbbbbbbbbb")
    server.fakes.responses.update(_canned(active=True))
    return server


def _venvs(server: Server) -> list[str]:
    return sorted(p.name for p in server.app.glob("venv-*"))


# --- a fresh install ----------------------------------------------------------


def test_install_fresh_lays_out_the_server(server: Server) -> None:
    result = server.install(stdin="DRIP_API_KEY=key-1=\nDRIP_ACCOUNT_ID=42\n")
    assert result.returncode == 0, result.stderr
    assert os_mode(server.data) == 0o711
    assert os_mode(server.data / "snapshots") == 0o700
    for sub in ("static", "media", "media/covers"):
        assert os_mode(server.data / sub) == 0o755, sub
    env = server.data / ".env"
    assert env.read_text() == "DRIP_API_KEY='key-1='\nDRIP_ACCOUNT_ID='42'\n"
    assert os_mode(env) == 0o600
    (new,) = _venvs(server)
    assert (server.app / "venv").readlink() == Path(new)
    assert re.fullmatch(r"venv-\d{8}T\d{6}Z-0123456789ab", new)
    log = server.log()
    assert any("pip install --require-hashes" in line for line in log)
    assert any("--no-deps" in line and line.endswith(".whl") for line in log)
    assert any(f"init --non-interactive --domain {DOMAIN}" in line for line in log)
    assert f"systemctl enable --now {' '.join(APP_UNITS)}" in log
    assert log[-1] == "systemctl reload caddy"
    assert (server.root / "etc/caddy/sites/conference-drawing.caddy").exists()
    assert (server.app / "bin" / "snapshot.sh").exists()


def test_install_takes_the_maintenance_lock_before_changing_anything(
    server: Server,
) -> None:
    assert server.install().returncode == 0
    lock = server.index("flock --wait")
    assert lock < server.index("uv venv")
    assert lock < server.index("systemctl daemon-reload")


def test_install_refuses_a_wheel_that_fails_its_checksum(server: Server) -> None:
    (server.release / "conference_drawing-0.1.0-py3-none-any.whl").write_text("evil\n")
    result = server.install()
    assert result.returncode != 0
    assert not any("--no-deps" in line for line in server.log())


def test_install_is_idempotent_for_drip_keys(server: Server) -> None:
    stdin = "DRIP_API_KEY=key-1\n"
    assert server.install(stdin=stdin).returncode == 0
    assert server.install(stdin=stdin).returncode == 0
    assert (server.data / ".env").read_text() == "DRIP_API_KEY='key-1'\n"


# --- upgrades ------------------------------------------------------------------


def test_upgrade_snapshots_before_migrate_and_keeps_one_previous_venv(
    upgrade: Server,
) -> None:
    result = upgrade.install()
    assert result.returncode == 0, result.stderr
    collect = upgrade.index("conference-drawing collectstatic")
    stop = upgrade.index("systemctl stop")
    snapshot = upgrade.index("bin/snapshot.sh")
    migrate = upgrade.index("conference-drawing init")
    start = upgrade.index("systemctl enable --now")
    assert collect < stop < snapshot < migrate < start
    assert len(list((upgrade.data / "snapshots").glob("db-*.sqlite3"))) == 1
    venvs = _venvs(upgrade)
    assert venvs[0] == "venv-20261115T000000Z-bbbbbbbbbbbb"  # previous, kept
    assert len(venvs) == 2  # the older one is pruned
    assert (upgrade.app / "venv").readlink() == Path(venvs[1])


def test_upgrade_failing_at_pip_install_leaves_the_old_units_running(
    upgrade: Server,
) -> None:
    upgrade.fakes.prepend("uv", {"match": "pip install --require-hashes", "exit": 1})
    result = upgrade.install()
    assert result.returncode != 0
    assert not any(line.startswith("systemctl stop") for line in upgrade.log())
    assert not any(line.startswith("systemctl start") for line in upgrade.log())
    assert (upgrade.app / "venv").readlink() == Path(
        "venv-20261115T000000Z-bbbbbbbbbbbb"
    )
    assert _venvs(upgrade) == [
        "venv-20261101T000000Z-aaaaaaaaaaaa",
        "venv-20261115T000000Z-bbbbbbbbbbbb",
    ]


def test_upgrade_failing_at_migrate_restarts_the_units_it_stopped(
    upgrade: Server,
) -> None:
    upgrade.fakes.prepend("runuser", {"match": "conference-drawing init", "exit": 1})
    result = upgrade.install()
    assert result.returncode != 0
    log = upgrade.log()
    stops = [line.split()[-1] for line in log if line.startswith("systemctl stop")]
    assert sorted(stops) == sorted(APP_UNITS)
    (start,) = [line for line in log if line.startswith("systemctl start")]
    assert sorted(start.split()[2:]) == sorted(APP_UNITS)
    assert log.index(start) > upgrade.index("conference-drawing init")
    assert (upgrade.app / "venv").readlink() == Path(
        "venv-20261115T000000Z-bbbbbbbbbbbb"
    )


# --- uv --------------------------------------------------------------------------


def _uv_tarball(server: Server, *, good_hash: bool = True) -> None:
    """Ship a uv tarball whose `uv` reports the pinned version."""
    name = "uv-x86_64-unknown-linux-gnu"
    shim = f'#!/bin/sh\nexec "{FAKES / "uv"}" "$@"\n'.encode()
    path = server.release / f"{name}.tar.gz"
    with tarfile.open(path, "w:gz") as tar:
        for tool in ("uv", "uvx"):
            info = tarfile.TarInfo(f"{name}/{tool}")
            info.size, info.mode = len(shim), 0o755
            tar.addfile(info, io.BytesIO(shim))
    digest = _sha256(path) if good_hash else "0" * 64
    (server.release / "uv.sha256").write_text(f"{digest}  {name}.tar.gz\n")


def _old_uv(server: Server) -> None:
    bin_uv = server.root / "usr/local/bin/uv"
    bin_uv.unlink()
    bin_uv.write_text('#!/bin/sh\necho "uv 0.12.0"\n')
    bin_uv.chmod(0o755)


def test_install_replaces_a_uv_of_another_version(server: Server) -> None:
    _old_uv(server)
    _uv_tarball(server)
    result = server.install()
    assert result.returncode == 0, result.stderr
    assert str(FAKES / "uv") in (server.root / "usr/local/bin/uv").read_text()
    assert (server.root / "usr/local/bin/uvx").exists()


def test_install_refuses_a_uv_tarball_with_a_bad_checksum(server: Server) -> None:
    _old_uv(server)
    _uv_tarball(server, good_hash=False)
    result = server.install()
    assert result.returncode != 0
    assert "0.12.0" in (server.root / "usr/local/bin/uv").read_text()
    assert not any("uv venv" in line for line in server.log())


def test_install_refuses_the_wrong_uv_without_a_tarball(server: Server) -> None:
    _old_uv(server)
    result = server.install()
    assert result.returncode != 0
    assert "uv" in result.stderr


# --- symlinks the app user could plant ------------------------------------------

PLANTED = ["snapshots", "static", "media", "media/covers", ".maintenance.lock", ".env"]


@pytest.mark.parametrize("planted", PLANTED)
def test_install_refuses_symlinks_in_the_data_dir(server: Server, planted: str) -> None:
    sentinel = server.fakes.root / "sentinel"
    if planted in (".maintenance.lock", ".env"):
        sentinel.write_text("untouched\n")
        sentinel.chmod(0o640)
    else:
        sentinel.mkdir()
        sentinel.chmod(0o750)
    link = server.data / planted
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(sentinel)
    result = server.install(stdin="DRIP_API_KEY=key-1\n")
    assert result.returncode != 0
    assert "symbolic link" in result.stderr
    assert os_mode(sentinel) in (0o640, 0o750)
    if sentinel.is_file():
        assert sentinel.read_text() == "untouched\n"
    else:
        assert list(sentinel.iterdir()) == []


def test_install_does_not_chmod_through_a_symlink_inside_media(server: Server) -> None:
    sentinel = server.fakes.root / "sentinel"
    sentinel.write_text("x")
    sentinel.chmod(0o600)
    (server.data / "media" / "covers").mkdir(parents=True)
    (server.data / "media" / "covers" / "evil.png").symlink_to(sentinel)
    result = server.install()
    assert result.returncode == 0, result.stderr
    assert os_mode(sentinel) == 0o600


def test_root_scripts_never_chown_or_redirect_into_the_data_dir() -> None:
    """Root must not follow a link the app user planted: no bare chown, and no
    shell redirection onto a path under the data directory."""
    for name in [*ROOT_SCRIPTS, "common.sh"]:
        text = (DEPLOY / "remote" / name).read_text()
        for number, line in enumerate(text.splitlines(), 1):
            code = line.split("#", 1)[0] if not line.lstrip().startswith("#") else ""
            assert not re.search(r"\bchown\s+(?!-h\b)", code), f"{name}:{number}"
            assert not re.search(
                r">>?\s*\"?\$\{?(DATA_DIR|ENV_FILE|LOCK_FILE)", code
            ), f"{name}:{number}"
    assert "last_backup" not in (DEPLOY / "remote" / "backup.sh").read_text()


# --- restore ------------------------------------------------------------------


def _tarball(path: Path, entries: list[tarfile.TarInfo]) -> Path:
    with tarfile.open(path, "w:gz") as tar:
        for info in entries:
            tar.addfile(info, io.BytesIO(b"x" * info.size) if info.isfile() else None)
    return path


def _file(name: str) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.size = 1
    return info


def _link(name: str, target: str, kind: bytes) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type, info.linkname = kind, target
    return info


HOSTILE = {
    "symlink": [
        _file("db.sqlite3"),
        _link("media/x.png", "/etc/passwd", tarfile.SYMTYPE),
    ],
    "hardlink": [
        _file("db.sqlite3"),
        _link("media/x.png", "db.sqlite3", tarfile.LNKTYPE),
    ],
    "dotdot": [_file("db.sqlite3"), _file("media/../../evil")],
    "absolute": [_file("db.sqlite3"), _file("/etc/evil")],
    "other-name": [_file("db.sqlite3"), _file(".env")],
    "no-db": [_file("media/covers/c.png")],
}


@pytest.mark.parametrize("case", HOSTILE)
def test_check_restore_tarball_refuses_hostile_archives(
    fakes: Fakes, case: str
) -> None:
    path = _tarball(fakes.root / f"{case}.tar.gz", HOSTILE[case])
    script = 'source "$1"; check_restore_tarball "$2"'
    result = fakes.run(["-c", script, "_", COMMON, str(path)])
    assert result.returncode != 0, case


def test_check_restore_tarball_accepts_a_backup(fakes: Fakes) -> None:
    path = make_backup_tarball(fakes.root / "canned" / "backup.tar.gz")
    script = 'source "$1"; check_restore_tarball "$2"'
    result = fakes.run(["-c", script, "_", COMMON, str(path)])
    assert result.returncode == 0, result.stderr


def test_install_restore_loads_the_database_and_media(server: Server) -> None:
    backup = make_backup_tarball(server.fakes.root / "canned" / "backup.tar.gz")
    shutil.copy(backup, server.release / "restore.tar.gz")
    result = server.install("--restore", str(server.release / "restore.tar.gz"))
    assert result.returncode == 0, result.stderr
    assert (server.data / "db.sqlite3").exists()
    assert os_mode(server.data / "media" / "covers" / "c.png") == 0o644


# --- uninstall -----------------------------------------------------------------


def test_uninstall_stops_units_then_removes_everything(upgrade: Server) -> None:
    for unit in [*APP_UNITS, "conference-drawing-snapshot.service"]:
        (upgrade.root / "etc/systemd/system" / unit).write_text("")
    (upgrade.root / "etc/caddy/sites/conference-drawing.caddy").write_text("x\n")
    upgrade.fakes.prepend("systemctl", {"match": "is-active", "exit": 3})
    result = upgrade.uninstall()
    assert result.returncode == 0, result.stderr
    assert not upgrade.app.exists()
    assert list((upgrade.root / "etc/systemd/system").iterdir()) == []
    assert list((upgrade.root / "etc/caddy/sites").iterdir()) == []
    assert upgrade.index("flock --wait") < upgrade.index("systemctl disable --now")
    assert upgrade.log()[-1] == "userdel conference-drawing"


def test_uninstall_refuses_while_a_unit_still_runs(upgrade: Server) -> None:
    result = upgrade.uninstall()  # is-active answers 0: still running
    assert result.returncode != 0
    assert upgrade.app.exists()
