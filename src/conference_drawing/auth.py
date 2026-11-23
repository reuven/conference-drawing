"""Admin sign-in by username or by email address.

The owner types the address they gave at setup, not the username Django made
for them, and was refused. This backend accepts either, without making the
login slower or leakier than Django's own.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.contrib.admin.forms import AdminAuthenticationForm
from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend
from django.http import HttpRequest
from django.utils.translation import gettext_lazy as _

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


class AdminLoginForm(AdminAuthenticationForm):
    """The admin login form, with a label that says email works too."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fields["username"].label = _("Username or email")
