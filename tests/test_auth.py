"""Admin login by username or email, with one hasher run on every miss."""

from io import StringIO
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from django.contrib.auth import authenticate, get_user_model
from django.contrib.auth.models import AbstractBaseUser
from django.core.management import CommandError, call_command
from django.test import Client, RequestFactory
from django.urls import reverse

from conference_drawing.auth import AdminLoginForm, EmailOrUsernameBackend

User = get_user_model()


@pytest.fixture
def boss(db: None) -> AbstractBaseUser:
    return User.objects.create_user("boss", "boss@example.com", "pw")


def test_username_login_works(boss: AbstractBaseUser) -> None:
    assert authenticate(username="boss", password="pw") == boss


def test_email_login_ignores_case_and_surrounding_spaces(
    boss: AbstractBaseUser,
) -> None:
    assert authenticate(username="  Boss@Example.COM ", password="pw") == boss


def test_username_containing_at_sign_wins_over_email_lookup(db: None) -> None:
    ops = User.objects.create_user("ops@example.com", "other@example.com", "pw1")
    User.objects.create_user("someone", "ops@example.com", "pw2")
    assert authenticate(username="ops@example.com", password="pw1") == ops
    assert authenticate(username="ops@example.com", password="pw2") is None


def test_shared_email_cannot_log_in_by_email_but_usernames_work(db: None) -> None:
    a = User.objects.create_user("a", "same@example.com", "pw")
    b = User.objects.create_user("b", "same@example.com", "pw")
    assert authenticate(username="same@example.com", password="pw") is None
    assert authenticate(username="a", password="pw") == a
    assert authenticate(username="b", password="pw") == b


def test_inactive_user_fails_by_username_and_email(db: None) -> None:
    User.objects.create_user("gone", "gone@example.com", "pw", is_active=False)
    assert authenticate(username="gone", password="pw") is None
    assert authenticate(username="gone@example.com", password="pw") is None


def test_inactive_duplicate_email_does_not_block_the_active_user(db: None) -> None:
    User.objects.create_user("old", "x@example.com", "pw", is_active=False)
    new = User.objects.create_user("new", "x@example.com", "pw")
    assert authenticate(username="x@example.com", password="pw") == new


def test_wrong_password_fails(boss: AbstractBaseUser) -> None:
    assert authenticate(username="boss", password="nope") is None
    assert authenticate(username="boss@example.com", password="nope") is None


def test_unknown_email_still_runs_the_hasher(db: None) -> None:
    with mock.patch.object(User, "set_password") as spy:
        assert authenticate(username="nobody@example.com", password="pw") is None
    spy.assert_called_once_with("pw")


def test_hasher_runs_once_for_duplicate_emails(db: None) -> None:
    User.objects.create_user("a", "same@example.com", "pw")
    User.objects.create_user("b", "same@example.com", "pw")
    with mock.patch.object(User, "set_password") as spy:
        assert authenticate(username="same@example.com", password="pw") is None
    spy.assert_called_once_with("pw")


def test_missing_credentials_return_none_without_hashing(db: None) -> None:
    backend = EmailOrUsernameBackend()
    with mock.patch.object(User, "set_password") as spy:
        assert backend.authenticate(None, username=None, password="pw") is None
        assert backend.authenticate(None, username="boss", password=None) is None
    spy.assert_not_called()


def test_username_may_arrive_under_the_user_model_field_name(
    boss: AbstractBaseUser,
) -> None:
    backend = EmailOrUsernameBackend()
    assert (
        backend.authenticate(None, password="pw", **{User.USERNAME_FIELD: "boss"})
        == boss
    )


def test_admin_login_page_labels_the_field_username_or_email(db: None) -> None:
    html = Client().get(reverse("admin:login")).content.decode()
    assert "Username or email" in html


def test_admin_login_post_with_email_redirects_to_admin(db: None) -> None:
    User.objects.create_superuser("boss", "boss@example.com", "pw")
    data: dict[str, Any] = {
        "username": "boss@example.com",
        "password": "pw",
        "next": "/admin/",
    }
    resp = Client().post(reverse("admin:login"), data, REMOTE_ADDR="203.0.113.77")
    assert (resp.status_code, resp["Location"]) == (302, "/admin/")


def test_unknown_username_still_runs_the_hasher(db: None) -> None:
    with mock.patch.object(User, "set_password") as spy:
        assert authenticate(username="nobody", password="pw") is None
    spy.assert_called_once_with("pw")


@pytest.mark.parametrize(
    ("typed", "password"),
    [
        ("nobody", "pw"),  # unknown username
        ("nobody@example.com", "pw"),  # unknown email
        ("same@example.com", "pw"),  # duplicate email
        ("gone@example.com", "pw"),  # inactive, by email
        ("gone", "pw"),  # inactive, by username
        ("boss", "wrong"),  # wrong password
    ],
)
def test_every_failure_path_does_exactly_one_hash(
    db: None, typed: str, password: str
) -> None:
    """One hasher run per failed login, so no path is cheaper than another."""
    User.objects.create_user("boss", "boss@example.com", "pw")
    User.objects.create_user("a", "same@example.com", "pw")
    User.objects.create_user("b", "same@example.com", "pw")
    User.objects.create_user("gone", "gone@example.com", "pw", is_active=False)
    with (
        mock.patch.object(
            User, "set_password", autospec=True, side_effect=User.set_password
        ) as setter,
        mock.patch.object(
            User, "check_password", autospec=True, side_effect=User.check_password
        ) as checker,
    ):
        assert authenticate(username=typed, password=password) is None
    assert setter.call_count + checker.call_count == 1


def test_unmatched_email_does_not_log_in_the_only_active_user(
    boss: AbstractBaseUser,
) -> None:
    # The email filter is what ties the typed address to an account: with one
    # active user in the table, a lookup that ignored it would let anyone in.
    assert authenticate(username="stranger@example.com", password="pw") is None


def test_admin_login_form_keeps_the_positional_request() -> None:
    request = RequestFactory().get("/admin/login/")
    form = AdminLoginForm(request)
    assert form.request is request


# --- password rules ------------------------------------------------------------


def _add_user(client: Client, password: str) -> Any:
    client.force_login(
        User.objects.create_superuser("root", "root@example.com", "x-not-validated")
    )
    return client.post(
        reverse("admin:auth_user_add"),
        {"username": "newbie", "password1": password, "password2": password},
    )


def test_admin_add_user_refuses_a_short_password(db: None, client: Client) -> None:
    response = _add_user(client, "short-pass")
    assert response.status_code == 200
    assert "at least 12 characters" in response.content.decode()
    assert not User.objects.filter(username="newbie").exists()


def test_admin_add_user_refuses_a_common_numeric_password(
    db: None, client: Client
) -> None:
    html = _add_user(client, "123456789012").content.decode()
    assert "entirely numeric" in html
    assert not User.objects.filter(username="newbie").exists()


def test_admin_add_user_accepts_a_long_uncommon_password(
    db: None, client: Client
) -> None:
    assert _add_user(client, "violet-lantern-ripple").status_code == 302
    assert User.objects.filter(username="newbie").exists()


def test_changepassword_refuses_a_short_password(
    boss: AbstractBaseUser, monkeypatch: pytest.MonkeyPatch
) -> None:
    """owner_password.sh runs changepassword, which applies the validators."""
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "short-pass")
    with pytest.raises(CommandError, match="Aborting password change"):
        call_command("changepassword", "boss", stderr=StringIO(), stdout=StringIO())


def test_owner_password_script_runs_changepassword_which_validates() -> None:
    """The owner-password path must stay a validated one (see changepassword)."""
    script = Path(__file__).parent.parent / "deploy" / "remote" / "owner_password.sh"
    assert "run_app changepassword" in script.read_text()
