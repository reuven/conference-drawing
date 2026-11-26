"""The shipped source must carry no owner-specific names or defaults."""

import re
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
_OWNER = re.compile(r"better developers|lernerpython|\bbd_|-bd-", re.IGNORECASE)
_SUFFIXES = {".py", ".html", ".txt", ".md", ".js", ".css", ".toml"}


def test_source_has_no_owner_specific_names() -> None:
    """Migrations and `db_column=` lines may keep old names; nothing else may."""
    hits = [
        f"{path.relative_to(SRC)}:{number}"
        for path in SRC.rglob("*")
        if path.is_file()
        and path.suffix in _SUFFIXES
        and "migrations" not in path.parts
        and "__pycache__" not in path.parts
        for number, line in enumerate(path.read_text().splitlines(), start=1)
        if _OWNER.search(line) and "db_column=" not in line
    ]
    assert hits == []
