"""Who may do what: the owner, a helper of one conference, or nobody.

Every booth view asks this module instead of checking users itself, so the
rule lives in one place. Owner means an active superuser. A helper is any
other active user listed in ``Conference.helpers`` while the conference is not
wrapped up. Staff status grants nothing. Refusals are always 404, so a
conference or drawing you can't reach looks the same as one that isn't there.
"""

import enum

from django.contrib.auth.base_user import AbstractBaseUser
from django.contrib.auth.models import AnonymousUser
from django.db.models import QuerySet
from django.http import Http404

from .models import Conference, Drawing, Pick

type AnyUser = AbstractBaseUser | AnonymousUser


class Access(enum.Enum):
    """A user's level of access to one conference."""

    OWNER = "owner"
    HELPER = "helper"
    NONE = "none"


def is_owner(user: AnyUser) -> bool:
    """Say whether the user is an active superuser, the only owner role."""
    return bool(user.is_active and getattr(user, "is_superuser", False))


def access_to(user: AnyUser, conference: Conference) -> Access:
    """Return what this user may do with this conference."""
    if not user.is_active:
        return Access.NONE
    if is_owner(user):
        return Access.OWNER
    if (
        conference.wrapped_up_at is None
        and conference.helpers.filter(pk=user.pk).exists()
    ):
        return Access.HELPER
    return Access.NONE


def visible_conferences(user: AnyUser) -> QuerySet[Conference]:
    """Return the conferences this user may see, newest first, then by name."""
    conferences = Conference.objects.order_by("-starts_on", "name")
    if is_owner(user):
        return conferences
    if not user.is_active or user.pk is None:
        return conferences.none()
    return conferences.filter(helpers__pk=user.pk, wrapped_up_at__isnull=True)


def _require(user: AnyUser, conference: Conference, *, owner_only: bool) -> None:
    access = access_to(user, conference)
    if access is Access.NONE or (owner_only and access is not Access.OWNER):
        raise Http404


def conference_or_404(
    user: AnyUser, slug: str, *, owner_only: bool = False
) -> Conference:
    """Fetch a conference by slug, or raise Http404 if the user may not use it."""
    try:
        conference = Conference.objects.get(slug=slug)
    except Conference.DoesNotExist:
        raise Http404 from None
    _require(user, conference, owner_only=owner_only)
    return conference


def drawing_or_404(user: AnyUser, pk: int) -> Drawing:
    """Fetch a drawing by id, or raise Http404 if its conference is off limits."""
    try:
        drawing = Drawing.objects.select_related("conference").get(pk=pk)
    except Drawing.DoesNotExist:
        raise Http404 from None
    _require(user, drawing.conference, owner_only=False)
    return drawing


def pick_or_404(user: AnyUser, pk: int) -> Pick:
    """Fetch a pick by id, or raise Http404 if its conference is off limits."""
    try:
        pick = Pick.objects.select_related("prize_line__drawing__conference").get(pk=pk)
    except Pick.DoesNotExist:
        raise Http404 from None
    _require(user, pick.prize_line.drawing.conference, owner_only=False)
    return pick
