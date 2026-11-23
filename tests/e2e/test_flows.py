"""Core flows in a real browser: attendee, owner, display, flyer, CSP."""

import re
from pathlib import Path
from typing import Any

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone
from PIL import Image
from playwright.sync_api import Page, expect

from conference_drawing.raffle.models import (
    Conference,
    Drawing,
    Entrant,
    InstallSettings,
    Pick,
)
from tests.e2e.conftest import CspWatch, is_csp_message

pytestmark = pytest.mark.e2e

_TIMEOUT = 10_000  # an upper bound over the display's 2 s poll, not a sleep


def test_attendee_enters_on_phone(
    phone_page: Page, live_server: Any, conference: Conference
) -> None:
    phone_page.goto(f"{live_server.url}/c/{conference.slug}/")
    phone_page.get_by_label("Name:").fill("Dana Reader")
    phone_page.get_by_label("Email:").fill("dana@example.com")
    phone_page.get_by_role("checkbox").first.check()
    phone_page.get_by_role("button", name="Enter the drawing").click()
    expect(
        phone_page.get_by_role("heading", name="Thanks — you're in the drawing.")
    ).to_be_visible()
    assert Entrant.objects.filter(email="dana@example.com").count() == 1


def test_owner_runs_drawing_with_redraw(
    owner_phone_page: Page, live_server: Any, drawing: Drawing
) -> None:
    page = owner_phone_page
    page.goto(f"{live_server.url}/booth/drawing/{drawing.pk}/")
    page.get_by_role("button", name="Start drawing").click()
    expect(page.get_by_text("Open. 2 waiting to be marked.")).to_be_visible()

    # Tap Not here on the first pick: a replacement row joins its copy.
    first_slot = page.locator(".slot").first
    expect(first_slot.locator(".pick")).to_have_count(1)
    first_slot.get_by_role("button", name="Not here").click()
    expect(first_slot.locator(".pick")).to_have_count(2)
    expect(first_slot.locator(".pick-not_here")).to_have_count(1)
    expect(page.get_by_text("Open. 2 waiting to be marked.")).to_be_visible()

    claimed = page.get_by_role("button", name="Claimed")
    expect(claimed).to_have_count(2)
    claimed.first.click()
    expect(claimed).to_have_count(1)
    claimed.first.click()
    expect(page.get_by_text("Finished. 3 awarded.")).to_be_visible()
    assert Pick.objects.filter(status=Pick.Status.CLAIMED).count() == 2


def test_display_updates_after_drawing(
    owner_phone_page: Page,
    desktop_page: Page,
    live_server: Any,
    drawing: Drawing,
    conference: Conference,
) -> None:
    desktop_page.goto(f"{live_server.url}/display/{conference.display_token}/")
    panel = desktop_page.locator("#panel")
    expect(panel.get_by_role("heading", name=conference.name)).to_be_visible()
    expect(panel.locator(".big").last).to_have_text("6")  # entries so far
    expect(panel.get_by_role("heading", name="Drawing now")).not_to_be_visible()

    owner_phone_page.goto(f"{live_server.url}/booth/drawing/{drawing.pk}/")
    owner_phone_page.get_by_role("button", name="Start drawing").click()
    expect(owner_phone_page.get_by_text("Open. 2 waiting")).to_be_visible()

    # No sleep: the display's own 2 s poll swaps the panel in.
    expect(panel.get_by_role("heading", name="Drawing now")).to_be_visible(
        timeout=_TIMEOUT
    )
    expect(panel.get_by_role("heading", name="Python Tricks")).to_be_visible()
    expect(panel.locator("li")).to_have_count(3)


def test_flyer_prints_one_page(
    owner_desktop_page: Page, live_server: Any, drawing: Drawing
) -> None:
    owner_desktop_page.goto(f"{live_server.url}/booth/{drawing.conference.slug}/flyer/")
    expect(
        owner_desktop_page.get_by_text("Scan to enter to win a book.")
    ).to_be_visible()
    pdf = owner_desktop_page.pdf()
    assert pdf.startswith(b"%PDF")
    # Page objects read "/Type /Page"; the page tree reads "/Type /Pages". This
    # relies on Skia writing page dictionaries uncompressed; if that changes the
    # count becomes 0 and this fails loudly rather than passing. pypdf was not
    # added to the dev dependencies just to count pages.
    assert len(re.findall(rb"/Type\s*/Page(?![s\w])", pdf)) == 1


def test_no_csp_violations_on_tour(
    owner_phone_page: Page,
    desktop_page: Page,
    live_server: Any,
    drawing: Drawing,
    conference: Conference,
    csp_watch: CspWatch,
) -> None:
    base = live_server.url
    slug = conference.slug
    page = owner_phone_page
    for path in [
        f"/c/{slug}/",
        f"/c/{slug}/thanks/",
        "/privacy/",
        f"/booth/{slug}/",
        f"/booth/drawing/{drawing.pk}/",
        f"/booth/{slug}/flyer/",
    ]:
        page.goto(base + path)
        expect(page.locator("h1")).to_be_visible()
        assert page.evaluate("window.__cspViolations") == [], path

    # An open drawing, driven through htmx.
    page.goto(f"{base}/booth/drawing/{drawing.pk}/")
    page.get_by_role("button", name="Start drawing").click()
    expect(page.get_by_text("Open. 2 waiting")).to_be_visible()
    page.get_by_role("button", name="Not here").first.click()
    expect(page.locator(".pick-not_here")).to_have_count(1)
    assert page.evaluate("window.__cspViolations") == []

    # The display, across at least one poll (response armed before the goto).
    with desktop_page.expect_response(re.compile(r"/display/.+/panel/")) as poll:
        desktop_page.goto(f"{base}/display/{conference.display_token}/")
    assert poll.value.ok
    expect(desktop_page.get_by_role("heading", name="Drawing now")).to_be_visible()
    assert desktop_page.evaluate("window.__cspViolations") == []

    assert csp_watch.violations == []
    assert csp_watch.console == []


def test_csp_watch_catches_a_real_violation(
    phone_page: Page, live_server: Any, conference: Conference, csp_watch: CspWatch
) -> None:
    """Prove the tour's net is wired: break the real policy and watch it notice."""
    phone_page.goto(f"{live_server.url}/c/{conference.slug}/")
    with phone_page.expect_console_message(is_csp_message):
        phone_page.evaluate(
            "() => { const s = document.createElement('script');"
            " s.textContent = 'window.pwned = 1'; document.head.append(s); }"
        )
    phone_page.evaluate("Promise.all(window.__cspPending)")
    assert phone_page.evaluate("window.__cspViolations") == ["script-src-elem inline"]
    assert csp_watch.violations == ["script-src-elem inline"]
    assert len(csp_watch.console) == 1


def test_after_wrap_up_the_entry_url_shows_the_owner_page(
    phone_page: Page,
    live_server: Any,
    conference: Conference,
    csp_watch: CspWatch,
    settings: Any,
    tmp_path: Path,
) -> None:
    """The site's logo and link appear on a wrapped-up entry URL, CSP-clean."""
    settings.MEDIA_ROOT = tmp_path
    (tmp_path / "logos").mkdir()
    Image.new("RGB", (8, 8), "navy").save(tmp_path / "logos" / "site.png")
    install = InstallSettings.get()
    install.after_logo = "logos/site.png"
    install.after_markdown = "Thanks! See [our site](https://example.com/)."
    install.save()
    conference.wrapped_up_at = timezone.now()
    conference.save()

    responses: list[tuple[str, int]] = []
    phone_page.on("response", lambda r: responses.append((r.url, r.status)))
    phone_page.on("console", lambda m: None)
    phone_page.goto(f"{live_server.url}/c/{conference.slug}/")

    expect(
        phone_page.get_by_text("Entries for this conference are closed")
    ).to_be_visible()
    expect(phone_page.get_by_role("link", name="our site")).to_have_attribute(
        "href", "https://example.com/"
    )
    logo = phone_page.locator("img")
    expect(logo).to_be_visible()
    assert logo.evaluate("img => img.naturalWidth") > 0
    assert (f"{live_server.url}/media/logos/site.png", 200) in responses
    assert csp_watch.console == []
    assert csp_watch.violations == []


def test_admin_login_with_email(
    phone_page: Page, live_server: Any, transactional_db: None
) -> None:
    """The admin login accepts the owner's email address, in any case."""
    get_user_model().objects.create_user(
        "owner",
        email="owner@example.com",
        password="pw-for-e2e-only",
        is_staff=True,
        is_superuser=True,
    )
    phone_page.goto(f"{live_server.url}/admin/login/")
    phone_page.get_by_label("Username").fill("Owner@Example.com")
    phone_page.get_by_label("Password").fill("pw-for-e2e-only")
    phone_page.get_by_role("button", name="Log in").click()
    expect(
        phone_page.get_by_role("heading", name="Site administration")
    ).to_be_visible()
