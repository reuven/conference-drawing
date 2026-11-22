"""Print outbox counts; exit 1 while anything is pending (used before deploys)."""

import sys
from typing import Any

from django.core.management.base import BaseCommand

from ...drip import outbox


class Command(BaseCommand):
    help = "Print pending and failed Drip outbox counts; exit 1 if any are pending."

    def handle(self, *args: Any, **options: Any) -> None:
        pending, failed = outbox.pending_count(), outbox.failed_count()
        self.stdout.write(f"pending={pending} failed={failed}")
        if pending:
            sys.exit(1)
