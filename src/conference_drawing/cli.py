"""Console entry point: ``conference-drawing <command>``.

``init`` is the one command a new self-hoster runs after ``uv sync``; every other
command behaves like ``manage.py``.

Bootstrapping order for ``init`` (settings need a secret key at import time, and
the key lives in the file ``init`` is about to create):

1. Decide the .env path and write the missing keys. Nothing Django-related has
   been imported yet, so no settings module has been evaluated.
2. Export ``CD_ENV_FILE`` so ``settings.py`` reads exactly that file, then set up
   Django. Settings now import with the freshly written secret key.
3. ``migrate``, ``createcachetable``, the superuser, and ``InstallSettings``.
"""

import argparse
import ipaddress
import os
import re
import secrets
import stat
import sys
from collections.abc import Mapping
from pathlib import Path

from django.core.management import call_command, execute_from_command_line
from dotenv import dotenv_values

BASE_DIR = Path(__file__).resolve().parent.parent.parent

_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_LABEL_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_SUPERUSER_VARS = (
    "DJANGO_SUPERUSER_USERNAME",
    "DJANGO_SUPERUSER_EMAIL",
    "DJANGO_SUPERUSER_PASSWORD",
)


def parse_hostname(value: str) -> str:
    """Return ``value`` lowercased if it is a bare hostname or IPv4 address.

    Why: the result goes into ``DJANGO_ALLOWED_HOSTS``, a comma-separated list, so
    a scheme, path, port, comma, or whitespace would silently produce a host that
    never matches (or, worse, several hosts). Reject them up front.
    """
    host = value.lower()
    try:
        ipaddress.IPv4Address(host)
    except ValueError:
        pass
    else:
        return host
    labels = host.split(".")
    if len(host) > 253 or not all(_LABEL_RE.fullmatch(label) for label in labels):
        raise argparse.ArgumentTypeError(
            f"{value!r} is not a bare hostname (no scheme, path, port, comma, "
            "or whitespace)"
        )
    return host


def resolve_env_file(explicit: Path | None) -> Path:
    """Pick the .env path: ``--env-file``, then ``CD_ENV_FILE``, then BASE_DIR/.env.

    Mirrors settings.py, and like it never looks at the home directory.
    """
    if explicit is not None:
        return explicit
    from_env = os.environ.get("CD_ENV_FILE")
    return Path(from_env) if from_env else BASE_DIR / ".env"


def format_env_line(key: str, value: str) -> str:
    """Render one ``KEY='value'`` line that python-dotenv reads back unchanged.

    Single quotes make ``#``, ``$``, double quotes and spaces literal; only
    backslash and the single quote need escaping. python-dotenv interpolates
    ``${NAME}`` even inside quotes and has no escape for it, so such a value
    cannot round-trip and is refused.
    """
    if not _KEY_RE.fullmatch(key):
        raise ValueError(f"invalid env key: {key!r}")
    if "${" in value:
        raise ValueError("values containing '${' would be subject to interpolation")
    escaped = value.replace("\\", "\\\\").replace("'", "\\'")
    return f"{key}='{escaped}'\n"


def write_env_values(path: Path, values: Mapping[str, str]) -> bool:
    """Append ``values`` to the env file, creating it 0600; return True if it tightened.

    The file is created with ``O_EXCL`` and mode 0600, so it is never readable by
    others, not even briefly. An existing file with looser permissions is
    tightened to 0600 before anything is appended (the return value says so).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(format_env_line(k, v) for k, v in values.items())
    tightened = False
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if stat.S_IMODE(path.stat().st_mode) & 0o077:
            path.chmod(0o600)
            tightened = True
        existing = path.read_text()
        if existing and not existing.endswith("\n"):
            text = "\n" + text
        with path.open("a") as handle:
            handle.write(text)
    else:
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
    return tightened


def _tighten_only(path: Path) -> bool:
    """Tighten an existing file's mode to 0600 when nothing needs writing."""
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        path.chmod(0o600)
        return True
    return False


def _prompt_domains() -> list[str]:
    """Ask for hostnames on a terminal; whitespace separates them."""
    answer = input("Domain(s) this site is served from (space-separated): ")
    hosts = answer.split()
    if not hosts:
        raise argparse.ArgumentTypeError("at least one domain is required")
    return [parse_hostname(h) for h in hosts]


def _ensure_superuser(non_interactive: bool) -> int:
    """Create the superuser; a no-op if it (or, without env vars, any) exists."""
    from django.contrib.auth import get_user_model

    user_model = get_user_model()
    if not non_interactive:
        if user_model.objects.filter(is_superuser=True).exists():
            print("superuser already exists; left alone")
        else:
            call_command("createsuperuser", interactive=True)
        return 0
    missing = [name for name in _SUPERUSER_VARS if not os.environ.get(name)]
    if missing:
        if user_model.objects.filter(is_superuser=True).exists():
            print("superuser already exists; left alone")
            return 0
        print(
            "error: --non-interactive needs these environment variables to create "
            f"the superuser: {', '.join(missing)}",
            file=sys.stderr,
        )
        return 2
    username = os.environ["DJANGO_SUPERUSER_USERNAME"]
    if user_model.objects.filter(username=username).exists():
        print("superuser already exists; left alone")
        return 0
    # createsuperuser reads DJANGO_SUPERUSER_PASSWORD itself; it is never echoed.
    call_command(
        "createsuperuser",
        interactive=False,
        username=username,
        email=os.environ["DJANGO_SUPERUSER_EMAIL"],
    )
    print("created superuser")
    return 0


def _init(args: argparse.Namespace) -> int:
    """Run first-time setup; safe to run again."""
    path = resolve_env_file(args.env_file)
    existing = dotenv_values(path) if path.exists() else {}

    to_write: dict[str, str] = {}
    if not existing.get("DJANGO_SECRET_KEY"):
        to_write["DJANGO_SECRET_KEY"] = secrets.token_urlsafe(50)
    if existing.get("DJANGO_ALLOWED_HOSTS"):
        print("kept DJANGO_ALLOWED_HOSTS")
    else:
        domains: list[str] = args.domain or []
        if not domains and args.non_interactive:
            print(
                "error: --domain is required with --non-interactive when no "
                "DJANGO_ALLOWED_HOSTS is set",
                file=sys.stderr,
            )
            return 2
        if not domains:
            try:
                domains = _prompt_domains()
            except argparse.ArgumentTypeError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 2
        to_write["DJANGO_ALLOWED_HOSTS"] = ",".join(domains)

    tightened = False
    if to_write:
        tightened = write_env_values(path, to_write)
    else:  # complete file: nothing to write, but still keep it private
        tightened = _tighten_only(path)
    for key in to_write:
        print(f"wrote {key}")
    if "DJANGO_SECRET_KEY" not in to_write:
        print("kept DJANGO_SECRET_KEY")
    if tightened:
        print(f"tightened permissions on {path} to 0600")

    # Settings read the file we just wrote (real environment variables still win).
    os.environ["CD_ENV_FILE"] = str(path)
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "conference_drawing.settings")
    import django

    django.setup()

    from django.conf import settings

    Path(settings.DATA_DIR).mkdir(parents=True, exist_ok=True)
    call_command("migrate", interactive=False, verbosity=1)
    call_command("createcachetable", verbosity=0)
    print("cache table ready")
    status = _ensure_superuser(args.non_interactive)
    if status:
        return status

    from conference_drawing.raffle.models import InstallSettings

    InstallSettings.get()
    print("install settings ready")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="conference-drawing init",
        description="Write .env, migrate, create the cache table, the superuser, "
        "and the install settings. Safe to run again.",
    )
    parser.add_argument("--env-file", type=Path, help="default: $CD_ENV_FILE or .env")
    parser.add_argument(
        "--domain",
        nargs="+",
        type=parse_hostname,
        metavar="HOST",
        help="hostname(s) the site is served from (DJANGO_ALLOWED_HOSTS)",
    )
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="no prompts; the superuser comes from DJANGO_SUPERUSER_USERNAME, "
        "DJANGO_SUPERUSER_EMAIL and DJANGO_SUPERUSER_PASSWORD",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run ``init`` or a Django management command; return a process exit status."""
    args = sys.argv[1:] if argv is None else argv
    if args[:1] == ["init"]:
        return _init(_parser().parse_args(args[1:]))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "conference_drawing.settings")
    execute_from_command_line(["conference-drawing", *args])
    return 0
