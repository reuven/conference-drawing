"""The booth sign-in page: helpers sign in without the admin, and sign out."""

from collections.abc import Callable
from typing import Any

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse

from conference_drawing.raffle.models import Conference, Drawing

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


def test_next_to_another_site_is_ignored(client: Client, helper: User) -> None:
    response = client.post(
        LOGIN + "?next=https://evil.example/",
        {"username": "helper-one", "password": "pw-12345"},
    )
    assert response["Location"] == "/booth/"


def test_signed_in_user_is_sent_to_the_booth(client: Client, helper: User) -> None:
    client.force_login(helper)
    response = client.get(LOGIN)
    assert (response.status_code, response["Location"]) == (302, "/booth/")


@pytest.mark.usefixtures("frozen_ratelimit_clock")
def test_login_posts_are_rate_limited_with_their_own_counter(db: None) -> None:
    client = Client()
    guess = {"username": "nobody", "password": "wrong"}
    who: dict[str, Any] = {"REMOTE_ADDR": "203.0.113.50"}
    assert {client.post(LOGIN, guess, **who).status_code for _ in range(10)} == {200}
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
