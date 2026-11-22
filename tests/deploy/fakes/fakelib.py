"""Shared body of the fake ``ssh``, ``scp``, ``dig``, ``curl``, ``uv``, ``git``,
``caddy``, ``systemctl``, ``flock``, ``apt-get``, ``dpkg-query``, ``psql``,
``pg_dump`` and ``pg_restore`` used by the deploy-script tests.

Each fake appends one JSON line to ``$FAKE_LOG`` (tool, argv, stdin) and replays
the first canned response in ``$FAKE_RESPONSES`` whose ``match`` is a substring
of the call's key (or equals it, with ``"exact": true``). The key is the remote
command for ``ssh`` and the joined arguments for everything else. Anything
unmatched exits 97, so a script step the test did not expect fails loudly
instead of reaching a real machine.

``ssh`` and ``scp`` also refuse every host but ``$FAKE_HOST`` (exit 97). An
entry with ``"passthrough": true`` runs the command after ``--`` instead (the
fake ``runuser`` runs shell helpers this way, as the current user).

``ssh`` and ``psql`` also log what they read on stdin (the remote command's
input; the SQL a script feeds psql). An entry with ``"require_magic"`` fails
like ``pg_restore`` on a file that is not a custom-format dump: the last
argument must be a file starting with those bytes.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

UNKNOWN = 97
# ssh options that take a value, so the fake can find the destination.
_SSH_VALUE_OPTIONS = {"-o", "-p", "-i", "-l", "-F", "-J", "-E"}


def _ssh_split(args: list[str]) -> tuple[str, str]:
    """Return (destination, remote command) from ssh's arguments."""
    i = 0
    while i < len(args) and args[i].startswith("-"):
        i += 2 if args[i] in _SSH_VALUE_OPTIONS else 1
    if i >= len(args):
        return "", ""
    return args[i], " ".join(args[i + 1 :])


def _host_of(destination: str) -> str:
    """Strip a ``user@`` prefix and a ``:path`` suffix."""
    return destination.split("@", 1)[-1].split(":", 1)[0]


def _scp(entry: dict[str, Any], args: list[str]) -> None:
    """Simulate a transfer.

    A download copies the canned ``file`` to the local destination; an upload
    copies its local sources into ``$FAKE_SCP_UPLOADS`` (if set) for inspection.
    """
    source = entry.get("file")
    if not source:
        uploads = os.environ.get("FAKE_SCP_UPLOADS")
        if uploads and ":" in args[-1]:
            for arg in args[:-1]:
                path = Path(arg)
                if arg.startswith("-") or not path.exists():
                    continue
                target = Path(uploads) / path.name
                if path.is_dir():
                    shutil.copytree(path, target, dirs_exist_ok=True)
                else:
                    shutil.copyfile(path, target)
        return
    destination = Path(args[-1])
    if destination.is_dir():
        destination = destination / Path(args[-2].split(":", 1)[-1]).name
    shutil.copyfile(source, destination)


def _touch_after(entry: dict[str, Any], args: list[str]) -> None:
    """Create ``name`` inside the directory given after ``flag`` (``uv build``)."""
    spec = entry.get("touch_after")
    if not spec:
        return
    flag, name = spec
    directory = Path(args[args.index(flag) + 1])
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text("fake build output\n")


def _copy_to_flag(entry: dict[str, Any], args: list[str]) -> None:
    """Copy the canned ``file`` to the path after ``copy_to_flag`` (``curl -o``)."""
    flag = entry.get("copy_to_flag")
    if flag:
        shutil.copyfile(entry["file"], args[args.index(flag) + 1])


def _passthrough(args: list[str]) -> int:
    """Run the command after ``--`` as the current user (fake ``runuser``)."""
    command = args[args.index("--") + 1 :]
    # Like the real runuser, pass every open descriptor on to the command.
    return subprocess.run(command, check=False, close_fds=False).returncode  # noqa: S603


def _env_file_rules(entry: dict[str, Any]) -> None:
    """Stand in for the app's settings (fake ``runuser`` of a manage command).

    ``require_env_key``: fail like Django's ImproperlyConfigured unless
    ``$CD_ENV_FILE`` has that key. ``ensure_env_line``: append the line unless
    its key is there (what ``init`` does for DJANGO_SECRET_KEY).
    """
    path = Path(os.environ.get("CD_ENV_FILE", "/nonexistent"))
    text = path.read_text() if path.exists() else ""
    keys = {line.split("=", 1)[0] for line in text.splitlines()}
    required = entry.get("require_env_key")
    if required and required not in keys:
        print(f"ImproperlyConfigured: {required} is not set", file=sys.stderr)
        sys.exit(1)
    line = entry.get("ensure_env_line")
    if line and line.split("=", 1)[0] not in keys:
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a") as env:
            env.write(line + "\n")


def _require_magic(entry: dict[str, Any], args: list[str]) -> None:
    """Refuse, as pg_restore does, an archive without the expected header."""
    magic = entry.get("require_magic")
    if not magic:
        return
    path = Path(args[-1]) if args else Path("/nonexistent")
    if not path.is_file() or not path.read_bytes().startswith(magic.encode()):
        print(
            "pg_restore: error: input file does not appear to be a valid archive",
            file=sys.stderr,
        )
        sys.exit(1)


def main() -> None:
    tool = Path(sys.argv[0]).name
    args = sys.argv[1:]
    log_path = os.environ.get("FAKE_LOG")
    responses_path = os.environ.get("FAKE_RESPONSES")
    if not log_path or not responses_path:
        print(f"fake {tool}: FAKE_LOG/FAKE_RESPONSES not set", file=sys.stderr)
        sys.exit(UNKNOWN)

    stdin = ""
    if tool in {"ssh", "psql"} and not sys.stdin.isatty():
        stdin = sys.stdin.read()
    with open(log_path, "a") as log:
        log.write(json.dumps({"tool": tool, "argv": args, "stdin": stdin}) + "\n")

    allowed_host = os.environ.get("FAKE_HOST", "example.invalid")
    if tool == "ssh":
        destination, key = _ssh_split(args)
        if _host_of(destination) != allowed_host:
            print(f"fake ssh: refusing host {destination!r}", file=sys.stderr)
            sys.exit(UNKNOWN)
    else:
        key = " ".join(args)
    if tool == "scp":
        remotes = [a for a in args if ":" in a and not a.startswith("-")]
        if not remotes or any(_host_of(a) != allowed_host for a in remotes):
            print(f"fake scp: refusing {remotes!r}", file=sys.stderr)
            sys.exit(UNKNOWN)

    table: dict[str, list[dict[str, Any]]] = json.loads(
        Path(responses_path).read_text()
    )
    for entry in table.get(tool, []):
        if entry.get("exact") and entry["match"] != key:
            continue
        if entry["match"] in key:
            _env_file_rules(entry)
            _require_magic(entry, args)
            if entry.get("passthrough"):
                sys.exit(_passthrough(args))
            if tool == "scp":
                _scp(entry, args)
            _touch_after(entry, args)
            _copy_to_flag(entry, args)
            if entry.get("mkdir_last"):  # `uv venv <dir>` creates <dir>
                Path(args[-1]).mkdir(parents=True, exist_ok=True)
            sys.stdout.write(entry.get("stdout", ""))
            sys.stderr.write(entry.get("stderr", ""))
            sys.exit(int(entry.get("exit", 0)))
    print(f"fake {tool}: no canned response for {key!r}", file=sys.stderr)
    sys.exit(UNKNOWN)
