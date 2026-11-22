"""Console entry point: ``conference-drawing <command>`` behaves like manage.py."""

import os
import sys


def main(argv: list[str] | None = None) -> int:
    """Run a Django management command and return a process exit status."""
    from django.core.management import execute_from_command_line

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "conference_drawing.settings")
    args = sys.argv[1:] if argv is None else argv
    execute_from_command_line(["conference-drawing", *args])
    return 0
