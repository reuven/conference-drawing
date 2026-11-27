"""remote/install.sh and remote/uninstall.sh, run whole in a temp root.

``CD_ROOT`` points every server path into a temp dir, and fakes stand in for
everything privileged: ``id`` says root, ``runuser`` runs shell helpers as the
current user (and fakes the app's own commands), ``chown``, ``useradd``,
``userdel``, ``systemctl``, ``caddy``, ``flock``, ``uv``, ``apt-get``,
``dpkg-query``, ``psql``, ``pg_dump`` and ``pg_restore`` are canned. No real
server step runs on this machine, and no real database is touched.
"""

import hashlib
import io
import json
import re
import shutil
import subprocess
import tarfile
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.deploy.conftest import (
    COMMIT,
    DEPLOY,
    DOMAIN,
    DUMP_BYTES,
    FAKES,
    Entry,
    Fakes,
    make_backup_tarball,
    make_dump,
    os_mode,
)

COMMON = str(DEPLOY / "remote" / "common.sh")
# The socket comes first: while it listens, any request starts the web app, so
# every stop must include it (a migration or a teardown must not wake the app).
APP_UNITS = [
    "conference-drawing-web.socket",
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
    "owner_password.sh",
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

    def stop(self) -> subprocess.CompletedProcess[str]:
        return self.fakes.run([str(self.release / "remote" / "stop.sh")])

    def log(self) -> list[str]:
        """Every fake call as ``tool args``, in order."""
        return [f"{c.tool} {c.key}" for c in self.fakes.calls()]

    def index(self, needle: str) -> int:
        return next(i for i, line in enumerate(self.log()) if needle in line)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


PG_MARKER = "srv/conference-drawing/.installed-postgresql"
PG_DROPIN = "etc/postgresql/16/main/conf.d/conference-drawing.conf"


APT_INSTALL = (
    "apt-get install -y -q --no-install-recommends -o DPkg::Lock::Timeout=300 "
    "postgresql"
)
PURGE = "apt-get purge -y -q postgresql postgresql-16"
CLUSTER_16 = (
    "16  main    5432 online postgres /var/lib/postgresql/16/main "
    "/var/log/postgresql/postgresql-16-main.log\n"
)


def _dpkg(*packages: str) -> list[Entry]:
    """dpkg-query's answer for the versioned server packages installed."""
    if not packages:
        return [{"match": "postgresql-[0-9]*", "exit": 1}]
    lines = "".join(f"{p} install ok installed\n" for p in packages)
    return [{"match": "postgresql-[0-9]*", "stdout": lines}]


def _canned(active: bool, dump: Path, root: Path) -> dict[str, list[Entry]]:
    """Responses for a server whose PostgreSQL 16 exists only if `active`."""
    return {
        "dpkg-query": _dpkg("postgresql-16") if active else _dpkg(),
        "apt-get": [
            {"match": "update -q"},
            {
                "match": APT_INSTALL.removeprefix("apt-get "),
                "exact": True,
                # What Ubuntu's package leaves: one cluster, 16/main.
                "mkdirs": [str(root / "etc/postgresql/16/main/conf.d")],
            },
            {"match": PURGE.removeprefix("apt-get "), "exact": True},
        ],
        "pg_lsclusters": [{"match": "--no-header", "stdout": CLUSTER_16}],
        "psql": [
            {"match": "SHOW server_version_num", "stdout": "160004\n"},
            {"match": "SHOW listen_addresses", "stdout": "localhost\n"},
            {"match": "pg_tables", "stdout": "14\n" if active else "0\n"},
            {"match": "-f -", "stdin_match": "datdba", "stdout": ""},
            {"match": "FROM pg_database WHERE", "stdout": ""},
            {"match": "--dbname=postgres", "stdout": "0\n"},
            {"match": "--dbname=template1", "stdout": "0\n"},
            {"match": "FROM pg_roles", "stdout": ""},
            {"match": "-v ON_ERROR_STOP=1"},
        ],
        "pg_dump": [{"match": "-Fc", "copy_to_flag": "--file", "file": str(dump)}],
        "pg_restore": [{"match": "", "require_magic": "PGDMP"}],
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
            {"match": "restart postgresql"},
        ],
        "runuser": [
            {
                "match": "/bin/conference-drawing init",
                "ensure_env_line": "DJANGO_SECRET_KEY='fake'",
                # The app must use the distribution's socket, not libpq's
                # compiled-in default (the binary wheel's may differ).
                "require_env": {"CD_DB_HOST": "/var/run/postgresql"},
            },
            {
                "match": "/bin/conference-drawing collectstatic",
                "require_env_key": "DJANGO_SECRET_KEY",
            },
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
    make_dump(fakes.root / "canned" / "db.dump")
    fakes.responses.update(_canned(active=False, dump=_canned_dump(fakes), root=root))
    return Server(fakes, root, release)


def _canned_dump(fakes: Fakes) -> Path:
    return fakes.root / "canned" / "db.dump"


@pytest.fixture
def upgrade(server: Server) -> Server:
    """A running install: data, database tables, two old venvs, units active."""
    server.data.mkdir(parents=True)
    (server.data / ".env").write_text("DJANGO_SECRET_KEY='x'\n")
    (server.app / ".maintenance.lock").touch()
    for name in (
        "venv-20261101T000000Z-aaaaaaaaaaaa",
        "venv-20261115T000000Z-bbbbbbbbbbbb",
    ):
        (server.app / name / "bin").mkdir(parents=True)
    (server.app / "venv").symlink_to("venv-20261115T000000Z-bbbbbbbbbbbb")
    (server.root / PG_MARKER).write_text("16\n")
    (server.root / "etc/postgresql/16/main/conf.d").mkdir(parents=True)
    server.fakes.responses.update(
        _canned(active=True, dump=_canned_dump(server.fakes), root=server.root)
    )
    return server


def _venvs(server: Server) -> list[str]:
    return sorted(p.name for p in server.app.glob("venv-*"))


# --- a fresh install ----------------------------------------------------------


def test_install_fresh_lays_out_the_server(server: Server) -> None:
    result = server.install(stdin="DRIP_API_KEY=key-1=\nDRIP_ACCOUNT_ID=42\n")
    assert result.returncode == 0, result.stderr
    assert os_mode(server.data) == 0o711
    assert os_mode(server.data / "snapshots") == 0o700
    for sub in ("static", "media", "media/covers", "media/logos"):
        assert os_mode(server.data / sub) == 0o755, sub
    env = server.data / ".env"
    assert env.read_text() == (
        "DRIP_API_KEY='key-1='\nDRIP_ACCOUNT_ID='42'\nDJANGO_SECRET_KEY='fake'\n"
    )
    assert os_mode(env) == 0o600
    lock = server.app / ".maintenance.lock"
    assert os_mode(lock) == 0o640
    assert f"chown -h root:conference-drawing {lock}" in server.log()
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


def test_fresh_install_with_drip_keys_runs_init_before_collectstatic(
    server: Server,
) -> None:
    # .env exists (Drip keys) but has no secret key yet: still a fresh install.
    result = server.install(stdin="DRIP_API_KEY=key-1\n")
    assert result.returncode == 0, result.stderr
    assert server.index("conference-drawing init") < server.index(
        "conference-drawing collectstatic"
    )
    assert not any(line.startswith("systemctl stop") for line in server.log())


def test_install_takes_the_lock_before_building_the_venv_or_touching_units(
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


# --- DEBUG must never be on a server --------------------------------------------

DEBUG_ON = [
    "DJANGO_DEBUG=1",
    "DJANGO_DEBUG=true",
    "DJANGO_DEBUG=True",
    "DJANGO_DEBUG='yes'",
    'DJANGO_DEBUG="ON"',
    "export DJANGO_DEBUG=1",
    "  DJANGO_DEBUG = 1",
    "DJANGO_DEBUG=0\nDJANGO_DEBUG=1",
]
DEBUG_OFF = [
    "DJANGO_DEBUG=0",
    "DJANGO_DEBUG=false",
    "DJANGO_DEBUG=",
    "# DJANGO_DEBUG=1",
]


def _write_env(server: Server, text: str) -> None:
    server.data.mkdir(parents=True)
    (server.data / ".env").write_text(f"DJANGO_SECRET_KEY='do-not-print-me'\n{text}\n")


@pytest.mark.parametrize("line", DEBUG_ON)
def test_install_refuses_a_server_env_with_debug_on(server: Server, line: str) -> None:
    _write_env(server, line)
    result = server.install()
    assert result.returncode != 0
    assert "DJANGO_DEBUG" in result.stderr
    assert "do-not-print-me" not in result.stderr + result.stdout
    # Refused before anything changed, and the file is left for the operator.
    assert not any(c.startswith(("useradd", "apt-get")) for c in server.log())
    assert not _venvs(server)
    assert line.split("\n")[-1] in (server.data / ".env").read_text()


@pytest.mark.parametrize("line", DEBUG_OFF)
def test_install_accepts_a_server_env_with_debug_off(server: Server, line: str) -> None:
    _write_env(server, line)
    assert server.install().returncode == 0


def test_install_is_idempotent_for_drip_keys(server: Server) -> None:
    stdin = "DRIP_API_KEY=key-1\n"
    assert server.install(stdin=stdin).returncode == 0
    assert server.install(stdin=stdin).returncode == 0
    assert (server.data / ".env").read_text() == (
        "DRIP_API_KEY='key-1'\nDJANGO_SECRET_KEY='fake'\n"
    )


def test_install_replaces_a_changed_drip_key_in_place(server: Server) -> None:
    server.data.mkdir(parents=True)
    env = server.data / ".env"
    env.write_text(
        "DJANGO_SECRET_KEY='keep'\nDRIP_API_KEY='old-key'\nDRIP_ACCOUNT_ID='42'\n"
    )
    result = server.install(stdin="DRIP_API_KEY=new-key\nDRIP_ACCOUNT_ID=42\n")
    assert result.returncode == 0, result.stderr
    assert env.read_text() == (
        "DJANGO_SECRET_KEY='keep'\nDRIP_API_KEY='new-key'\nDRIP_ACCOUNT_ID='42'\n"
    )
    assert os_mode(env) == 0o600
    assert "wrote DRIP_API_KEY" in result.stderr
    assert "wrote DRIP_ACCOUNT_ID" not in result.stderr
    assert "old-key" not in result.stderr + result.stdout
    assert "new-key" not in result.stderr + result.stdout
    assert [p.name for p in server.data.glob(".env*")] == [".env"]  # no temp left


def test_install_leaves_an_unchanged_env_byte_for_byte(server: Server) -> None:
    server.data.mkdir(parents=True)
    env = server.data / ".env"
    # No trailing newline, an odd line: a rewrite would normalize them.
    env.write_bytes(b"DRIP_API_KEY='same'\nDJANGO_SECRET_KEY='k'   ")
    before = env.stat()
    result = server.install(stdin="DRIP_API_KEY=same\n")
    assert result.returncode == 0, result.stderr
    assert env.read_bytes() == b"DRIP_API_KEY='same'\nDJANGO_SECRET_KEY='k'   "
    assert env.stat().st_ino == before.st_ino
    assert "wrote" not in result.stderr


def test_install_replaces_every_stale_copy_of_a_drip_key(server: Server) -> None:
    server.data.mkdir(parents=True)
    env = server.data / ".env"
    env.write_text("DJANGO_SECRET_KEY='k'\nDRIP_API_KEY='a'\nDRIP_API_KEY='b'\n")
    assert server.install(stdin="DRIP_API_KEY=b\n").returncode == 0
    assert env.read_text() == (
        "DJANGO_SECRET_KEY='k'\nDRIP_API_KEY='b'\nDRIP_API_KEY='b'\n"
    )


def test_install_replaces_a_value_that_needs_escaping(server: Server) -> None:
    server.data.mkdir(parents=True)
    env = server.data / ".env"
    env.write_text("DJANGO_SECRET_KEY='k'\nDRIP_API_KEY='old'\n")
    assert server.install(stdin="DRIP_API_KEY=a\\b'c&d\n").returncode == 0
    assert env.read_text() == "DJANGO_SECRET_KEY='k'\nDRIP_API_KEY='a\\\\b\\'c&d'\n"


# --- PostgreSQL ------------------------------------------------------------------


def _psql_calls(server: Server, user: str) -> list[str]:
    """The argv of each runuser call that ran psql as `user`."""
    return [
        " ".join(c.argv)
        for c in server.fakes.calls("runuser")
        if c.argv[:2] == ["-u", user] and "psql" in c.argv
    ]


def _admin_sql(server: Server) -> str:
    """Everything the scripts fed psql on stdin (the admin SQL)."""
    return "".join(c.stdin for c in server.fakes.calls("psql"))


def test_fresh_install_installs_postgresql_and_records_that_it_did(
    server: Server,
) -> None:
    result = server.install()
    assert result.returncode == 0, result.stderr
    log = server.log()
    assert log.index("apt-get update -q -o DPkg::Lock::Timeout=300") < log.index(
        APT_INSTALL
    )
    assert (server.root / PG_MARKER).read_text() == "16\n"
    dropin = (server.root / PG_DROPIN).read_text()
    for setting in (
        "listen_addresses = ''",
        "shared_buffers = 64MB",
        "max_connections = 20",
    ):
        assert f"\n{setting}\n" in dropin, setting
    assert os_mode(server.root / PG_DROPIN) == 0o644
    restart = server.index("systemctl restart postgresql")
    assert server.index("flock --wait") < server.index("apt-get install")
    assert restart < server.index("runuser -u postgres -- psql")
    assert "listens on" not in result.stderr  # our own server: no warning


def test_install_creates_the_role_and_database_through_psql_variables(
    server: Server,
) -> None:
    assert server.install().returncode == 0
    admin = next(
        " ".join(c.argv)
        for c in server.fakes.calls("runuser")
        if c.argv[:2] == ["-u", "postgres"] and "-f" in c.argv
    )
    assert admin.startswith("-u postgres -- psql -X ")
    assert "-v ON_ERROR_STOP=1" in admin
    assert "-v role=conference-drawing" in admin
    assert "-v db=conference_drawing" in admin
    sql = _admin_sql(server)
    # Identifiers only as :"role" / :"db" (psql quotes them), never spliced in.
    assert "conference-drawing" not in sql and "conference_drawing" not in sql
    assert 'CREATE ROLE :"role" LOGIN;' in sql
    assert 'CREATE DATABASE :"db" OWNER :"role";' in sql
    assert "\\if :need_role" in sql and "\\if :need_db" in sql  # idempotent
    # Peer auth only: the role never has a password.
    assert (
        'ALTER ROLE :"role" WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE '
        "PASSWORD NULL;" in sql
    )
    assert 'REVOKE ALL ON DATABASE :"db" FROM PUBLIC;' in sql
    assert "PASSWORD '" not in sql


def _existing_server(server: Server, major: str) -> None:
    """Someone else's PostgreSQL `major` is already on the server."""
    (server.root / f"etc/postgresql/{major}/main/conf.d").mkdir(parents=True)
    server.fakes.set("dpkg-query", _dpkg(f"postgresql-{major}"))
    server.fakes.prepend(
        "psql", {"match": "SHOW server_version_num", "stdout": f"{major}0004\n"}
    )
    server.fakes.prepend(
        "psql", {"match": "SHOW listen_addresses", "stdout": "localhost, 10.0.0.5\n"}
    )


@pytest.mark.parametrize("major", ["16", "17"])
def test_install_never_claims_an_existing_postgresql(
    server: Server, major: str
) -> None:
    """postgresql-16 without the metapackage, or PGDG's postgresql-17: either way
    the server is someone else's. No apt, no marker, no drop-in, no restart."""
    _existing_server(server, major)
    result = server.install()
    assert result.returncode == 0, result.stderr
    log = server.log()
    assert not any(line.startswith("apt-get") for line in log)
    assert not (server.root / PG_MARKER).exists()
    assert list((server.root / f"etc/postgresql/{major}/main/conf.d").iterdir()) == []
    assert not any("restart postgresql" in line for line in log)
    # It still gets our role and database, and a warning, nothing changed.
    assert 'CREATE DATABASE :"db"' in _admin_sql(server)
    assert "warning" in result.stderr
    assert "listens on 'localhost, 10.0.0.5'" in result.stderr


def test_install_refuses_a_postgresql_older_than_15(server: Server) -> None:
    _existing_server(server, "14")
    result = server.install()
    assert result.returncode != 0
    assert "15 or later" in result.stderr
    assert "PostgreSQL 14" in result.stderr
    assert 'CREATE ROLE :"role"' not in _admin_sql(server)
    assert not any("conference-drawing init" in line for line in server.log())


def test_install_refuses_a_database_owned_by_another_role(server: Server) -> None:
    server.fakes.prepend(
        "psql", {"match": "-f -", "stdin_match": "datdba", "stdout": "wordpress\n"}
    )
    result = server.install()
    assert result.returncode != 0
    assert "owned by role wordpress" in result.stderr
    sql = _admin_sql(server)
    assert "CREATE ROLE" not in sql and "ALTER" not in sql and "REVOKE" not in sql


def test_install_restarts_postgresql_only_when_its_dropin_changes(
    server: Server,
) -> None:
    assert server.install().returncode == 0
    server.fakes.log.unlink()
    server.fakes.set("dpkg-query", _dpkg("postgresql-16"))
    assert server.install().returncode == 0
    log = server.log()
    assert "systemctl restart postgresql" not in log
    assert not any(line.startswith("apt-get") for line in log)
    assert (server.root / PG_MARKER).exists()  # still ours


def test_install_refuses_a_symlinked_postgresql_dropin(server: Server) -> None:
    # A server this install set up earlier (the marker), whose drop-in is a link.
    _existing_server(server, "16")
    server.app.mkdir(parents=True)
    (server.root / PG_MARKER).write_text("16\n")
    sentinel = server.fakes.root / "sentinel"
    sentinel.write_text("untouched\n")
    (server.root / PG_DROPIN).symlink_to(sentinel)
    result = server.install()
    assert result.returncode != 0
    assert "symbolic link" in result.stderr
    assert sentinel.read_text() == "untouched\n"


def test_install_fails_when_the_role_cannot_be_created(server: Server) -> None:
    server.fakes.prepend(
        "psql", {"match": "-f -", "stdin_match": "CREATE ROLE", "exit": 3}
    )
    result = server.install()
    assert result.returncode != 0
    assert not any("conference-drawing init" in line for line in server.log())


@pytest.mark.parametrize(
    "unit",
    [
        "conference-drawing-web.service",
        "conference-drawing-drip.service",
        "conference-drawing-snapshot.service",
    ],
)
def test_units_need_postgresql(unit: str) -> None:
    lines = (DEPLOY / "systemd" / unit).read_text().splitlines()
    after = [line for line in lines if line.startswith("After=")]
    assert any("postgresql.service" in line.split("=", 1)[1].split() for line in after)
    assert "Requires=postgresql.service" in lines
    # The distribution's socket, not libpq's compiled-in default.
    assert "Environment=CD_DB_HOST=/var/run/postgresql" in lines


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
    (dump,) = (upgrade.data / "snapshots").glob("db-*.dump")
    assert dump.read_bytes() == DUMP_BYTES
    # The snapshot ran as the app user (peer auth), holding the lock already.
    as_app = upgrade.index("runuser -u conference-drawing -- env CD_LOCK_HELD=1")
    assert as_app == snapshot < upgrade.index("pg_dump -Fc") < migrate
    assert "systemctl restart postgresql" in upgrade.log()  # no drop-in yet
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
    install_log = upgrade.root / "var/log/conference-drawing-install.log"
    assert os_mode(install_log) == 0o600
    text = install_log.read_text()
    assert "restarted" in text
    assert "partly migrated" in text
    (snapshot,) = (upgrade.data / "snapshots").glob("db-*.dump")
    assert (
        "runuser -u postgres -- dropdb --force conference_drawing && "
        "runuser -u postgres -- createdb --owner=conference-drawing "
        "conference_drawing && "
        "runuser -u conference-drawing -- pg_restore --no-owner "
        "--role=conference-drawing --exit-on-error --single-transaction "
        f"--dbname=conference_drawing {upgrade.data / 'snapshots' / snapshot.name}"
    ) in text
    assert "sqlite" not in text


def test_fresh_install_failing_at_migrate_gives_no_restore_advice(
    server: Server,
) -> None:
    """No tables, so no snapshot was taken, so there is nothing to put back."""
    server.fakes.prepend("runuser", {"match": "conference-drawing init", "exit": 1})
    result = server.install()
    assert result.returncode != 0
    assert not any("pg_dump" in line for line in server.log())
    text = (server.root / "var/log/conference-drawing-install.log").read_text()
    assert "install failed" in text
    assert "pg_restore" not in text
    assert "no pre-migrate snapshot" in text


def test_upgrade_restarts_units_even_after_the_ssh_session_drops(
    upgrade: Server,
) -> None:
    """Close the output pipe mid-run, as a dropped ssh connection does: the
    trap must still start the units this run stopped."""
    upgrade.fakes.prepend("runuser", {"match": "conference-drawing init", "exit": 1})
    fakes = upgrade.fakes
    (fakes.root / "responses.json").write_text(json.dumps(fakes.responses))
    script = str(upgrade.release / "remote" / "install.sh")
    process = subprocess.Popen(  # noqa: S603 - fixed test inputs
        [str(fakes.bin / "bash"), script, DOMAIN],
        env=fakes.env(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=fakes.root,
    )
    assert process.stdout is not None
    for raw in process.stdout:
        if b"switching to the new release" in raw:
            break
    process.stdout.close()  # the reader is gone
    assert process.wait(timeout=60) != 0
    time.sleep(0.2)
    log = upgrade.log()
    assert any(line.startswith("systemctl stop") for line in log)
    (start,) = [line for line in log if line.startswith("systemctl start")]
    assert sorted(start.split()[2:]) == sorted(APP_UNITS)
    text = (upgrade.root / "var/log/conference-drawing-install.log").read_text()
    assert "restarted" in text


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

PLANTED = ["snapshots", "static", "media", "media/covers", "media/logos", ".env"]


@pytest.mark.parametrize("planted", PLANTED)
def test_install_refuses_symlinks_in_the_data_dir(server: Server, planted: str) -> None:
    sentinel = server.fakes.root / "sentinel"
    if planted == ".env":
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


def test_install_refuses_a_symlinked_lock_file(server: Server) -> None:
    sentinel = server.fakes.root / "sentinel"
    sentinel.write_text("untouched\n")
    sentinel.chmod(0o600)
    server.app.mkdir(parents=True)
    (server.app / ".maintenance.lock").symlink_to(sentinel)
    result = server.install()
    assert result.returncode != 0
    assert "symbolic link" in result.stderr
    assert os_mode(sentinel) == 0o600
    assert not any(
        line.startswith("chown") and "sentinel" in line for line in server.log()
    )


def test_as_app_closes_the_lock_descriptor(fakes: Fakes) -> None:
    """Root holds the lock on fd 9; no app-user process may inherit it."""
    fakes.set("runuser", [{"match": "", "passthrough": True}])
    lock = fakes.root / "lock"
    lock.touch()
    script = 'source "$1"; exec 9<"$2"; ls /dev/fd; echo ---; as_app ls /dev/fd'
    result = fakes.run(["-c", script, "_", COMMON, str(lock)])
    assert result.returncode == 0, result.stderr
    root_fds, app_fds = result.stdout.split("---")
    assert "9" in root_fds.split()  # the probe can see fd 9
    assert "9" not in app_fds.split()


def test_lock_lives_in_roots_app_dir() -> None:
    common = (DEPLOY / "remote" / "common.sh").read_text()
    assert 'LOCK_FILE="$APP_DIR/.maintenance.lock"\n' in common
    for path in [*DEPLOY.rglob("*.sh"), *DEPLOY.rglob("*.service")]:
        assert "data/.maintenance.lock" not in path.read_text(), path
    # The snapshot service flocks the root-owned file read-only: it needs no
    # write access outside the data directory.
    service = (DEPLOY / "systemd" / "conference-drawing-snapshot.service").read_text()
    assert "ReadWritePaths=/srv/conference-drawing/data\n" in service


def test_remote_backup_reads_the_database_as_the_app_user(upgrade: Server) -> None:
    (upgrade.data / "media" / "covers").mkdir(parents=True)
    (upgrade.data / "media" / "covers" / "c.png").write_bytes(b"png")
    (upgrade.data / "snapshots").mkdir()
    result = upgrade.fakes.run([str(upgrade.release / "remote" / "backup.sh")])
    assert result.returncode == 0, result.stderr
    tarball = Path(result.stdout.splitlines()[-1])
    with tarfile.open(tarball) as tar:
        names = tar.getnames()
        dump = tar.extractfile("db.dump")
        assert dump is not None and dump.read() == DUMP_BYTES
    assert "media/covers/c.png" in names
    log = upgrade.log()
    dump_cmd = upgrade.index("runuser -u conference-drawing -- pg_dump -Fc --file")
    verify = upgrade.index("runuser -u conference-drawing -- pg_restore -f /dev/null")
    tar_cmd = upgrade.index("runuser -u conference-drawing -- tar -czf -")
    assert upgrade.index("flock --wait 300") < dump_cmd < verify < tar_cmd
    assert "--dbname=conference_drawing" in log[dump_cmd]
    assert list((upgrade.data / "snapshots").iterdir()) == []
    shutil.rmtree(tarball.parent)


def test_remote_backup_refuses_a_dump_that_does_not_read_back(
    upgrade: Server,
) -> None:
    make_dump(_canned_dump(upgrade.fakes), b"truncated")
    (upgrade.data / "snapshots").mkdir()
    result = upgrade.fakes.run([str(upgrade.release / "remote" / "backup.sh")])
    assert result.returncode != 0
    assert "verification" in result.stderr
    assert not any("tar -czf" in line for line in upgrade.log())
    _assert_no_backup_left(upgrade)


def _assert_no_backup_left(server: Server) -> None:
    """A failed backup leaves no dump (personal data) and no partial tarball."""
    assert list((server.data / "snapshots").iterdir()) == []
    leftovers = list((server.root / "tmp").glob("conference-drawing-backup.*"))
    assert leftovers == []


def test_remote_backup_removes_its_dump_when_tar_fails(upgrade: Server) -> None:
    (upgrade.data / "snapshots").mkdir()
    upgrade.fakes.prepend("runuser", {"match": "tar -czf", "exit": 2})
    result = upgrade.fakes.run([str(upgrade.release / "remote" / "backup.sh")])
    assert result.returncode != 0
    _assert_no_backup_left(upgrade)


def test_install_does_not_chmod_through_a_symlink_inside_media(server: Server) -> None:
    sentinel = server.fakes.root / "sentinel"
    sentinel.write_text("x")
    sentinel.chmod(0o600)
    (server.data / "media" / "covers").mkdir(parents=True)
    (server.data / "media" / "covers" / "evil.png").symlink_to(sentinel)
    result = server.install()
    assert result.returncode == 0, result.stderr
    assert os_mode(sentinel) == 0o600


def test_root_scripts_never_chown_or_redirect_into_app_user_paths() -> None:
    """Root must not follow a link the app user planted: no bare chown, and no
    shell redirection onto a path under the data directory."""
    for name in [*ROOT_SCRIPTS, "common.sh"]:
        text = (DEPLOY / "remote" / name).read_text()
        for number, line in enumerate(text.splitlines(), 1):
            code = line.split("#", 1)[0] if not line.lstrip().startswith("#") else ""
            assert not re.search(r"\bchown\s+(?!-h\b)", code), f"{name}:{number}"
            assert not re.search(
                r">>?\s*\"?\$\{?(DATA_DIR|ENV_FILE|LOCK_FILE|APP_DIR)", code
            ), f"{name}:{number}"
            # Root reaches the database only as the app user (as_app) or,
            # for the role and database themselves, as postgres (pg_admin).
            # (Quoted strings are messages and advice, not commands.)
            unquoted = re.sub(r'"[^"]*"', '""', code)
            if re.search(r"(^|[\s;|&(])(pg_dump|pg_restore|psql)\s", unquoted):
                assert re.search(
                    r"\b(as_app|pg_admin)\b|runuser -u postgres -- ", code
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
        _file("db.dump"),
        _link("media/x.png", "/etc/passwd", tarfile.SYMTYPE),
    ],
    "hardlink": [
        _file("db.dump"),
        _link("media/x.png", "db.dump", tarfile.LNKTYPE),
    ],
    "dotdot": [_file("db.dump"), _file("media/../../evil")],
    "absolute": [_file("db.dump"), _file("/etc/evil")],
    "other-name": [_file("db.dump"), _file(".env")],
    "no-db": [_file("media/covers/c.png")],
    "old-sqlite-backup": [_file("db.sqlite3"), _file("media/covers/c.png")],
    "symlinked-dump": [_link("db.dump", "/etc/shadow", tarfile.SYMTYPE)],
}


def test_check_restore_tarball_explains_an_old_sqlite_backup(fakes: Fakes) -> None:
    entries = [_file("db.sqlite3"), _file("media/covers/c.png")]
    path = _tarball(fakes.root / "old.tar.gz", entries)
    script = 'source "$1"; check_restore_tarball "$2"'
    result = fakes.run(["-c", script, "_", COMMON, str(path)])
    assert result.returncode != 0
    assert (
        "from the SQLite version of conference-drawing and cannot be restored "
        "into PostgreSQL" in result.stderr
    )


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
    assert os_mode(server.data / "media" / "covers" / "c.png") == 0o644
    log = server.log()
    restore = server.index("runuser -u conference-drawing -- pg_restore --clean")
    assert log[restore].endswith(
        "pg_restore --clean --if-exists --no-owner --role=conference-drawing "
        "--exit-on-error --single-transaction --dbname=conference_drawing "
        f"{server.data / 'db.dump'}"
    )
    # Into the fresh database: after the role and database exist, before migrate.
    assert server.index("runuser -u postgres -- psql") < restore
    assert restore < server.index("conference-drawing init")
    assert server.index("pg_restore -f /dev/null") < restore  # read fully first
    assert not (server.data / "db.dump").exists()  # loaded, then removed


def test_install_restore_refuses_a_database_that_already_has_tables(
    server: Server,
) -> None:
    server.fakes.prepend("psql", {"match": "pg_tables", "stdout": "3\n"})
    backup = make_backup_tarball(server.fakes.root / "canned" / "backup.tar.gz")
    result = server.install("--restore", str(backup))
    assert result.returncode != 0
    assert "already has tables" in result.stderr
    assert not any("pg_restore" in line for line in server.log())


def test_install_restore_refuses_a_dump_that_does_not_read_back(
    server: Server,
) -> None:
    import tarfile as tf

    work = server.fakes.root / "bad"
    make_dump(work / "db.dump", b"not a dump")
    bad = server.fakes.root / "bad.tar.gz"
    with tf.open(bad, "w:gz") as tar:
        tar.add(work / "db.dump", arcname="db.dump")
    result = server.install("--restore", str(bad))
    assert result.returncode != 0
    assert "failed verification" in result.stderr
    assert not any("pg_restore --clean" in line for line in server.log())
    assert not (server.data / "db.dump").exists()  # personal data, cleaned up


def test_install_restore_removes_the_dump_when_loading_fails(server: Server) -> None:
    server.fakes.prepend("pg_restore", {"match": "--clean", "exit": 1})
    backup = make_backup_tarball(server.fakes.root / "canned" / "backup.tar.gz")
    result = server.install("--restore", str(backup))
    assert result.returncode != 0
    assert "the restore failed" in result.stderr
    assert not (server.data / "db.dump").exists()
    assert not any("conference-drawing init" in line for line in server.log())


def test_install_restore_removes_the_dump_when_extracting_fails(
    server: Server,
) -> None:
    """tar writes db.dump first, then fails on a later member."""
    blocker = server.data / "media" / "covers" / "c.png"
    (blocker / "inside").mkdir(parents=True)  # a directory where a file goes
    backup = make_backup_tarball(server.fakes.root / "canned" / "backup.tar.gz")
    result = server.install("--restore", str(backup))
    assert result.returncode != 0
    assert not (server.data / "db.dump").exists()
    assert not any("pg_restore" in line for line in server.log())


# --- stop (down.sh's quiet step) ------------------------------------------------

WEB_QUIET = (
    "conference-drawing-snapshot.timer conference-drawing-web.socket "
    "conference-drawing-web.service"
)


def test_stop_quiets_the_socket_with_the_web_app_before_the_outbox_check(
    upgrade: Server,
) -> None:
    upgrade.fakes.prepend("runuser", {"match": "outbox_status"})
    result = upgrade.stop()
    assert result.returncode == 0, result.stderr
    log = upgrade.log()
    assert log.index(f"systemctl stop {WEB_QUIET}") < upgrade.index("outbox_status")
    assert log[-1] == "systemctl stop conference-drawing-drip.service"


def test_stop_starts_the_socket_again_when_a_drip_job_is_pending(
    upgrade: Server,
) -> None:
    upgrade.fakes.prepend("runuser", {"match": "outbox_status", "exit": 1})
    result = upgrade.stop()
    assert result.returncode != 0
    assert f"systemctl start {WEB_QUIET}" in upgrade.log()
    assert "systemctl stop conference-drawing-drip.service" not in upgrade.log()


# --- uninstall -----------------------------------------------------------------


def test_uninstall_stops_units_then_removes_everything(upgrade: Server) -> None:
    for unit in [*APP_UNITS, "conference-drawing-snapshot.service"]:
        (upgrade.root / "etc/systemd/system" / unit).write_text("")
    (upgrade.root / "etc/caddy/sites/conference-drawing.caddy").write_text("x\n")
    (upgrade.root / PG_DROPIN).write_text("listen_addresses = ''\n")
    upgrade.fakes.prepend("systemctl", {"match": "is-active", "exit": 3})
    result = upgrade.uninstall()
    assert result.returncode == 0, result.stderr
    assert not upgrade.app.exists()
    assert list((upgrade.root / "etc/systemd/system").iterdir()) == []
    assert list((upgrade.root / "etc/caddy/sites").iterdir()) == []
    assert upgrade.index("flock --wait") < upgrade.index("systemctl disable --now")
    assert upgrade.log()[-1] == "userdel conference-drawing"
    sql = _admin_sql(upgrade)
    assert 'DROP DATABASE IF EXISTS :"db" WITH (FORCE);' in sql
    assert 'DROP ROLE IF EXISTS :"role";' in sql
    assert upgrade.index("runuser -u postgres -- psql") > upgrade.index(
        "systemctl disable --now"
    )
    # This install put PostgreSQL there and nothing else uses it: it goes too,
    # after every check (one cluster, no other database, relation or role).
    purge = upgrade.index(PURGE)
    for check in (
        "pg_lsclusters",
        "FROM pg_database WHERE",
        "--dbname=postgres",
        "--dbname=template1",
        "FROM pg_roles",
    ):
        assert upgrade.index(check) < purge, check
    assert purge < upgrade.index("userdel")
    assert not (upgrade.root / PG_DROPIN).exists()


def _stopped(upgrade: Server) -> Server:
    upgrade.fakes.prepend("systemctl", {"match": "is-active", "exit": 3})
    return upgrade


TWO_CLUSTERS = CLUSTER_16 + CLUSTER_16.replace(" main ", " other ")

PURGE_BLOCKERS: dict[str, tuple[str, Entry]] = {
    "two clusters": ("pg_lsclusters", {"match": "--no-header", "stdout": TWO_CLUSTERS}),
    "not main": (
        "pg_lsclusters",
        {"match": "--no-header", "stdout": CLUSTER_16.replace(" main ", " web ")},
    ),
    "other major": (
        "pg_lsclusters",
        {"match": "--no-header", "stdout": CLUSTER_16.replace("16 ", "17 ", 1)},
    ),
    "no clusters listed": ("pg_lsclusters", {"match": "--no-header", "stdout": ""}),
    "pg_lsclusters fails": ("pg_lsclusters", {"match": "--no-header", "exit": 1}),
    "other database": (
        "psql",
        {"match": "FROM pg_database WHERE", "stdout": "someone_else\n"},
    ),
    "tables in postgres": ("psql", {"match": "--dbname=postgres", "stdout": "3\n"}),
    "tables in template1": ("psql", {"match": "--dbname=template1", "stdout": "1\n"}),
    "other role": ("psql", {"match": "FROM pg_roles", "stdout": "wordpress\n"}),
    "a query fails": ("psql", {"match": "FROM pg_roles", "exit": 2}),
    "a count is not a number": (
        "psql",
        {"match": "--dbname=postgres", "stdout": "\n"},
    ),
}


def test_uninstall_keeps_a_database_of_that_name_owned_by_another_role(
    upgrade: Server,
) -> None:
    """Someone else's conference_drawing database is not ours to drop."""
    upgrade.fakes.prepend("systemctl", {"match": "is-active", "exit": 3})
    upgrade.fakes.prepend(
        "psql", {"match": "-f -", "stdin_match": "datdba", "stdout": "wordpress\n"}
    )
    result = upgrade.uninstall()
    assert result.returncode == 0, result.stderr
    sql = _admin_sql(upgrade)
    assert "DROP DATABASE" not in sql and "DROP ROLE" not in sql
    said = result.stdout + result.stderr
    assert "kept the conference_drawing database: it is owned by wordpress" in said


def test_uninstall_drops_the_database_its_role_owns(upgrade: Server) -> None:
    upgrade.fakes.prepend("systemctl", {"match": "is-active", "exit": 3})
    upgrade.fakes.prepend(
        "psql",
        {"match": "-f -", "stdin_match": "datdba", "stdout": "conference-drawing\n"},
    )
    result = upgrade.uninstall()
    assert result.returncode == 0, result.stderr
    assert 'DROP DATABASE IF EXISTS :"db" WITH (FORCE);' in _admin_sql(upgrade)


def test_uninstall_stops_if_it_cannot_read_the_owner(upgrade: Server) -> None:
    upgrade.fakes.prepend("systemctl", {"match": "is-active", "exit": 3})
    upgrade.fakes.prepend("psql", {"match": "-f -", "stdin_match": "datdba", "exit": 2})
    result = upgrade.uninstall()
    assert result.returncode != 0
    assert "cannot read the owner" in result.stderr
    assert "DROP DATABASE" not in _admin_sql(upgrade)


@pytest.mark.parametrize("blocker", PURGE_BLOCKERS)
def test_uninstall_keeps_postgresql_unless_it_is_provably_ours_alone(
    upgrade: Server, blocker: str
) -> None:
    tool, entry = PURGE_BLOCKERS[blocker]
    _stopped(upgrade).fakes.prepend(tool, entry)
    result = upgrade.uninstall()
    assert result.returncode == 0, result.stderr
    assert not any(line.startswith("apt-get") for line in upgrade.log())
    assert 'DROP DATABASE IF EXISTS :"db"' in _admin_sql(upgrade)  # ours still goes
    # The operator gets the command and is told the drop-in stayed.
    assert f"{PURGE}" in result.stderr
    assert f"/{PG_DROPIN}" in result.stderr
    assert not upgrade.app.exists()


def test_uninstall_keeps_a_postgresql_it_did_not_install(upgrade: Server) -> None:
    upgrade.fakes.prepend("systemctl", {"match": "is-active", "exit": 3})
    (upgrade.root / PG_MARKER).unlink()
    result = upgrade.uninstall()
    assert result.returncode == 0, result.stderr
    assert not any(line.startswith("apt-get") for line in upgrade.log())
    assert 'DROP ROLE IF EXISTS :"role"' in _admin_sql(upgrade)


def test_uninstall_without_postgresql_skips_the_database(upgrade: Server) -> None:
    upgrade.fakes.prepend("systemctl", {"match": "is-active", "exit": 3})
    upgrade.fakes.set("dpkg-query", _dpkg())
    shutil.rmtree(upgrade.root / "etc/postgresql")
    result = upgrade.uninstall()
    assert result.returncode == 0, result.stderr
    assert upgrade.fakes.calls("psql") == []
    assert not upgrade.app.exists()


def test_uninstall_refuses_while_a_unit_still_runs(upgrade: Server) -> None:
    result = upgrade.uninstall()  # is-active answers 0: still running
    assert result.returncode != 0
    assert upgrade.app.exists()


# --- owner_password.sh ---------------------------------------------------------


def test_owner_password_runs_changepassword_as_the_app_user(server: Server) -> None:
    script = str(server.release / "remote" / "owner_password.sh")
    server.fakes.prepend("runuser", {"match": "changepassword", "stdout": "ok\n"})
    result = server.fakes.run([script, "owner@example.com"])
    assert result.returncode == 0, result.stderr
    (call,) = [c for c in server.fakes.calls("runuser") if "changepassword" in c.argv]
    assert call.argv[:2] == ["-u", "conference-drawing"]
    assert call.argv[-3:] == ["changepassword", "--", "owner@example.com"]
    assert call.argv[-4].endswith("/bin/conference-drawing")


@pytest.mark.parametrize("name", ["", "a b", "x;y", "$(id)", "a" * 151, "-x", "--help"])
def test_owner_password_refuses_a_bad_username(server: Server, name: str) -> None:
    script = str(server.release / "remote" / "owner_password.sh")
    result = server.fakes.run([script, name])
    assert result.returncode != 0
    assert server.fakes.calls("runuser") == []


def test_owner_password_refuses_without_a_username(server: Server) -> None:
    script = str(server.release / "remote" / "owner_password.sh")
    result = server.fakes.run([script])
    assert result.returncode != 0
    assert server.fakes.calls("runuser") == []


def test_owner_password_refuses_when_not_root(server: Server) -> None:
    server.fakes.prepend("id", {"match": "-u", "stdout": "1000\n"})
    script = str(server.release / "remote" / "owner_password.sh")
    result = server.fakes.run([script, "owner"])
    assert result.returncode != 0
    assert "run this as root" in result.stderr
    assert server.fakes.calls("runuser") == []
