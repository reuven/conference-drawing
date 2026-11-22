"""Deploy, backup and teardown scripts, run against fakes only.

Every laptop script runs with ``tests/deploy/fakes`` first on a PATH that holds
no real network tool, against the host ``example.invalid``. The server-side
scripts run only in pieces that are safe on a laptop: the Caddy-site step and
the snapshot job, in a temp root (``CD_ROOT``) with fake ``caddy``,
``systemctl`` and ``flock``. test_install.py runs install.sh and uninstall.sh.
"""

import hashlib
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.deploy.conftest import (
    COMMIT,
    DEPLOY,
    DOMAIN,
    DUMP_BYTES,
    HOST,
    HOST_IP,
    REMOTE_BACKUP,
    STAGE,
    STAGE_RE,
    Entry,
    Fakes,
    make_backup_tarball,
    make_dump,
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
UV_TARBALL = "uv-x86_64-unknown-linux-gnu.tar.gz"
UV_URL = f"https://github.com/astral-sh/uv/releases/download/0.13.9/{UV_TARBALL}"


def _git() -> list[Entry]:
    return [
        {"match": "status --porcelain", "stdout": ""},
        {"match": "rev-parse --abbrev-ref HEAD", "stdout": "main\n"},
        {"match": "rev-parse HEAD", "stdout": f"{COMMIT}\n"},
        {"match": "describe", "stdout": "v0.1.0-3-g0123456\n"},
    ]


def _staging_ssh() -> list[Entry]:
    return [
        {"match": f"mkdir -m 700 {STAGE}"},
        {"match": "rm -rf /tmp/conference-drawing-backup.Ab12Cd34"},
        {"match": f"rm -rf {STAGE}"},
    ]


def _scp(tarball: Path | None = None) -> list[Entry]:
    entries: list[Entry] = []
    if tarball is not None:
        entries.append({"match": "conference-drawing-backup", "file": str(tarball)})
    entries.append({"match": f"{HOST}:{STAGE}"})
    return entries


@pytest.fixture
def up(fakes: Fakes) -> Fakes:
    """Canned responses for a clean first install of DOMAIN on HOST."""
    uv_tarball = fakes.root / "canned" / UV_TARBALL
    uv_tarball.parent.mkdir()
    uv_tarball.write_bytes(b"pretend uv release")
    digest = hashlib.sha256(uv_tarball.read_bytes()).hexdigest()
    hashes = fakes.root / "uv.sha256"
    hashes.write_text(f"{digest}  {UV_TARBALL}\n{'1' * 64}  uv-aarch64-x.tar.gz\n")
    fakes.extra_env["CD_UV_SHA256_FILE"] = str(hashes)
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
            {"match": "getent group caddy", "stdout": "caddy:x:996:\n"},
            {"match": "hostname -I", "stdout": f"{HOST_IP} 10.10.0.5 2001:db8::5\n"},
            {"match": "uname -m", "stdout": "x86_64\n"},
            {"match": "/usr/local/bin/uv --version", "stdout": "uv 0.13.9 (a 2026)\n"},
            {"match": "test -L /srv/conference-drawing/venv", "stdout": "fresh\n"},
            {"match": f"/remote/install.sh {DOMAIN}"},
            *_staging_ssh(),
        ],
    )
    fakes.set(
        "dig",
        [
            {"match": f"+short {DOMAIN} A", "exact": True, "stdout": f"{HOST_IP}\n"},
            {"match": f"+short -t AAAA {DOMAIN}", "exact": True, "stdout": ""},
        ],
    )
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
            {"match": UV_URL, "copy_to_flag": "-o", "file": str(uv_tarball)},
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
            {"match": "/remote/outbox_status.sh", "stdout": "pending=0"},
            {"match": "/remote/stop.sh"},
            {"match": "/remote/backup.sh", "stdout": REMOTE_BACKUP + "\n"},
            {"match": "/remote/uninstall.sh"},
            *_staging_ssh()[1:],
        ],
    )
    fakes.set("scp", _scp(tarball))
    fakes.extra_env["CD_BACKUP_DIR"] = str(fakes.root / "backups")
    return fakes


def _fail(fakes: Fakes, tool: str, match: str, *, stdout: str = "") -> None:
    fakes.prepend(tool, {"match": match, "exit": 1, "stdout": stdout})


def _stage_of(key: str) -> str:
    found = re.search(STAGE_RE, key)
    assert found, key
    return found[0]


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
        "test -L /srv/conference-drawing/venv",
        "mkdir -m 700",
        "remote/install.sh",
    ]
    positions = [next(i for i, k in enumerate(keys) if m in k) for m in order]
    assert positions == sorted(positions)
    stage = _stage_of(keys[0 if "mkdir" in keys[0] else -1])
    assert "-up-" in stage
    assert keys[-1] == f"rm -rf {stage}"
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
    assert {"--frozen", "--no-dev", "--no-emit-project"} <= set(export.argv)
    assert "--no-hashes" not in export.argv  # the server installs --require-hashes
    wheel = uploads / "conference_drawing-0.1.0-py3-none-any.whl"
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    assert (uploads / "wheel.sha256").read_text() == f"{digest}  {wheel.name}\n"
    # The server already has the pinned uv, so none is downloaded or shipped.
    assert not any(UV_URL in c.key for c in up.calls("curl"))
    assert "uv.sha256" not in names


def test_up_stages_each_run_in_its_own_directory(up: Fakes) -> None:
    assert up.run([UP, HOST, DOMAIN], stdin=OWNER_INPUT).returncode == 0
    assert up.run([UP, HOST, DOMAIN], stdin=OWNER_INPUT).returncode == 0
    made = [_stage_of(k) for k in _ssh_keys(up) if k.startswith("mkdir")]
    assert len(made) == 2 and made[0] != made[1]
    assert all(
        k == f"mkdir -m 700 {_stage_of(k)}" for k in _ssh_keys(up) if "mkdir" in k
    )


def test_up_ships_the_pinned_uv_when_the_server_has_another(up: Fakes) -> None:
    uploads = _uploads(up)
    up.prepend("ssh", {"match": "/usr/local/bin/uv --version", "stdout": "uv 0.12.0\n"})
    result = up.run([UP, HOST, DOMAIN], stdin=OWNER_INPUT)
    assert result.returncode == 0, result.stderr
    (download,) = [c for c in up.calls("curl") if UV_URL in c.key]
    assert "--proto" in download.argv
    assert (uploads / UV_TARBALL).read_bytes() == b"pretend uv release"
    digest = hashlib.sha256(b"pretend uv release").hexdigest()
    assert (uploads / "uv.sha256").read_text() == f"{digest}  {UV_TARBALL}\n"


def test_up_refuses_a_uv_download_with_the_wrong_hash(up: Fakes) -> None:
    up.prepend("ssh", {"match": "/usr/local/bin/uv --version", "stdout": ""})
    (up.root / "canned" / UV_TARBALL).write_bytes(b"tampered")
    result = up.run([UP, HOST, DOMAIN], stdin=OWNER_INPUT)
    assert result.returncode != 0
    assert "sha256" in result.stderr
    _never_installed(up)


def test_up_refuses_while_the_uv_hash_file_is_a_placeholder(up: Fakes) -> None:
    placeholder = up.root / "uv-0.13.9.sha256"
    placeholder.write_text(f"PLACEHOLDER  {UV_TARBALL}\n")
    up.extra_env["CD_UV_SHA256_FILE"] = str(placeholder)
    result = up.run([UP, HOST, DOMAIN], stdin=OWNER_INPUT)
    assert result.returncode != 0
    assert "uv-0.13.9.sha256" in result.stderr
    assert "releases" in result.stderr
    assert up.calls() == []


def test_committed_uv_hash_file_is_filled_in() -> None:
    lines = [
        line
        for line in (DEPLOY / "uv-0.13.9.sha256").read_text().splitlines()
        if line and not line.startswith("#")
    ]
    names = sorted(line.split("  ", 1)[1] for line in lines)
    assert names == [
        "uv-aarch64-unknown-linux-gnu.tar.gz",
        "uv-x86_64-unknown-linux-gnu.tar.gz",
    ]
    assert all(re.fullmatch(r"[0-9a-f]{64}", line.split("  ", 1)[0]) for line in lines)


def test_up_refuses_an_unknown_server_architecture(up: Fakes) -> None:
    up.prepend("ssh", {"match": "uname -m", "stdout": "riscv64\n"})
    result = up.run([UP, HOST, DOMAIN], stdin=OWNER_INPUT)
    assert result.returncode != 0
    _never_installed(up)


def test_uv_version_agrees_everywhere() -> None:
    lib = (DEPLOY / "lib.sh").read_text()
    common = (DEPLOY / "remote" / "common.sh").read_text()
    assert "UV_VERSION=0.13.9\n" in lib
    assert "UV_VERSION=0.13.9\n" in common


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


def test_up_decides_upgrade_from_the_live_venv_link_not_env(up: Fakes) -> None:
    # A first install that failed after writing .env (Drip keys) is still
    # fresh: the owner prompt and --restore must stay available on a rerun.
    text = (DEPLOY / "up.sh").read_text()
    assert (
        "test -L /srv/conference-drawing/venv && echo installed || echo fresh" in text
    )
    assert "data/.env" not in text.split("# 2. Preflight", 1)[1]


def test_up_refuses_when_there_is_no_caddy_group(up: Fakes) -> None:
    _fail(up, "ssh", "getent group caddy")
    result = up.run([UP, HOST, DOMAIN], stdin=OWNER_INPUT)
    assert result.returncode != 0
    assert "caddy" in result.stderr
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
    up.prepend(
        "dig", {"match": f"+short {DOMAIN} A", "exact": True, "stdout": dig_output}
    )
    result = up.run([UP, HOST, DOMAIN], stdin=OWNER_INPUT)
    assert result.returncode != 0
    assert "DNS" in result.stderr
    _never_installed(up)


def test_up_refuses_an_aaaa_record_elsewhere(up: Fakes) -> None:
    up.prepend(
        "dig",
        {
            "match": f"+short -t AAAA {DOMAIN}",
            "exact": True,
            "stdout": "2001:db8::99\n",
        },
    )
    result = up.run([UP, HOST, DOMAIN], stdin=OWNER_INPUT)
    assert result.returncode != 0
    assert "2001:db8::99" in result.stderr
    _never_installed(up)


def test_up_accepts_an_aaaa_record_for_the_host(up: Fakes) -> None:
    up.prepend(
        "dig",
        {"match": f"+short -t AAAA {DOMAIN}", "exact": True, "stdout": "2001:db8::5\n"},
    )
    assert up.run([UP, HOST, DOMAIN], stdin=OWNER_INPUT).returncode == 0


def test_up_accepts_dns_through_a_cname(up: Fakes) -> None:
    up.prepend(
        "dig",
        {
            "match": f"+short {DOMAIN} A",
            "exact": True,
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
        {"match": "test -L /srv/conference-drawing/venv", "stdout": "installed\n"},
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
    stage = _stage_of(install.key)
    assert install.key == (
        f"bash {stage}/remote/install.sh {DOMAIN} --restore {stage}/restore.tar.gz"
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
        {"match": "test -L /srv/conference-drawing/venv", "stdout": "installed\n"},
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
    # A handled 5xx is logged at DEBUG, so Caddy's error log (which ignores
    # the access-log filters) never records a display URL.
    assert 'handle_errors 5xx {\n\t\trespond "Temporarily unavailable" 503\n\t}' in text
    # client_ip trusts the rightmost X-Forwarded-For entry, which holds only
    # while Caddy is the sole proxy and ignores client-sent X-Forwarded-For.
    assert not re.search(r"^\s*trusted_proxies", text, re.M)


def test_caddy_buffers_request_bodies_for_the_sync_workers() -> None:
    """A slow-trickling body is read by Caddy, not by a gunicorn worker."""
    text = TEMPLATE.read_text()
    proxy = re.search(r"reverse_proxy unix//\S+ \{\n(.*?)\n\t\t\}", text, re.S)
    assert proxy is not None
    assert "\t\t\trequest_buffers 1MB" in proxy[1].splitlines()


def test_caddy_sends_hsts_for_this_host_only_and_no_server_header() -> None:
    text = TEMPLATE.read_text()
    block = '\theader {\n\t\tStrict-Transport-Security "max-age=31536000"\n'
    assert block + "\t\t-Server\n\t}" in text
    # The host is shared: sibling subdomains must not be forced onto HTTPS.
    assert "includeSubDomains" not in text


def WEB_UNIT_TEXT() -> str:  # noqa: N802 - reads like the constant it stands for
    return (DEPLOY / "systemd" / "conference-drawing-web.service").read_text()


def _log_filters(text: str) -> dict[str, list[tuple[str, str]] | str]:
    """Parse the `format filter { ... }` block: field -> regexp ops, or "delete".

    Anything else in the block (another filter type, an unknown field) fails,
    so a new logged field cannot slip in unreviewed.
    """
    (block,) = re.findall(r"\n(\t+)format filter \{\n(.*?)\n\1\}", text, re.S)
    lines = block[1].splitlines()
    filters: dict[str, list[tuple[str, str]] | str] = {}
    i = 0
    while i < len(lines):
        field, kind = lines[i].split()[:2]
        if kind == "delete":
            filters[field] = "delete"
            i += 1
            continue
        assert kind == "multi_regexp", lines[i]
        ops = []
        i += 1
        while lines[i].strip() != "}":
            op = re.fullmatch(
                r'\s*regexp "((?:[^"\\]|\\.)*)" "((?:[^"\\]|\\.)*)"', lines[i]
            )
            assert op, lines[i]
            ops.append((op[1], op[2]))
            i += 1
        filters[field] = ops
        i += 1
    return filters


def test_caddy_access_log_drops_every_header() -> None:
    # htmx sends HX-Current-URL (the display URL) on every poll, and Django's
    # APPEND_SLASH redirect puts it in Location: no header is logged at all.
    filters = _log_filters(TEMPLATE.read_text())
    assert filters == {
        "request>uri": filters["request>uri"],
        "request>headers": "delete",
        "resp_headers": "delete",
    }


def test_log_filter_parser_rejects_unknown_fields() -> None:
    text = TEMPLATE.read_text().replace(
        "\t\t\tresp_headers delete", "\t\t\tresp_headers delete\n\t\t\tuser_id hash"
    )
    with pytest.raises(AssertionError):
        _log_filters(text)


def _apply(ops: list[tuple[str, str]] | str, value: str) -> str:
    """Apply Caddy's regexp replacements (RE2 `${1}` -> Python `\\g<1>`)."""
    assert isinstance(ops, list)
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
    assert _apply(filters["request>uri"], uri) == logged
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
    # Only gunicorn's user and the caddy group may reach the socket: the
    # directory is 0750 and handed to group caddy (caddy's primary group, so
    # no Caddy restart) before gunicorn starts.
    assert unit["Service.RuntimeDirectoryMode"] == ["0750"]
    assert "up.sh refuses a server without a caddy group" in WEB_UNIT_TEXT()
    assert unit["Service.ExecStartPre"] == [
        "+/usr/bin/chgrp caddy /run/conference-drawing"
    ]
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


def _snapshot_root(fakes: Fakes, dump: bytes = DUMP_BYTES) -> Path:
    """A data dir, the lock file, and a fake pg_dump that writes `dump`."""
    data = fakes.root / "root" / "srv" / "conference-drawing" / "data"
    data.mkdir(parents=True)
    (data.parent / ".maintenance.lock").touch()
    canned = make_dump(fakes.root / "canned" / "db.dump", dump)
    fakes.extra_env["CD_ROOT"] = str(fakes.root / "root")
    fakes.set("flock", [{"match": "--wait"}])
    fakes.set(
        "pg_dump", [{"match": "-Fc", "copy_to_flag": "--file", "file": str(canned)}]
    )
    fakes.set("pg_restore", [{"match": "-f /dev/null", "require_magic": "PGDMP"}])
    return data


def test_snapshot_keeps_newest_48(fakes: Fakes) -> None:
    data = _snapshot_root(fakes)
    snaps = data / "snapshots"
    snaps.mkdir()
    for hour in range(50):
        (snaps / f"db-20000101T{hour % 24:02d}{hour // 24:02d}00Z.dump").write_text("")
    keep = snaps / "notes.txt"  # not a snapshot: never pruned
    keep.write_text("x")
    result = fakes.run([SNAPSHOT])
    assert result.returncode == 0, result.stderr
    names = sorted(p.name for p in snaps.glob("db-*.dump"))
    assert len(names) == 48
    newest = snaps / names[-1]
    assert not newest.name.startswith("db-2000")
    assert newest.read_bytes() == DUMP_BYTES
    assert keep.exists()
    assert [c.argv[:2] for c in fakes.calls("flock")] == [["--wait", "600"]]
    assert os_mode(newest) & 0o077 == 0
    (dump,) = fakes.calls("pg_dump")
    assert dump.argv[:3] == ["-Fc", "--file", f"{newest}.partial"]
    assert dump.argv[3:] == ["--dbname=conference_drawing"]
    # Verified by reading every block (pg_restore --list reads only the TOC).
    (verify,) = fakes.calls("pg_restore")
    assert verify.argv == ["-f", "/dev/null", f"{newest}.partial"]


def test_snapshot_removes_its_partial_file_on_failure(fakes: Fakes) -> None:
    data = _snapshot_root(fakes, dump=b"this is not a dump" * 100)
    result = fakes.run([SNAPSHOT])
    assert result.returncode != 0
    assert "verification" in result.stderr
    assert list((data / "snapshots").iterdir()) == []


def test_snapshot_fails_when_pg_dump_fails(fakes: Fakes) -> None:
    data = _snapshot_root(fakes)
    fakes.set("pg_dump", [{"match": "-Fc", "exit": 1}])
    result = fakes.run([SNAPSHOT])
    assert result.returncode != 0
    assert list((data / "snapshots").iterdir()) == []
    assert fakes.calls("pg_restore") == []


def test_snapshot_skips_the_lock_its_caller_already_holds(fakes: Fakes) -> None:
    _snapshot_root(fakes)
    fakes.extra_env["CD_LOCK_HELD"] = "1"
    assert fakes.run([SNAPSHOT]).returncode == 0
    assert fakes.calls("flock") == []


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
    stage = _stage_of(keys[0])
    assert "-backup-" in stage
    assert keys == [
        f"mkdir -m 700 {stage}",
        f"bash {stage}/remote/backup.sh",
        "rm -rf /tmp/conference-drawing-backup.Ab12Cd34",
        f"rm -rf {stage}",
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


def test_down_uses_its_own_staging_dir_apart_from_backups(down: Fakes) -> None:
    assert down.run([DOWN, HOST]).returncode == 0
    made = [_stage_of(k) for k in _ssh_keys(down) if k.startswith("mkdir")]
    assert len(made) == 2
    assert "-down-" in made[0] and "-backup-" in made[1]
    (uninstall,) = [k for k in _ssh_keys(down) if "uninstall.sh" in k]
    assert uninstall == f"bash {made[0]}/remote/uninstall.sh"


def test_down_says_the_app_is_stopped_when_the_backup_fails(down: Fakes) -> None:
    _fail(down, "ssh", "/remote/backup.sh")
    result = down.run([DOWN, HOST])
    assert result.returncode != 0
    assert "stopped" in result.stderr
    assert (
        "systemctl start conference-drawing-web.service "
        "conference-drawing-drip.service conference-drawing-snapshot.timer"
    ) in result.stderr
    assert not any("uninstall.sh" in k for k in _ssh_keys(down))


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
    ("ssh", "getent group caddy"),
    ("ssh", "hostname -I"),
    ("dig", f"-t AAAA {DOMAIN}"),
    ("dig", f"{DOMAIN} A"),
    ("ssh", "uname -m"),
    ("ssh", "uv --version"),
    ("ssh", "test -L /srv"),
    ("ssh", "mkdir -m 700"),
    ("scp", f"{HOST}:{STAGE}"),
    ("ssh", "remote/install.sh"),
    ("curl", "/health"),
    ("curl", "/version"),
    ("curl", "/privacy/"),
    ("ssh", f"rm -rf {STAGE}"),
]
DOWN_STEPS: list[tuple[str, str]] = [
    ("git", "rev-parse HEAD"),
    ("ssh", "mkdir -m 700"),
    ("scp", f"{HOST}:{STAGE}"),
    ("ssh", "remote/outbox_status.sh"),
    ("ssh", "remote/stop.sh"),
    ("ssh", "remote/backup.sh"),
    ("scp", "conference-drawing-backup"),
    ("ssh", "rm -rf /tmp/conference-drawing-backup."),
    ("ssh", "remote/uninstall.sh"),
    ("ssh", f"rm -rf {STAGE}"),
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
    _fail(fakes, tool, match)
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
