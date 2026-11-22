"""Fixtures for the browser tests: live server, phone and desktop pages, CSP watch.

Everything here is only collected under ``tests/e2e``; the default suite runs
with ``-m "not e2e"`` and never starts a browser.
"""

import os
from collections.abc import Callable, Generator, Iterator
from typing import Any

import pytest
from django.contrib.auth import get_user_model
from django.db import connections
from django.test import Client
from playwright.sync_api import (
    Browser,
    BrowserContext,
    BrowserType,
    ConsoleMessage,
    Page,
    Playwright,
    ViewportSize,
    sync_playwright,
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


@pytest.fixture
def playwright() -> Iterator[Playwright]:
    """Start Playwright per test instead of per session.

    Its sync API keeps an event loop alive in the test thread for as long as it
    runs, and Django refuses ORM calls (even in unrelated transactional tests)
    while one is. Stopping it after each e2e test means no loop outlives the
    test, so tests that run later in a mixed session see a normal thread.
    """
    with sync_playwright() as instance:
        yield instance


@pytest.fixture
def browser_type(playwright: Playwright, browser_name: str) -> BrowserType:
    browser_type: BrowserType = getattr(playwright, browser_name)
    return browser_type


@pytest.fixture
def browser(
    browser_type: BrowserType, browser_type_launch_args: dict[str, Any]
) -> Iterator[Browser]:
    browser = browser_type.launch(**browser_type_launch_args)
    yield browser
    browser.close()


@pytest.fixture(autouse=True)
def _allow_orm_beside_playwright(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Let tests in this directory use the ORM while Playwright's loop is running.

    Playwright's sync API keeps an event loop in the test thread, and Django
    refuses ORM calls inside one unless DJANGO_ALLOW_ASYNC_UNSAFE is set. It is
    function-scoped (and the fixture lives only in tests/e2e), so monkeypatch
    removes it after every test and no test outside this directory ever sees
    it, whatever the collection mix or order. (The loop itself is why the
    playwright and browser fixtures above are per test.)
    """
    monkeypatch.setitem(os.environ, "DJANGO_ALLOW_ASYNC_UNSAFE", "true")
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


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item: pytest.Item) -> Generator[None]:
    """Close the connection the test body's own ORM calls opened.

    Under Playwright the body runs in a different context from the fixtures, so
    its connection is not the one fixture teardown sees. Left open it would
    block dropping the test database at the end of the session.
    """
    try:
        yield
    finally:
        connections.close_all()
