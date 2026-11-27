"""The booth sign-in page: helpers sign in without the admin, and sign out."""

import datetime
import unicodedata
from collections.abc import Callable
from typing import Any
from unittest import mock

import pytest
from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.cache.backends import db as db_cache
from django.test import Client, RequestFactory
from django.urls import reverse
from django.utils import timezone
from django_ratelimit.exceptions import Ratelimited
from hypothesis import example, given
from hypothesis import strategies as st

from conference_drawing.auth import _record_failure, login_failure_key
from conference_drawing.raffle.models import Conference, Drawing
from conference_drawing.raffle.views.public import rate_limited

LOGIN = "/booth/login/"


@pytest.fixture
def helper(db: None) -> User:
    return User.objects.create_user(
        "helper-one", "Helper.One@example.com", password="pw-12345"
    )


def test_urls() -> None:
    assert reverse("booth-login") == LOGIN
    assert reverse("booth-logout") == "/booth/logout/"


def test_label_says_email_works_too(client: Client) -> None:
    html = client.get(LOGIN).content.decode()
    assert "Username or email" in html


def test_helper_signs_in_by_username(client: Client, helper: User) -> None:
    response = client.post(LOGIN, {"username": "helper-one", "password": "pw-12345"})
    assert (response.status_code, response["Location"]) == (302, "/booth/")


def test_helper_signs_in_by_email_in_any_case(client: Client, helper: User) -> None:
    response = client.post(
        LOGIN, {"username": "HELPER.one@EXAMPLE.com", "password": "pw-12345"}
    )
    assert (response.status_code, response["Location"]) == (302, "/booth/")


def test_inactive_user_cannot_sign_in(client: Client, helper: User) -> None:
    helper.is_active = False
    helper.save()
    response = client.post(LOGIN, {"username": "helper-one", "password": "pw-12345"})
    assert response.status_code == 200
    assert "_auth_user_id" not in client.session


def test_wrong_password_shows_the_standard_error(client: Client, helper: User) -> None:
    response = client.post(LOGIN, {"username": "helper-one", "password": "nope"})
    assert response.status_code == 200
    assert "Please enter a correct" in response.content.decode()


def test_next_is_honored_when_local(client: Client, helper: User) -> None:
    response = client.post(
        LOGIN + "?next=/booth/a/", {"username": "helper-one", "password": "pw-12345"}
    )
    assert response["Location"] == "/booth/a/"


@pytest.mark.parametrize(
    "nxt",
    [
        "https://evil.example/",
        "//evil.example/",
        "/\\evil.example/",
        "https:evil.example",
        "///evil.example/",
    ],
)
def test_next_to_another_site_is_ignored(
    client: Client, helper: User, nxt: str
) -> None:
    response = client.post(
        LOGIN,
        {"username": "helper-one", "password": "pw-12345", "next": nxt},
    )
    assert response["Location"] == "/booth/"


def test_signed_in_user_is_sent_to_the_booth(client: Client, helper: User) -> None:
    client.force_login(helper)
    response = client.get(LOGIN)
    assert (response.status_code, response["Location"]) == (302, "/booth/")


def test_signed_in_user_with_next_pointing_here_is_not_looped(
    client: Client, helper: User
) -> None:
    client.force_login(helper)
    response = client.get(LOGIN + "?next=/booth/login/")
    assert (response.status_code, response["Location"]) == (302, "/booth/")


@pytest.mark.usefixtures("frozen_ratelimit_clock")
def test_login_posts_are_rate_limited_with_their_own_counter(db: None) -> None:
    client = Client()
    # A different account each time, so only the address limit can answer 429.
    guess = {"username": "nobody", "password": "wrong"}
    who: dict[str, Any] = {"REMOTE_ADDR": "203.0.113.50"}
    statuses = {
        client.post(LOGIN, {**guess, "username": f"n{i}"}, **who).status_code
        for i in range(10)
    }
    assert statuses == {200}
    assert client.post(LOGIN, guess, **who).status_code == 429
    # The admin login keeps a separate counter for the same address.
    assert client.post(reverse("admin:login"), guess, **who).status_code == 200


def test_logout_refuses_get(client: Client, helper: User) -> None:
    client.force_login(helper)
    assert client.get(reverse("booth-logout")).status_code == 405
    assert "_auth_user_id" in client.session


def test_logout_post_signs_out_and_returns_to_login(
    client: Client, helper: User
) -> None:
    client.force_login(helper)
    response = client.post(reverse("booth-logout"))
    assert (response.status_code, response["Location"]) == (302, LOGIN)
    assert "_auth_user_id" not in client.session


def test_every_booth_page_has_a_sign_out_button(
    conference_factory: Callable[..., Conference],
    drawing_factory: Callable[..., Drawing],
) -> None:
    conference = conference_factory(slug="signout-conf")
    drawing = drawing_factory(conference)
    owner = Client()
    owner.force_login(
        User.objects.create_user("boss", password="pw", is_superuser=True)
    )
    for url in (
        reverse("booth-home"),
        reverse("booth-conference", args=[conference.slug]),
        reverse("booth-drawing", args=[drawing.pk]),
    ):
        html = owner.get(url).content.decode()
        assert f'action="{reverse("booth-logout")}"' in html, url
        assert "Sign out" in html, url


def test_login_page_has_no_sign_out_button(client: Client) -> None:
    assert "Sign out" not in client.get(LOGIN).content.decode()


# --- per-account limit on failed sign-ins, shared by both login pages ------------

ADMIN_LOGIN = "/admin/login/"
GOOD = "violet-lantern-ripple"


@pytest.fixture
def helper_account(db: None) -> User:
    return User.objects.create_user("helper", "helper@example.com", password=GOOD)


def _post(client: Client, url: str, name: str, password: str, n: int) -> int:
    """Post one sign-in from its own address, so only the account limit can fire."""
    return client.post(
        url,
        {"username": name, "password": password},
        REMOTE_ADDR=f"198.51.100.{n}",
    ).status_code


def _fail(client: Client, url: str, name: str, times: int, start: int = 1) -> None:
    for n in range(start, start + times):
        assert _post(client, url, name, "wrong", n) == 200


def test_eleventh_attempt_on_the_admin_page_is_blocked_after_booth_failures(
    helper_account: User,
) -> None:
    client = Client()
    _fail(client, LOGIN, "helper", 10)
    # Even the right password is refused, and nothing is authenticated.
    assert _post(client, ADMIN_LOGIN, "helper", GOOD, 50) == 429
    assert _post(client, LOGIN, "helper", GOOD, 51) == 429
    assert "_auth_user_id" not in client.session


def test_failures_on_both_pages_share_one_count(helper_account: User) -> None:
    client = Client()
    _fail(client, LOGIN, "helper", 5)
    _fail(client, ADMIN_LOGIN, "helper", 5, start=10)
    assert _post(client, LOGIN, "helper", GOOD, 60) == 429


def test_case_and_surrounding_spaces_share_one_bucket(helper_account: User) -> None:
    client = Client()
    _fail(client, LOGIN, "Helper", 5)
    _fail(client, LOGIN, "  helper  ", 5, start=10)
    assert _post(client, LOGIN, "HELPER", GOOD, 60) == 429


def test_blocked_attempts_do_not_count_further(helper_account: User) -> None:
    client = Client()
    _fail(client, LOGIN, "helper", 10)
    assert {_post(client, LOGIN, "helper", "x", n) for n in range(60, 70)} == {429}
    assert cache.get(login_failure_key("helper")) == 10


def test_successful_sign_ins_do_not_count(helper_account: User) -> None:
    client = Client()
    for n in range(10):
        assert _post(client, LOGIN, "helper", GOOD, n + 1) == 302
        client.post(reverse("booth-logout"))
    assert _post(client, LOGIN, "helper", "wrong", 40) == 200
    assert _post(client, LOGIN, "helper", GOOD, 41) == 302


def test_another_account_is_unaffected(helper_account: User) -> None:
    User.objects.create_user("other", password=GOOD)
    client = Client()
    _fail(client, LOGIN, "helper", 10)
    assert _post(client, LOGIN, "other", GOOD, 70) == 302


def test_a_blocked_account_does_not_end_a_signed_in_session(
    helper_account: User,
) -> None:
    client = Client()
    assert _post(client, LOGIN, "helper", GOOD, 1) == 302
    _fail(Client(), LOGIN, "helper", 10, start=10)
    assert client.get("/booth/").status_code == 200


def test_the_block_uses_the_address_limit_response(helper_account: User) -> None:
    client = Client()
    _fail(client, LOGIN, "helper", 10)
    blocked = client.post(
        LOGIN, {"username": "helper", "password": "x"}, REMOTE_ADDR="198.51.100.80"
    )
    expected = rate_limited(RequestFactory().post(LOGIN), Ratelimited())
    assert blocked.status_code == 429
    assert blocked.content == expected.content


def test_the_typed_name_never_appears_in_the_cache_key() -> None:
    key = login_failure_key("  Secret.Name@Example.com ")
    assert "secret" not in key.lower()
    assert key == login_failure_key("secret.name@example.com")


def test_a_missing_username_has_the_empty_bucket() -> None:
    assert login_failure_key("") == login_failure_key("   ")


def test_unicode_variants_of_a_name_share_one_bucket(helper_account: User) -> None:
    """Django NFKC-normalizes the typed name, so these all sign in as helper."""
    client = Client()
    _fail(client, LOGIN, "ｈelper", 5)  # fullwidth h
    _fail(client, LOGIN, "helper", 5, start=10)
    assert _post(client, LOGIN, "\U0001d421elper", GOOD, 60) == 429  # math bold h


@given(st.text(max_size=30))
@example("ｈelper")
@example("Kelvin")  # Kelvin sign
def test_every_spelling_the_login_form_accepts_has_one_key(name: str) -> None:
    key = login_failure_key(name)
    assert key == login_failure_key(unicodedata.normalize("NFKC", name))
    assert key == login_failure_key(f"  {name.upper()}\t")
    assert key == login_failure_key(name.swapcase())


def test_right_password_on_a_non_staff_account_counts_on_the_admin_page(
    helper_account: User,
) -> None:
    """The admin form refuses non-staff after a correct password; that is a guess."""
    client = Client()
    statuses = {_post(client, ADMIN_LOGIN, "helper", GOOD, n) for n in range(1, 11)}
    assert statuses == {200}
    assert _post(client, ADMIN_LOGIN, "helper", GOOD, 60) == 429


def test_failure_count_outlives_the_cache_default_timeout(
    helper_account: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """incr rewrites the row with a 300 s default; the 15 minutes must hold."""
    client = Client()
    _fail(client, LOGIN, "helper", 10)
    later = timezone.now() + datetime.timedelta(seconds=600)
    monkeypatch.setattr(db_cache, "tz_now", lambda: later)
    assert _post(client, LOGIN, "helper", GOOD, 90) == 429


def test_the_block_lifts_after_fifteen_minutes(
    helper_account: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = Client()
    _fail(client, LOGIN, "helper", 10)
    later = timezone.now() + datetime.timedelta(minutes=16)
    monkeypatch.setattr(db_cache, "tz_now", lambda: later)
    assert _post(client, LOGIN, "helper", GOOD, 91) == 302


def test_a_get_is_never_counted_or_blocked(helper_account: User) -> None:
    client = Client()
    _fail(client, LOGIN, "helper", 10)
    assert client.get(LOGIN).status_code == 200
    assert cache.get(login_failure_key("helper")) == 10


def test_a_counter_that_expires_mid_update_restarts_at_one(db: None) -> None:

    def expired(key: str) -> int:
        cache.delete(key)  # the row lapsed between add and incr
        raise ValueError(key)

    with mock.patch.object(cache, "incr", side_effect=expired):
        _record_failure("helper")
    assert cache.get(login_failure_key("helper")) == 1
