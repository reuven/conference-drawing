"""Django's async guard stays on outside the browser tests, even after them.

Plain `pytest` deselects the e2e tests (pyproject addopts), so this proves the
point only in a run that includes them: `pytest -m ""` or a mixed selection,
where tests/e2e collects first and so runs before this."""

import asyncio
import os
import threading

import pytest
from django.core.exceptions import SynchronousOnlyOperation

from conference_drawing.raffle.models import Entrant


@pytest.mark.django_db(transaction=True)
def test_async_safety_is_back_on_after_browser_tests() -> None:
    """Django's async guard must still fire once the e2e tests have run.

    The e2e fixtures switch the guard off per test; this (unmarked, so it runs
    alongside them in `pytest -m ""`) proves the switch is gone and the guard
    still works.
    """
    assert "DJANGO_ALLOW_ASYNC_UNSAFE" not in os.environ
    errors: list[BaseException] = []

    async def sync_orm_inside_a_loop() -> None:
        try:
            Entrant.objects.count()
        except SynchronousOnlyOperation as exc:
            errors.append(exc)

    thread = threading.Thread(target=lambda: asyncio.run(sync_orm_inside_a_loop()))
    thread.start()
    thread.join()
    assert len(errors) == 1
