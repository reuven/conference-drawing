"""Deploy, backup and teardown scripts, run against fakes only.

Every laptop script runs with ``tests/deploy/fakes`` first on a PATH that holds
no real network tool, against the host ``example.invalid``. The server-side
scripts run only in pieces that are safe on a laptop: the Caddy-site step and
the snapshot job, in a temp root (``CD_ROOT``) with fake ``caddy``,
``systemctl`` and ``flock``.
"""

import re
import shutil
import sqlite3
import subprocess
from contextlib import closing
from pathlib import Path

import pytest

from tests.deploy.conftest import (
    COMMIT,
    DEPLOY,
    DOMAIN,
    HOST,
    HOST_IP,
    RDIR,
    REMOTE_BACKUP,
    Entry,
    Fakes,
    make_backup_tarball,
    os_mode,
)

UP = str(DEPLOY / "up.sh")
DOWN = str(DEPLOY / "down.sh")
BACKUP = str(DEPLOY / "backup.sh")
COMMON = str(DEPLOY / "remote" / "common.sh")
SNAPSHOT = str(DEPLOY / "remote" / "snapshot.sh")
TEMPLATE = DEPLOY / "caddy" / "conference-drawing.caddy.tmpl"
SYSTEMD = DEPLOY / "systemd"

OWNER_INPUT = (
    "owner\nowner@example.invalid\nowner-SECRET-pw-1234\nowner-SECRET-pw-1234\n"
)
DRIP_KEY = "drip-SECRET-key-5678"
DRIP_ACCOUNT = "acct-SECRET-9012"


def _git() -> list[Entry]:
    return [
        {"match": "status --porcelain", "stdout": ""},
        {"match": "rev-parse --abbrev-ref HEAD", "stdout": "main\n"},
        {"match": "rev-parse HEAD", "stdout": f"{COMMIT}\n"},
        {"match": "describe", "stdout": "v0.1.0-3-g0123456\n"},
    ]


def _staging_ssh() -> list[Entry]:
    return [
        {"match": f"rm -rf {RDIR} && mkdir -m 700 {RDIR}"},
        {"match": "rm -rf /tmp/conference-drawing-backup.Ab12Cd34"},
        {"match": f"rm -rf {RDIR}"},
    ]


def _scp(tarball: Path | None = None) -> list[Entry]:
    entries: list[Entry] = []
    if tarball is not None:
        entries.append({"match": "conference-drawing-backup", "file": str(tarball)})
    entries.append({"match": f"{HOST}:{RDIR}/"})
    return entries


@pytest.fixture
def up(fakes: Fakes) -> Fakes:
    """Canned responses for a clean first install of DOMAIN on HOST."""
    fakes.set("git", _git())
    fakes.set(
        "uv",
        [
            {
                "match": "build --wheel",
                "touch_after": [
                    "--out-dir",
                    "conference_drawing-0.1.0-py3-none-any.whl",
                ],
            },
            {"match": "export --frozen", "stdout": "django==6.1.3\n"},
        ],
    )
    fakes.set(
        "ssh",
        [
            {"match": "systemctl --failed", "stdout": ""},
            {"match": "caddy validate --config /etc/caddy/Caddyfile"},
            {"match": "/etc/caddy/sites/", "stdout": "", "exit": 0},
            {"match": "hostname -I", "stdout": f"{HOST_IP} 10.10.0.5 2001:db8::5\n"},
            {"match": "test -e /srv/conference-drawing/data/.env", "stdout": "fresh\n"},
            {"match": f"bash {RDIR}/remote/install.sh {DOMAIN}"},
            *_staging_ssh(),
        ],
    )
    fakes.set("dig", [{"match": f"+short {DOMAIN} A", "stdout": f"{HOST_IP}\n"}])
    fakes.set("scp", _scp())
    fakes.set(
        "curl",
        [
            {"match": f"https://{DOMAIN}/health", "stdout": '{"status": "ok"}'},
            {
                "match": f"https://{DOMAIN}/version",
                "stdout": f'{{"version": "0.1.0", "commit": "{COMMIT}", '
                '"built_at": "", "branch": "main", "commit_short": "0123456"}',
            },
            {"match": f"https://{DOMAIN}/privacy/"},
        ],
    )
    laptop_env = fakes.root / "laptop.env"
    laptop_env.write_text(
        f"DJANGO_SECRET_KEY=dev-only\nDRIP_API_KEY='{DRIP_KEY}'\n"
        f'export DRIP_ACCOUNT_ID="{DRIP_ACCOUNT}"\n'
    )
    fakes.extra_env["CD_LAPTOP_ENV_FILE"] = str(laptop_env)
    fakes.extra_env["CD_BACKUP_DIR"] = str(fakes.root / "backups")
    return fakes


@pytest.fixture
def down(fakes: Fakes) -> Fakes:
    """Canned responses for a teardown whose backup succeeds."""
    tarball = make_backup_tarball(fakes.root / "canned" / "backup.tar.gz")
    fakes.set("git", _git())
    fakes.set(
        "ssh",
        [
            *_staging_ssh()[:1],
            {"match": f"bash {RDIR}/remote/outbox_status.sh", "stdout": "pending=0"},
            {"match": f"bash {RDIR}/remote/stop.sh"},
            {"match": f"bash {RDIR}/remote/backup.sh", "stdout": REMOTE_BACKUP + "\n"},
            {"match": f"bash {RDIR}/remote/uninstall.sh"},
            *_staging_ssh()[1:],
        ],
    )
    fakes.set("scp", _scp(tarball))
    fakes.extra_env["CD_BACKUP_DIR"] = str(fakes.root / "backups")
    return fakes


def _fail(
    fakes: Fakes, tool: str, match: str, *, stdout: str = "", exact: bool = False
) -> None:
    fakes.prepend(tool, {"match": match, "exit": 1, "stdout": stdout, "exact": exact})


def _ssh_keys(fakes: Fakes) -> list[str]:
    return [c.key for c in fakes.calls("ssh")]


def _never_installed(fakes: Fakes) -> None:
    assert not any("install.sh" in k for k in _ssh_keys(fakes))
    assert fakes.calls("scp") == []


# --- up.sh preflight --------------------------------------------------------


def test_up_happy_path_installs_then_smoke_tests(up: Fakes) -> None:
    result = up.run([UP, HOST, DOMAIN], stdin=OWNER_INPUT)
    assert result.returncode == 0, result.stderr
    keys = _ssh_keys(up)
    order = [
        "systemctl --failed",
        "caddy validate",
        "/etc/caddy/sites/",
        "hostname -I",
        "test -e /srv/conference-drawing/data/.env",
        "mkdir -m 700",
        "remote/install.sh",
    ]
    positions = [next(i for i, k in enumerate(keys) if m in k) for m in order]
    assert positions == sorted(positions)
    assert keys[-1] == f"rm -rf {RDIR}"
    log = up.calls()
    scp_at = next(i for i, c in enumerate(log) if c.tool == "scp")
    install_at = next(i for i, c in enumerate(log) if "install.sh" in c.key)
    first_curl = next(i for i, c in enumerate(log) if c.tool == "curl")
    assert scp_at < install_at < first_curl
    curls = [c.key for c in up.calls("curl")]
    assert [u.split()[-1] for u in curls] == [
        f"https://{DOMAIN}/health",
        f"https://{DOMAIN}/version",
        f"https://{DOMAIN}/privacy/",
    ]


def _uploads(fakes: Fakes) -> Path:
    uploads = fakes.root / "uploads"
    uploads.mkdir()
    fakes.extra_env["FAKE_SCP_UPLOADS"] = str(uploads)
    return uploads


def test_up_stages_everything_install_needs_in_one_copy(up: Fakes) -> None:
    uploads = _uploads(up)
    result = up.run([UP, HOST, DOMAIN], stdin=OWNER_INPUT)
    assert result.returncode == 0, result.stderr
    assert len(up.calls("scp")) == 1
    names = {p.name for p in uploads.iterdir()}
    assert {"remote", "systemd", "caddy", "requirements.txt", "version.json"} <= names
    assert "conference_drawing-0.1.0-py3-none-any.whl" in names
    assert (uploads / "remote" / "install.sh").exists()
    assert (uploads / "requirements.txt").read_text() == "django==6.1.3\n"
    (export,) = [c for c in up.calls("uv") if c.argv[0] == "export"]
    assert {"--frozen", "--no-dev", "--no-hashes", "--no-emit-project"} <= set(
        export.argv
    )


def test_up_writes_version_json_for_the_version_endpoint(up: Fakes) -> None:
    import json

    uploads = _uploads(up)
    result = up.run([UP, HOST, DOMAIN], stdin=OWNER_INPUT)
    assert result.returncode == 0, result.stderr
    data = json.loads((uploads / "version.json").read_text())
    assert data["commit"] == COMMIT
    assert data["branch"] == "main"
    assert data["describe"] == "v0.1.0-3-g0123456"
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", data["built_at"])


def test_up_refuses_when_failed_units(up: Fakes) -> None:
    up.prepend(
        "ssh",
        {"match": "systemctl --failed", "stdout": "php8.3-fpm.service loaded failed\n"},
    )
    result = up.run([UP, HOST, DOMAIN], stdin=OWNER_INPUT)
    assert result.returncode != 0
    assert "failed" in result.stderr
    assert "php8.3-fpm.service" in result.stderr
    _never_installed(up)


def test_up_refuses_when_caddy_invalid(up: Fakes) -> None:
    _fail(up, "ssh", "caddy validate")
    result = up.run([UP, HOST, DOMAIN], stdin=OWNER_INPUT)
    assert result.returncode != 0
    assert "caddy" in result.stderr.lower()
    _never_installed(up)


def test_up_refuses_when_caddyfile_does_not_import_sites(up: Fakes) -> None:
    _fail(up, "ssh", "/etc/caddy/sites/")
    result = up.run([UP, HOST, DOMAIN], stdin=OWNER_INPUT)
    assert result.returncode != 0
    assert "import /etc/caddy/sites/*.caddy" in result.stderr
    _never_installed(up)


@pytest.mark.parametrize(
    "dig_output",
    ["198.51.100.99\n", "", f"{HOST_IP}\n198.51.100.99\n"],
    ids=["other-ip", "no-record", "one-of-two-elsewhere"],
)
def test_up_refuses_when_dns_mismatch(up: Fakes, dig_output: str) -> None:
    up.prepend("dig", {"match": f"+short {DOMAIN} A", "stdout": dig_output})
    result = up.run([UP, HOST, DOMAIN], stdin=OWNER_INPUT)
    assert result.returncode != 0
    assert "DNS" in result.stderr
    _never_installed(up)


def test_up_accepts_dns_through_a_cname(up: Fakes) -> None:
    up.prepend(
        "dig",
        {
            "match": f"+short {DOMAIN} A",
            "stdout": f"edge.example.invalid.\n{HOST_IP}\n",
        },
    )
    assert up.run([UP, HOST, DOMAIN], stdin=OWNER_INPUT).returncode == 0


def test_up_refuses_a_dirty_checkout(up: Fakes) -> None:
    up.prepend("git", {"match": "status --porcelain", "stdout": " M README.md\n"})
    result = up.run([UP, HOST, DOMAIN], stdin=OWNER_INPUT)
    assert result.returncode != 0
    assert "uncommitted" in result.stderr
    assert up.calls("ssh") == []


def test_up_upgrade_does_not_prompt_for_a_password(up: Fakes) -> None:
    up.prepend(
        "ssh",
        {"match": "test -e /srv/conference-drawing/data/.env", "stdout": "installed\n"},
    )
    result = up.run([UP, HOST, DOMAIN], stdin="")
    assert result.returncode == 0, result.stderr
    (install,) = [c for c in up.calls("ssh") if "install.sh" in c.key]
    assert "DJANGO_SUPERUSER_PASSWORD" not in install.stdin


def test_up_refuses_mismatched_passwords(up: Fakes) -> None:
    stdin = "owner\nowner@example.invalid\nfirst-password-1\nsecond-password-2\n"
    result = up.run([UP, HOST, DOMAIN], stdin=stdin)
    assert result.returncode != 0
    _never_installed(up)


def test_up_restore_copies_the_backup_and_skips_the_password(up: Fakes) -> None:
    backup = make_backup_tarball(up.root / "old" / "previous.tar.gz")
    uploads = _uploads(up)
    result = up.run([UP, HOST, DOMAIN, "--restore", str(backup)], stdin="")
    assert result.returncode == 0, result.stderr
    assert (uploads / "restore.tar.gz").read_bytes() == backup.read_bytes()
    (install,) = [c for c in up.calls("ssh") if "install.sh" in c.key]
    assert install.key == (
        f"bash {RDIR}/remote/install.sh {DOMAIN} --restore {RDIR}/restore.tar.gz"
    )
    assert "DJANGO_SUPERUSER_PASSWORD" not in install.stdin


def test_up_restore_refuses_a_missing_file(up: Fakes) -> None:
    result = up.run([UP, HOST, DOMAIN, "--restore", str(up.root / "nope.tar.gz")])
    assert result.returncode != 0
    assert up.calls() == []


def test_up_restore_refuses_over_an_existing_install(up: Fakes) -> None:
    backup = make_backup_tarball(up.root / "old" / "previous.tar.gz")
    up.prepend(
        "ssh",
        {"match": "test -e /srv/conference-drawing/data/.env", "stdout": "installed\n"},
    )
    result = up.run([UP, HOST, DOMAIN, "--restore", str(backup)], stdin="")
    assert result.returncode != 0
    assert "already installed" in result.stderr
    _never_installed(up)


def test_up_fails_when_version_commit_differs(up: Fakes) -> None:
    up.prepend(
        "curl",
        {
            "match": "/version",
            "stdout": '{"commit": "ffffffffffffffffffffffffffffffffffffffff"}',
        },
    )
    result = up.run([UP, HOST, DOMAIN], stdin=OWNER_INPUT)
    assert result.returncode != 0
    assert "commit" in result.stderr
    assert not any(c.key.endswith("/privacy/") for c in up.calls("curl"))


# --- secrets ----------------------------------------------------------------


def test_no_secret_values_in_ssh_argv(up: Fakes) -> None:
    result = up.run([UP, HOST, DOMAIN], stdin=OWNER_INPUT)
    assert result.returncode == 0, result.stderr
    secrets = [DRIP_KEY, DRIP_ACCOUNT, "owner-SECRET-pw-1234"]
    for call in up.calls():
        for secret in secrets:
            assert all(secret not in arg for arg in call.argv), (call.tool, secret)
    (install,) = [c for c in up.calls("ssh") if "install.sh" in c.key]
    for secret in secrets:
        assert secret in install.stdin
    # Every other ssh call gets nothing on stdin.
    assert all(c.stdin == "" for c in up.calls("ssh") if c.key != install.key)
    for secret in secrets:
        assert secret not in result.stdout + result.stderr
    # The laptop-side temp file holding them is gone.
    leftovers = [
        p
        for p in (up.root / "tmp").rglob("*")
        if p.is_file() and "SECRET" in p.read_text(errors="replace")
    ]
    assert leftovers == []


def test_drip_keys_are_optional(up: Fakes) -> None:
    (up.root / "laptop.env").write_text("DJANGO_SECRET_KEY=dev-only\n")
    result = up.run([UP, HOST, DOMAIN], stdin=OWNER_INPUT)
    assert result.returncode == 0, result.stderr
    (install,) = [c for c in up.calls("ssh") if "install.sh" in c.key]
    assert "DRIP_API_KEY" not in install.stdin


# --- domain validation and the Caddy site -----------------------------------


BAD_DOMAINS = [
    "evil.example { }",
    "a b.example",
    "-x.example",
    "x;rm.example",
    "Raffle.Example.invalid",
    "localhost",
    "raffle.example.invalid/c/",
    "raffle.example.invalid\nimport /etc/passwd",
    "",
]


@pytest.mark.parametrize("domain", BAD_DOMAINS, ids=range(len(BAD_DOMAINS)))
def test_up_refuses_an_invalid_domain_before_any_call(up: Fakes, domain: str) -> None:
    result = up.run([UP, HOST, domain], stdin=OWNER_INPUT)
    assert result.returncode != 0
    assert up.calls() == []


def _caddy_root(fakes: Fakes, previous: str | None) -> Path:
    root = fakes.root / "root"
    (root / "etc" / "caddy" / "sites").mkdir(parents=True)
    (root / "etc" / "caddy" / "Caddyfile").write_text(
        "import /etc/caddy/sites/*.caddy\n"
    )
    if previous is not None:
        (root / "etc/caddy/sites/conference-drawing.caddy").write_text(previous)
    fakes.extra_env["CD_ROOT"] = str(root)
    return root / "etc/caddy/sites/conference-drawing.caddy"


def _install_site(fakes: Fakes, domain: str) -> subprocess.CompletedProcess[str]:
    script = 'source "$1"; install_caddy_site "$2" "$3"'
    return fakes.run(["-c", script, "_", COMMON, domain, str(TEMPLATE)])


def test_install_restores_previous_caddy_file_on_validate_failure(fakes: Fakes) -> None:
    site = _caddy_root(fakes, previous="# previous site\n")
    fakes.set("caddy", [{"match": "validate", "exit": 1, "stderr": "bad config\n"}])
    fakes.set("systemctl", [{"match": "reload caddy"}])
    result = _install_site(fakes, DOMAIN)
    assert result.returncode != 0
    assert site.read_text() == "# previous site\n"
    assert sorted(p.name for p in site.parent.iterdir()) == ["conference-drawing.caddy"]
    assert fakes.calls("systemctl") == []


def test_install_removes_new_caddy_file_on_first_validate_failure(fakes: Fakes) -> None:
    site = _caddy_root(fakes, previous=None)
    fakes.set("caddy", [{"match": "validate", "exit": 1}])
    result = _install_site(fakes, DOMAIN)
    assert result.returncode != 0
    assert list(site.parent.iterdir()) == []
    assert fakes.calls("systemctl") == []


def test_install_validates_then_reloads_caddy(fakes: Fakes) -> None:
    site = _caddy_root(fakes, previous="# previous site\n")
    fakes.set("caddy", [{"match": "validate --config"}])
    fakes.set("systemctl", [{"match": "reload caddy"}])
    result = _install_site(fakes, DOMAIN)
    assert result.returncode == 0, result.stderr
    text = site.read_text()
    assert text.startswith("# Managed by conference-drawing")
    assert f"\n{DOMAIN} {{\n" in text
    assert "__DOMAIN__" not in text
    assert [c.tool for c in fakes.calls()] == ["caddy", "systemctl"]
    (validate,) = fakes.calls("caddy")
    assert validate.argv[:3] == [
        "validate",
        "--config",
        str(site.parents[1] / "Caddyfile"),
    ]
    assert sorted(p.name for p in site.parent.iterdir()) == ["conference-drawing.caddy"]


@pytest.mark.parametrize("domain", BAD_DOMAINS, ids=range(len(BAD_DOMAINS)))
def test_caddy_template_renders_valid_domain_only(fakes: Fakes, domain: str) -> None:
    site = _caddy_root(fakes, previous=None)
    fakes.set("caddy", [{"match": "validate"}])
    fakes.set("systemctl", [{"match": "reload caddy"}])
    result = _install_site(fakes, domain)
    assert result.returncode != 0
    assert list(site.parent.iterdir()) == []
    assert fakes.calls() == []


def test_caddy_template_shape() -> None:
    text = TEMPLATE.read_text()
    assert "request_body {\n\t\tmax_size 6MB\n\t}" in text
    assert "handle /static/* {" in text
    assert "handle /media/* {" in text
    assert "root * /srv/conference-drawing/data" in text
    assert "reverse_proxy unix//run/conference-drawing/web.sock" in text
    # client_ip trusts the rightmost X-Forwarded-For entry, which holds only
    # while Caddy is the sole proxy and ignores client-sent X-Forwarded-For.
    assert not re.search(r"^\s*trusted_proxies", text, re.M)


def _log_filters(text: str) -> dict[str, list[tuple[str, str]]]:
    """Parse `<field> multi_regexp { regexp "<pattern>" "<replacement>" ... }`."""
    filters: dict[str, list[tuple[str, str]]] = {}
    for block in re.finditer(r"(\S+) multi_regexp \{\n(.*?)\n\t*\}", text, re.S):
        ops = re.findall(r'regexp "((?:[^"\\]|\\.)*)" "((?:[^"\\]|\\.)*)"', block[2])
        filters[block[1]] = ops
    return filters


def _apply(ops: list[tuple[str, str]], value: str) -> str:
    """Apply Caddy's regexp replacements (RE2 `${1}` -> Python `\\g<1>`)."""
    for pattern, replacement in ops:
        value = re.sub(pattern, re.sub(r"\$\{(\d+)\}", r"\\g<\1>", replacement), value)
    return value


LOGGED_URIS = [
    ("/display/Tok3n-abc_DEF/", "/display/REDACTED/"),
    ("/display/Tok3n-abc_DEF/panel/", "/display/REDACTED/panel/"),
    ("/DISPLAY/Tok3n-abc_DEF/panel/", "/DISPLAY/REDACTED/panel/"),
    ("//display//Tok3n-abc_DEF/panel/?x=1", "//display//REDACTED/panel/"),
    ("/%64isplay/Tok3n-abc_DEF/", "/%64isplay/REDACTED/"),
    ("/display%2FTok3n-abc_DEF/panel/", "/display%2FREDACTED/panel/"),
    ("/c/pycon-2027/?email=someone%40example.invalid", "/c/pycon-2027/"),
    ("/C//pycon-2027/thanks/?utm=x", "/C//pycon-2027/thanks/"),
    ("/admin/raffle/entrant/?q=someone", "/admin/raffle/entrant/"),
    ("/static/raffle/site.css", "/static/raffle/site.css"),
]


@pytest.mark.parametrize(("uri", "logged"), LOGGED_URIS, ids=range(len(LOGGED_URIS)))
def test_caddy_access_log_redacts_tokens_and_query_strings(
    uri: str, logged: str
) -> None:
    filters = _log_filters(TEMPLATE.read_text())
    assert set(filters) == {"request>uri", "request>headers>Referer"}
    assert _apply(filters["request>uri"], uri) == logged
    referer = f"https://{DOMAIN}{uri}"
    assert (
        _apply(filters["request>headers>Referer"], referer)
        == f"https://{DOMAIN}{logged}"
    )
    assert "Tok3n" not in _apply(filters["request>uri"], uri)


# --- systemd units ----------------------------------------------------------


def _unit(name: str) -> dict[str, list[str]]:
    """Map `Section.Key` to every value it is given (systemd allows repeats)."""
    values: dict[str, list[str]] = {}
    section = ""
    for line in (SYSTEMD / name).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            section = line.strip("[]")
            continue
        key, _, value = line.partition("=")
        values.setdefault(f"{section}.{key}", []).append(value)
    return values


SERVICES = [
    "conference-drawing-web.service",
    "conference-drawing-drip.service",
    "conference-drawing-snapshot.service",
]


def test_units_are_exactly_the_expected_four() -> None:
    assert sorted(p.name for p in SYSTEMD.iterdir()) == sorted(
        [*SERVICES, "conference-drawing-snapshot.timer"]
    )


@pytest.mark.parametrize("name", SERVICES)
def test_services_set_paths_user_and_hardening(name: str) -> None:
    unit = _unit(name)
    env = unit["Service.Environment"]
    assert "CD_ENV_FILE=/srv/conference-drawing/data/.env" in env
    assert "CD_DATA_DIR=/srv/conference-drawing/data" in env
    assert "CD_VERSION_FILE=/srv/conference-drawing/version.json" in env
    expected = {
        "User": "conference-drawing",
        "UMask": "007",
        "NoNewPrivileges": "yes",
        "ProtectSystem": "strict",
        "ReadWritePaths": "/srv/conference-drawing/data",
        "ProtectHome": "yes",
        "PrivateTmp": "yes",
    }
    for key, value in expected.items():
        assert unit[f"Service.{key}"] == [value], key


def test_web_unit_serves_gunicorn_on_the_socket() -> None:
    unit = _unit("conference-drawing-web.service")
    assert unit["Service.RuntimeDirectory"] == ["conference-drawing"]
    (start,) = unit["Service.ExecStart"]
    assert start.startswith("/srv/conference-drawing/venv/bin/gunicorn ")
    assert "--bind unix:/run/conference-drawing/web.sock" in start
    assert "conference_drawing.wsgi" in start
    assert unit["Install.WantedBy"] == ["multi-user.target"]


def test_drip_unit_is_a_single_worker_with_graceful_stop() -> None:
    unit = _unit("conference-drawing-drip.service")
    (start,) = unit["Service.ExecStart"]
    assert start == "/srv/conference-drawing/venv/bin/conference-drawing drip_worker"
    assert unit["Service.KillSignal"] == ["SIGTERM"]
    assert unit["Service.TimeoutStopSec"] == ["60"]
    assert not any("@" in p.name for p in SYSTEMD.iterdir())


def test_snapshot_timer_runs_hourly() -> None:
    timer = _unit("conference-drawing-snapshot.timer")
    assert timer["Timer.OnCalendar"] == ["hourly"]
    assert timer["Timer.Unit"] == ["conference-drawing-snapshot.service"]
    service = _unit("conference-drawing-snapshot.service")
    assert service["Service.Type"] == ["oneshot"]
    assert service["Service.ExecStart"] == ["/srv/conference-drawing/bin/snapshot.sh"]


# --- snapshots ---------------------------------------------------------------


def test_snapshot_keeps_newest_48(fakes: Fakes) -> None:
    data = fakes.root / "root" / "srv" / "conference-drawing" / "data"
    snaps = data / "snapshots"
    snaps.mkdir(parents=True)
    with closing(sqlite3.connect(data / "db.sqlite3")) as db, db:
        db.execute("create table books (title text)")
        db.execute("insert into books values ('Python Workout')")
    (data / ".maintenance.lock").touch()
    for hour in range(50):
        (snaps / f"db-20000101T{hour % 24:02d}{hour // 24:02d}00Z.sqlite3").write_text(
            ""
        )
    fakes.extra_env["CD_ROOT"] = str(fakes.root / "root")
    fakes.set("flock", [{"match": "--wait"}])
    result = fakes.run([SNAPSHOT])
    assert result.returncode == 0, result.stderr
    names = sorted(p.name for p in snaps.iterdir())
    assert len(names) == 48
    newest = snaps / names[-1]
    assert not newest.name.startswith("db-2000")
    with closing(sqlite3.connect(newest)) as db, db:
        assert db.execute("select title from books").fetchall() == [("Python Workout",)]
    assert [c.argv[:2] for c in fakes.calls("flock")] == [["--wait", "600"]]
    assert os_mode(newest) & 0o077 == 0


# --- .env lines written by install.sh ---------------------------------------


@pytest.mark.parametrize(
    "value",
    ["plain", "with space", "has'quote", "back\\slash", "dollar$HOME", "hash # x", "é"],
)
def test_env_line_round_trips_through_dotenv(fakes: Fakes, value: str) -> None:
    from dotenv import dotenv_values

    out = fakes.root / "line.env"
    script = 'source "$1"; env_line DRIP_API_KEY "$2" > "$3"'
    result = fakes.run(["-c", script, "_", COMMON, value, str(out)])
    assert result.returncode == 0, result.stderr
    assert dotenv_values(out) == {"DRIP_API_KEY": value}


def test_read_secrets_keeps_every_character_after_the_first_equals(
    fakes: Fakes,
) -> None:
    secrets = fakes.root / "secrets"
    secrets.write_text(
        "DRIP_API_KEY=abc=\n"
        "DRIP_ACCOUNT_ID= 12 34 \n"
        "DJANGO_SUPERUSER_USERNAME=owner\n"
        "DJANGO_SUPERUSER_EMAIL=o@example.invalid\n"
        'DJANGO_SUPERUSER_PASSWORD=p=w \' "x"=='  # no final newline
    )
    script = (
        'source "$1"; read_secrets "$2"; printf "%s|" "$drip_api_key" '
        '"$drip_account_id" "$su_username" "$su_email" "$su_password"'
    )
    result = fakes.run(["-c", script, "_", COMMON, str(secrets)])
    assert result.returncode == 0, result.stderr
    assert result.stdout.split("|")[:5] == [
        "abc=",
        " 12 34 ",
        "owner",
        "o@example.invalid",
        'p=w \' "x"==',
    ]


def test_read_secrets_refuses_unknown_keys(fakes: Fakes) -> None:
    secrets = fakes.root / "secrets"
    secrets.write_text("PATH=/evil\n")
    script = 'source "$1"; read_secrets "$2"'
    result = fakes.run(["-c", script, "_", COMMON, str(secrets)])
    assert result.returncode != 0


def test_env_line_refuses_interpolation(fakes: Fakes) -> None:
    script = 'source "$1"; env_line DRIP_API_KEY "$2"'
    result = fakes.run(["-c", script, "_", COMMON, "a${HOME}b"])
    assert result.returncode != 0


# --- backup.sh ---------------------------------------------------------------


def test_backup_pulls_a_checked_tarball(down: Fakes) -> None:
    result = down.run([BACKUP, HOST])
    assert result.returncode == 0, result.stderr
    local = Path(result.stdout.strip().splitlines()[-1])
    assert local.parent == down.root / "backups"
    assert re.fullmatch(rf"{HOST}-\d{{8}}T\d{{6}}Z\.tar\.gz", local.name)
    assert local.stat().st_size > 0
    assert os_mode(local) == 0o600
    keys = _ssh_keys(down)
    assert keys == [
        f"rm -rf {RDIR} && mkdir -m 700 {RDIR}",
        f"bash {RDIR}/remote/backup.sh",
        "rm -rf /tmp/conference-drawing-backup.Ab12Cd34",
        f"rm -rf {RDIR}",
    ]


def test_backup_refuses_an_unexpected_remote_path(down: Fakes) -> None:
    down.prepend("ssh", {"match": "remote/backup.sh", "stdout": "/etc/shadow\n"})
    result = down.run([BACKUP, HOST])
    assert result.returncode != 0
    assert len(down.calls("scp")) == 1  # only the staging upload


def test_backup_refuses_a_download_that_is_not_a_backup(down: Fakes) -> None:
    junk = down.root / "canned" / "junk.tar.gz"
    junk.write_text("not a tarball")
    down.prepend("scp", {"match": "conference-drawing-backup", "file": str(junk)})
    result = down.run([BACKUP, HOST])
    assert result.returncode != 0
    assert list((down.root / "backups").iterdir()) == []
    # The server copy is kept, so the operator can try again.
    assert not any(
        k.startswith("rm -rf /tmp/conference-drawing-backup") for k in _ssh_keys(down)
    )


# --- down.sh -----------------------------------------------------------------


def test_down_refuses_with_pending_outbox(down: Fakes) -> None:
    _fail(down, "ssh", "remote/outbox_status.sh", stdout="pending=3 failed=0\n")
    result = down.run([DOWN, HOST])
    assert result.returncode != 0
    assert "pending" in result.stderr
    keys = _ssh_keys(down)
    assert not any(
        s in k for k in keys for s in ("stop.sh", "backup.sh", "uninstall.sh")
    )


def test_down_runs_backup_before_uninstall(down: Fakes) -> None:
    result = down.run([DOWN, HOST])
    assert result.returncode == 0, result.stderr
    keys = _ssh_keys(down)
    order = ["outbox_status.sh", "stop.sh", "backup.sh", "uninstall.sh"]
    positions = [next(i for i, k in enumerate(keys) if m in k) for m in order]
    assert positions == sorted(positions)
    # The tarball came down (scp from the server) before uninstall ran.
    log = down.calls()
    download = next(
        i for i, c in enumerate(log) if c.tool == "scp" and "backup.tar.gz" in c.key
    )
    uninstall = next(i for i, c in enumerate(log) if "uninstall.sh" in c.key)
    assert download < uninstall
    backups = list((down.root / "backups").iterdir())
    assert len(backups) == 1 and backups[0].stat().st_size > 0


def test_down_refuses_an_empty_backup(down: Fakes) -> None:
    empty = down.root / "canned" / "empty.tar.gz"
    empty.write_bytes(b"")
    down.prepend("scp", {"match": "conference-drawing-backup", "file": str(empty)})
    result = down.run([DOWN, HOST])
    assert result.returncode != 0
    assert not any("uninstall.sh" in k for k in _ssh_keys(down))


# --- failures stop everything after them -------------------------------------

UP_STEPS: list[tuple[str, str]] = [
    ("git", "status --porcelain"),
    ("git", "rev-parse"),
    ("git", "describe"),
    ("uv", "build --wheel"),
    ("uv", "export --frozen"),
    ("ssh", "systemctl --failed"),
    ("ssh", "caddy validate"),
    ("ssh", "/etc/caddy/sites/"),
    ("ssh", "hostname -I"),
    ("dig", "+short"),
    ("ssh", "test -e /srv"),
    ("ssh", "mkdir -m 700"),
    ("scp", f"{HOST}:{RDIR}/"),
    ("ssh", "remote/install.sh"),
    ("curl", "/health"),
    ("curl", "/version"),
    ("curl", "/privacy/"),
    ("ssh", f"rm -rf {RDIR}"),
]
DOWN_STEPS: list[tuple[str, str]] = [
    ("git", "rev-parse HEAD"),
    ("ssh", "mkdir -m 700"),
    ("scp", f"{HOST}:{RDIR}/"),
    ("ssh", "remote/outbox_status.sh"),
    ("ssh", "remote/stop.sh"),
    ("ssh", "remote/backup.sh"),
    ("scp", "conference-drawing-backup"),
    ("ssh", "rm -rf /tmp/conference-drawing-backup."),
    ("ssh", "remote/uninstall.sh"),
    ("ssh", f"rm -rf {RDIR}"),
]


def _sequence(fakes: Fakes, steps: list[tuple[str, str]]) -> list[int]:
    """Map every logged call to the index of the step it is; fail on strays."""
    sequence = []
    for call in fakes.calls():
        index = next(
            (i for i, (t, m) in enumerate(steps) if t == call.tool and m in call.key),
            None,
        )
        assert index is not None, f"unlisted step: {call.tool} {call.key}"
        sequence.append(index)
    return sequence


def _check_failure_stops_the_run(
    fakes: Fakes,
    args: list[str],
    stdin: str,
    steps: list[tuple[str, str]],
    failed: int,
) -> None:
    """Fail step `failed`; the run must exit non-zero right after that call."""
    assert fakes.run(args, stdin=stdin).returncode == 0
    happy = _sequence(fakes, steps)
    assert failed in happy, "the step list names a step the script never runs"
    fakes.log.unlink()
    backups = Path(fakes.extra_env["CD_BACKUP_DIR"])
    if backups.exists():  # one-second timestamps would collide
        shutil.rmtree(backups)
    tool, match = steps[failed]
    # The cleanup command is also the start of the staging command.
    _fail(fakes, tool, match, exact=match == f"rm -rf {RDIR}")
    result = fakes.run(args, stdin=stdin)
    assert result.returncode != 0
    assert _sequence(fakes, steps) == happy[: happy.index(failed) + 1]


@pytest.mark.parametrize("failed", range(len(UP_STEPS)), ids=[m for _, m in UP_STEPS])
def test_scripts_propagate_failure_exit_status_up(up: Fakes, failed: int) -> None:
    _check_failure_stops_the_run(up, [UP, HOST, DOMAIN], OWNER_INPUT, UP_STEPS, failed)


@pytest.mark.parametrize(
    "failed", range(len(DOWN_STEPS)), ids=[m for _, m in DOWN_STEPS]
)
def test_scripts_propagate_failure_exit_status_down(down: Fakes, failed: int) -> None:
    _check_failure_stops_the_run(down, [DOWN, HOST], "", DOWN_STEPS, failed)


# --- the harness itself -------------------------------------------------------


def test_fake_ssh_refuses_any_other_host(fakes: Fakes) -> None:
    fakes.set("ssh", [{"match": ""}])
    result = fakes.run(["-c", "ssh root@wp2.example.com true"])
    assert result.returncode == 97


def test_unknown_commands_exit_97(fakes: Fakes) -> None:
    fakes.set("ssh", [])
    result = fakes.run(["-c", f"ssh root@{HOST} whoami"])
    assert result.returncode == 97


def test_shellcheck_is_clean() -> None:
    shellcheck = shutil.which("shellcheck")
    if shellcheck is None:
        pytest.fail("shellcheck is not installed (brew install shellcheck)")
    scripts = sorted(str(p) for p in DEPLOY.rglob("*.sh"))
    assert len(scripts) >= 9
    result = subprocess.run(  # noqa: S603 - fixed arguments
        [shellcheck, *scripts], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stdout
