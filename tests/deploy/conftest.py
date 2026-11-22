"""Harness for the deploy-script tests: a PATH with fakes and no network tools.

The scripts run with a PATH holding only the fakes (``tests/deploy/fakes``) and
symlinks to an allowlist of harmless host tools, so a real ``ssh``, ``scp``,
``curl``, ``dig``, ``uv`` or ``git`` can never be reached. The environment is
built from scratch (nothing inherited), ``HOME`` is a temp dir, and the only
host the fakes accept is ``example.invalid``.
"""

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
DEPLOY = REPO / "deploy"
FAKES = Path(__file__).resolve().parent / "fakes"

HOST = "example.invalid"
DOMAIN = "raffle.example.invalid"
HOST_IP = "203.0.113.10"
COMMIT = "0123456789abcdef0123456789abcdef01234567"
# Paths on the (fake) server, never touched on this machine.
RDIR = f"/tmp/conference-drawing-{COMMIT}"  # noqa: S108
REMOTE_BACKUP = "/tmp/conference-drawing-backup.Ab12Cd34/backup.tar.gz"  # noqa: S108

# Host tools the scripts and fakes may use. Nothing here can reach a network.
ALLOWED_TOOLS = (
    "awk basename bash cat chmod cp cut date dirname env find grep gzip head id "
    "ls mkdir mktemp mv rm sed sh sleep sort sqlite3 stat tail tar tee touch tr "
    "uname wc xargs"
).split()
NETWORK_TOOLS = {"ssh", "scp", "curl", "dig", "uv", "git", "wget", "rsync", "nc"}
SYSTEM_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"

Entry = dict[str, Any]


def _which(tool: str) -> str:
    found = shutil.which(tool, path=SYSTEM_PATH) or shutil.which(tool)
    if found is None:
        pytest.fail(f"host tool {tool!r} is missing; the deploy tests need it")
    return found


@dataclass
class Call:
    tool: str
    argv: list[str]
    stdin: str

    @property
    def key(self) -> str:
        """The remote command for ssh, else the joined arguments."""
        if self.tool != "ssh":
            return " ".join(self.argv)
        args = self.argv
        i = 0
        while i < len(args) and args[i].startswith("-"):
            i += 2 if args[i] in {"-o", "-p", "-i", "-l", "-F", "-J", "-E"} else 1
        return " ".join(args[i + 1 :])


@dataclass
class Fakes:
    """Canned responses, the call log, and a runner for one test."""

    root: Path
    responses: dict[str, list[Entry]] = field(default_factory=dict)
    extra_env: dict[str, str] = field(default_factory=dict)

    @property
    def bin(self) -> Path:
        return self.root / "bin"

    @property
    def log(self) -> Path:
        return self.root / "fake-log.jsonl"

    def set(self, tool: str, entries: list[Entry]) -> None:
        self.responses[tool] = entries

    def prepend(self, tool: str, entry: Entry) -> None:
        """Add `entry` ahead of the defaults (first match wins)."""
        self.responses.setdefault(tool, []).insert(0, entry)

    def env(self) -> dict[str, str]:
        tmp = self.root / "tmp"
        tmp.mkdir(exist_ok=True)
        home = self.root / "home"
        home.mkdir(exist_ok=True)
        return {
            "PATH": f"{FAKES}:{self.bin}",
            "HOME": str(home),
            "TMPDIR": str(tmp),
            "LC_ALL": "C",
            "FAKE_LOG": str(self.log),
            "FAKE_RESPONSES": str(self.root / "responses.json"),
            "FAKE_HOST": HOST,
            **self.extra_env,
        }

    def run(
        self, args: list[str], stdin: str = "", cwd: Path | None = None
    ) -> subprocess.CompletedProcess[str]:
        """Run `args` (``args[0]`` a script path) with bash and the fake PATH."""
        (self.root / "responses.json").write_text(json.dumps(self.responses))
        return subprocess.run(  # noqa: S603 - fixed test inputs
            [str(self.bin / "bash"), *args],
            env=self.env(),
            input=stdin,
            capture_output=True,
            text=True,
            timeout=60,
            cwd=cwd or self.root,
            check=False,
        )

    def calls(self, tool: str | None = None) -> list[Call]:
        if not self.log.exists():
            return []
        calls = [Call(**json.loads(line)) for line in self.log.read_text().splitlines()]
        return [c for c in calls if tool is None or c.tool == tool]


@pytest.fixture
def fakes(tmp_path: Path) -> Iterator[Fakes]:
    """A fake toolbox rooted in `tmp_path`, with no network tool on PATH."""
    box = Fakes(root=tmp_path)
    box.bin.mkdir()
    for tool in ALLOWED_TOOLS:
        (box.bin / tool).symlink_to(_which(tool))
    (box.bin / "python3").symlink_to(Path(sys.executable).resolve())
    for tool in NETWORK_TOOLS:
        assert not (box.bin / tool).exists()
    assert {p.name for p in FAKES.iterdir()} >= {"ssh", "scp", "dig", "curl"}
    yield box


def make_backup_tarball(path: Path) -> Path:
    """Write a small real backup tarball (db.sqlite3 plus media/) to `path`."""
    import sqlite3
    import tarfile

    work = path.parent / "tarball-src"
    (work / "media" / "covers").mkdir(parents=True)
    with closing(sqlite3.connect(work / "db.sqlite3")) as db, db:
        db.execute("create table t (x)")
    (work / "media" / "covers" / "c.png").write_bytes(b"png")
    with tarfile.open(path, "w:gz") as tar:
        tar.add(work / "db.sqlite3", arcname="db.sqlite3")
        tar.add(work / "media", arcname="media")
    return path


def os_mode(path: Path) -> int:
    return os.stat(path).st_mode & 0o777
