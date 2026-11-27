"""The Drip outbox worker: send due jobs every few seconds until told to stop."""

import logging
import signal
import threading
from types import FrameType
from typing import Any

import httpx
from django.core.management.base import BaseCommand, CommandParser
from django.db import close_old_connections

from ...drip import outbox
from ...drip.client import DripClient
from ...drip.config import drip_settings

INTERVAL_SECONDS = 5
logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Send due Drip outbox jobs until SIGTERM/SIGINT (or once with --once)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--once", action="store_true", help="run one pass, exit")

    def handle(self, *args: Any, **options: Any) -> None:
        config = drip_settings()
        if config is None:
            self.stdout.write("Drip is not configured; nothing to send.")
            if options["once"]:
                return
        stop = threading.Event()

        def request_stop(signum: int, frame: FrameType | None) -> None:
            stop.set()

        previous = {
            sig: signal.signal(sig, request_stop)
            for sig in (signal.SIGTERM, signal.SIGINT)
        }
        try:
            with httpx.Client() as http:
                while True:
                    if config is not None:
                        try:
                            result = outbox.process_due(
                                DripClient(config, http),
                                should_stop=stop.is_set,
                            )
                        except Exception as exc:  # noqa: BLE001 - keep the worker up
                            # Class name only: messages can carry emails.
                            logger.error(
                                "drip worker pass failed: %s", type(exc).__name__
                            )
                        else:
                            # Quiet when idle: no 5 s heartbeat in the journal.
                            if result.sent or result.retried or result.failed:
                                self.stdout.write(
                                    f"sent={result.sent} retried={result.retried} "
                                    f"failed={result.failed}"
                                )
                    # An interruptible sleep: a signal sets `stop` and wakes us.
                    if options["once"] or stop.wait(INTERVAL_SECONDS):
                        break
                    close_old_connections()  # drop stale connections
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
