"""Admin forms: warn before leaving unsaved work, and jump to the first error.

Behavior tests drive real navigation. The coverage test at the end replays
each branch of `raffle/admin-forms.js` and reads V8's block coverage over
the Chrome DevTools Protocol, so the script stays at 100% without a Node
toolchain.
"""

from typing import Any

import pytest
from playwright.sync_api import Dialog, Page, expect

from tests.e2e.conftest import CspWatch

pytestmark = pytest.mark.e2e

ADD = "/admin/raffle/conference/add/"
SCRIPT = "raffle/admin-forms.js"


def _fill_valid_conference(page: Page) -> None:
    page.locator("#id_name").fill("Form Test Conf")
    page.locator("#id_slug").fill("form-test")
    page.locator("#id_time_zone").select_option("Asia/Jerusalem")
    page.locator("#id_starts_on").fill("2027-05-10")
    page.locator("#id_ends_on").fill("2027-05-12")


def _leave_by_link(page: Page) -> list[Dialog]:
    """Click the breadcrumb 'Home' link, dismissing any 'leave page?' prompt."""
    dialogs: list[Dialog] = []

    def on_dialog(dialog: Dialog) -> None:
        dialogs.append(dialog)
        dialog.dismiss()

    page.on("dialog", on_dialog)
    page.locator(".breadcrumbs a").first.click()
    return dialogs


def test_leaving_an_untouched_form_does_not_ask(
    owner_desktop_page: Page, live_server: Any
) -> None:
    page = owner_desktop_page
    page.goto(live_server.url + ADD)
    dialogs = _leave_by_link(page)
    expect(page).to_have_url(live_server.url + "/admin/")
    assert dialogs == []


def test_leaving_a_form_with_typed_changes_asks_first(
    owner_desktop_page: Page, live_server: Any
) -> None:
    page = owner_desktop_page
    page.goto(live_server.url + ADD)
    page.locator("#id_name").fill("Half-typed Conf")
    dialogs = _leave_by_link(page)
    assert [d.type for d in dialogs] == ["beforeunload"]
    expect(page).to_have_url(live_server.url + ADD)
    expect(page.locator("#id_name")).to_have_value("Half-typed Conf")


def test_a_failed_save_focuses_the_first_error_and_still_guards_the_data(
    owner_desktop_page: Page, live_server: Any, csp_watch: CspWatch
) -> None:
    page = owner_desktop_page
    page.goto(live_server.url + ADD)
    page.locator("#id_name").fill("Missing Slug Conf")
    page.get_by_role("button", name="Save", exact=True).click()
    expect(page.locator(".errornote")).to_be_visible()
    expect(page.locator("#id_slug")).to_be_focused()
    # What came back is still unsaved: leaving must ask, even before any typing.
    dialogs = _leave_by_link(page)
    assert [d.type for d in dialogs] == ["beforeunload"]
    expect(page.locator("#id_name")).to_have_value("Missing Slug Conf")
    assert csp_watch.console == []
    assert csp_watch.violations == []


def test_an_error_in_a_collapsed_section_is_shown_and_focused(
    owner_desktop_page: Page, live_server: Any
) -> None:
    page = owner_desktop_page
    page.goto(live_server.url + ADD)
    _fill_valid_conference(page)
    page.get_by_text("After a conference").click()  # open, to reach the select
    page.locator("#id_after_mode").select_option("redirect")
    page.get_by_role("button", name="Save", exact=True).click()
    expect(page.locator(".errornote")).to_be_visible()
    expect(page.locator("#id_after_redirect_url")).to_be_focused()
    expect(page.locator("#id_after_redirect_url")).to_be_visible()


def _uncovered(page: Page, snapshots: list[list[dict[str, Any]]]) -> str:
    """Source text of the script that no snapshot ever ran (V8 block coverage)."""
    source = page.evaluate("url => fetch(url).then(r => r.text())", f"/static/{SCRIPT}")
    ran = [False] * len(source)
    for functions in snapshots:
        counts = [0] * len(source)
        # V8 lists outer ranges before the nested ranges that refine them.
        for function in functions:
            for r in function["ranges"]:
                for i in range(r["startOffset"], min(r["endOffset"], len(source))):
                    counts[i] = r["count"]
        for i, count in enumerate(counts):
            ran[i] = ran[i] or count > 0
    return "".join(c for i, c in enumerate(source) if not ran[i] and not c.isspace())


def _error_page_html(page: Page, base: str) -> str:
    """Submit a conference with no slug and return the error page, as sent."""
    page.goto(base + ADD)
    page.locator("#id_name").fill("No slug")
    with page.expect_response(lambda r: r.request.method == "POST") as posted:
        page.get_by_role("button", name="Save", exact=True).click()
    return posted.value.text()


def test_admin_forms_script_is_fully_covered(
    owner_desktop_page: Page, live_server: Any
) -> None:
    """Union the script's V8 block coverage over one fresh tab per scenario.

    Each tab loads the page exactly once: a second navigation in the same tab
    reports the script without block detail (isBlockCoverage false). The
    error page is captured first, then served to the measured tab by GET.
    """
    base = live_server.url
    context = owner_desktop_page.context
    error_html = _error_page_html(owner_desktop_page, base)
    snapshots: list[list[dict[str, Any]]] = []

    def measure(path: str, act: Any, html: str | None = None) -> None:
        page = context.new_page()
        cdp = context.new_cdp_session(page)
        cdp.send("Profiler.enable")
        cdp.send("Profiler.startPreciseCoverage", {"callCount": True, "detailed": True})
        if html is not None:
            page.route(
                base + path,
                lambda route: route.fulfill(body=html, content_type="text/html"),
            )
        page.goto(base + path)
        act(page)
        result = cdp.send("Profiler.takePreciseCoverage")
        functions = [
            f
            for s in result["result"]
            if s["url"].endswith(SCRIPT)
            for f in s["functions"]
        ]
        assert any(f["isBlockCoverage"] for f in functions), path
        snapshots.append(functions)
        page.close(run_before_unload=False)

    def edit_and_leave(page: Page) -> None:
        page.evaluate("window.dispatchEvent(new Event('beforeunload'))")  # clean
        page.locator("#id_name").fill("Typed")  # input
        page.locator("#id_time_zone").select_option("Asia/Jerusalem")  # change
        page.evaluate("window.dispatchEvent(new Event('beforeunload'))")  # dirty
        page.evaluate(
            "document.querySelector('#conference_form')"
            ".dispatchEvent(new Event('submit'))"
        )

    measure("/admin/", lambda page: None)  # no change form: stops at once
    measure(ADD, edit_and_leave)
    measure(ADD, lambda page: None, error_html)  # a failed save

    assert _uncovered(owner_desktop_page, snapshots) == ""
