"""Admin sign-in by username or by email address.

The owner types the address they gave at setup, not the username Django made
for them, and was refused. This backend accepts either, without making the
login slower or leakier than Django's own.
"""

from __future__ import annotations

import functools
import hashlib
import hmac
import unicodedata
from collections.abc import Callable
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

from django.conf import settings
from django.contrib.admin.forms import AdminAuthenticationForm
from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.views import LoginView
from django.core.cache import cache
from django.http import HttpRequest
from django.http.response import HttpResponseBase
from django.shortcuts import resolve_url
from django.utils.translation import gettext_lazy as _
from django_ratelimit.exceptions import Ratelimited

if TYPE_CHECKING:  # the stubs' name for the user model instance
    from django.contrib.auth.models import _User


class EmailOrUsernameBackend(ModelBackend):
    """Authenticate by exact username first, then by a unique active email.

    Username wins so an account named like an address (``ops@example.com``)
    is never hijacked by someone else's email. Shared emails are ambiguous, so
    they cannot sign in by email; each user still can by username. Every miss
    runs the hasher once, as ``ModelBackend`` does, so timing does not reveal
    which accounts exist. Typed credentials are never logged.
    """

    def authenticate(
        self,
        request: HttpRequest | None,
        username: str | None = None,
        password: str | None = None,
        **kwargs: Any,
    ) -> _User | None:
        user_model = get_user_model()
        if username is None:
            username = kwargs.get(user_model.USERNAME_FIELD)
        if username is None or password is None:
            return None
        typed = username.strip()
        user: _User | None
        try:
            user = user_model._default_manager.get_by_natural_key(typed)
        except user_model.DoesNotExist:
            user = None
            if "@" in typed:
                matches = list(
                    user_model._default_manager.filter(
                        email__iexact=typed, is_active=True
                    )[:2]
                )
                if len(matches) == 1:
                    user = matches[0]
        if user is None:
            # Same work as a real check, so a miss costs what a hit costs.
            user_model().set_password(password)
            return None
        if user.check_password(password) and self.user_can_authenticate(user):
            return user
        return None


class BoothLoginForm(AuthenticationForm):
    """The booth sign-in form, so helpers who are not staff can sign in too.

    The admin form refuses non-staff accounts; this one is Django's plain
    form, with the label saying that an email address works as well.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fields["username"].label = _("Username or email")


class BoothLoginView(LoginView):
    """The booth sign-in page, safe against a ``next`` that points back at it.

    Django raises (a 500) when a signed-in user opens the login page with
    ``next`` equal to the login page itself. Sending them to the booth home
    instead is the same answer they would get with no ``next`` at all.
    """

    def get_success_url(self) -> str:
        url = super().get_success_url()
        if url == self.request.path:
            return resolve_url(settings.LOGIN_REDIRECT_URL)
        return url


class AdminLoginForm(AdminAuthenticationForm):
    """The admin login form, with a label that says email works too."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fields["username"].label = _("Username or email")


# Failed sign-ins allowed per account name in one window, across both login pages.
ACCOUNT_FAILURE_LIMIT = 10
ACCOUNT_WINDOW_SECONDS = 15 * 60


def login_failure_key(name: str) -> str:
    """Return the cache key for the typed name's failed-sign-in counter.

    The one place a typed name becomes an account: NFKC-normalized (as Django's
    username field does before authenticating, so ``\uff48elper`` is
    ``helper``), trimmed and case-folded. Keyed with the secret and hashed, so
    the name (it may be an email address) is never stored in the cache or
    reachable from a dump of it.
    """
    account = unicodedata.normalize("NFKC", name).strip().casefold()
    digest = hmac.new(
        settings.SECRET_KEY.encode(), f"acct:{account}".encode(), hashlib.sha256
    ).hexdigest()
    return f"loginfail:{digest}"


def _record_failure(name: str) -> None:
    """Count one failed sign-in; the window slides 15 minutes from the latest one.

    The check before authenticating and this increment are separate steps, so
    the limit may overshoot by at most the number of concurrent requests.

    ``cache.incr`` rewrites the row with the backend's default timeout
    (300 s), which would cut the window short, so the full window is restored
    with ``touch`` (the same fix as the entry cap's counter).
    """
    key = login_failure_key(name)
    cache.add(key, 0, ACCOUNT_WINDOW_SECONDS)
    try:
        cache.incr(key)
        cache.touch(key, ACCOUNT_WINDOW_SECONDS)
    except ValueError:  # expired between add and incr; start at this failure
        cache.add(key, 1, ACCOUNT_WINDOW_SECONDS)


def limit_failed_logins(
    view: Callable[..., HttpResponseBase],
) -> Callable[..., HttpResponseBase]:
    """Wrap a login view with the per-account limit on failed sign-ins.

    Why: the per-address limit does nothing against guessing spread over many
    addresses. A POST for an account already at the limit is answered with the
    same 429 as the address limit, before any authentication, so even the
    right password is refused. Only a POST that does not sign in (any answer
    but the redirect) counts, so successes never use up the budget. Wrap both
    login views: they share the count.
    """

    @functools.wraps(view)
    def wrapper(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        if request.method != "POST":
            return view(request, *args, **kwargs)
        name = str(request.POST.get("username", ""))
        if int(cache.get(login_failure_key(name), 0)) >= ACCOUNT_FAILURE_LIMIT:
            raise Ratelimited
        response = view(request, *args, **kwargs)
        if response.status_code != HTTPStatus.FOUND:
            _record_failure(name)
        return response

    return wrapper
