"""Fixtures for the browser tests: live server, phone and desktop pages, CSP watch.

Everything here is only collected under ``tests/e2e``; the default suite runs
with ``-m "not e2e"`` and never starts a browser.
"""

import os
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from playwright.sync_api import (
    Browser,
    BrowserContext,
    ConsoleMessage,
    Page,
    ViewportSize,
)

from conference_drawing.raffle.models import Conference, Drawing, PrizeLine
from tests import factories

PHONE = ViewportSize(width=390, height=844)
DESKTOP = ViewportSize(width=1280, height=800)

# Records every CSP violation twice: in the page (window.__cspViolations, which
# the tour reads on each page) and through a binding that outlives navigation.
CSP_WATCH = """
window.__cspViolations = [];
window.__cspPending = [];
document.addEventListener("securitypolicyviolation", (event) => {
  const entry = event.violatedDirective + " " + event.blockedURI;
  window.__cspViolations.push(entry);
  window.__cspPending.push(window.__reportCsp(entry));
});
"""


class CspWatch:
    """Collects CSP violations and CSP-looking console messages from a context."""

    def __init__(self) -> None:
        self.violations: list[str] = []
        self.console: list[str] = []

    def watch_page(self, page: Page) -> None:
        def on_console(message: ConsoleMessage) -> None:
            if is_csp_message(message):
                self.console.append(message.text)

        page.on("console", on_console)


def is_csp_message(message: ConsoleMessage) -> bool:
    """True for the console lines Chromium prints when it blocks something."""
    return "Content Security Policy" in message.text or "Refused to" in message.text


@pytest.fixture(autouse=True, scope="session")
def _allow_orm_beside_playwright() -> Iterator[None]:
    """Playwright's sync API keeps an event loop running in the test thread.

    Django refuses ORM calls from inside one unless told otherwise. Setting it
    through a session fixture that exists only under tests/e2e scopes it to the
    e2e run (so the default suite keeps Django's protection) and, being autouse
    at session scope, it is set before the browser starts and the test database
    is created.
    """
    with pytest.MonkeyPatch.context() as patch:
        patch.setitem(os.environ, "DJANGO_ALLOW_ASYNC_UNSAFE", "true")
        yield


@pytest.fixture
def csp_watch() -> CspWatch:
    return CspWatch()


@pytest.fixture
def make_context(
    browser: Browser, csp_watch: CspWatch
) -> Iterator[Callable[..., BrowserContext]]:
    """Return a factory for contexts of a given size, each watching for CSP trouble."""
    contexts: list[BrowserContext] = []

    def make(viewport: ViewportSize, **kwargs: Any) -> BrowserContext:
        context = browser.new_context(viewport=viewport, **kwargs)
        context.expose_function(
            "__reportCsp", lambda entry: csp_watch.violations.append(entry)
        )
        context.add_init_script(CSP_WATCH)
        context.on("page", csp_watch.watch_page)
        contexts.append(context)
        return context

    yield make
    for context in contexts:
        context.close()


@pytest.fixture
def phone_page(make_context: Callable[..., BrowserContext]) -> Page:
    """An anonymous attendee's phone (iPhone-sized viewport)."""
    return make_context(PHONE, is_mobile=True, has_touch=True).new_page()


@pytest.fixture
def owner_cookie(transactional_db: None, live_server: Any) -> dict[str, Any]:
    """A signed-in staff session, as a cookie for the browser.

    Chosen over filling the admin login form: the login page is not what these
    tests are about, and a forced session keeps each test to its own flow.
    """
    owner = get_user_model().objects.create_user(
        "owner", is_staff=True, is_superuser=True
    )
    client = Client()
    client.force_login(owner)
    return {
        "name": "sessionid",
        "value": client.cookies["sessionid"].value,
        "url": live_server.url,
    }


@pytest.fixture
def owner_phone_page(
    make_context: Callable[..., BrowserContext], owner_cookie: dict[str, Any]
) -> Page:
    context = make_context(PHONE, is_mobile=True, has_touch=True)
    context.add_cookies([owner_cookie])  # type: ignore[list-item]
    return context.new_page()


@pytest.fixture
def owner_desktop_page(
    make_context: Callable[..., BrowserContext], owner_cookie: dict[str, Any]
) -> Page:
    context = make_context(DESKTOP)
    context.add_cookies([owner_cookie])  # type: ignore[list-item]
    return context.new_page()


@pytest.fixture
def desktop_page(make_context: Callable[..., BrowserContext]) -> Page:
    """The booth monitor (no login; the display URL carries a token)."""
    return make_context(DESKTOP).new_page()


@pytest.fixture
def conference(transactional_db: None) -> Conference:
    return factories.make_conference()


@pytest.fixture
def drawing(conference: Conference) -> Drawing:
    """A scheduled drawing: two physical copies and one digital, six entrants."""
    drawing = factories.make_drawing(conference)
    factories.make_prize_line(
        drawing, book=factories.make_book(title="Python Tricks"), quantity=2
    )
    factories.make_prize_line(
        drawing,
        book=factories.make_book(title="Pandas Workout (ebook)"),
        kind=PrizeLine.Kind.DIGITAL,
        url_template="https://example.com/redeem?code={code}",
        code_mode=PrizeLine.CodeMode.SHARED,
        shared_code="BOOKCLUB",
        position=1,
    )
    for n in range(6):
        factories.make_entrant(conference, name=f"Attendee {n}")
    return drawing
