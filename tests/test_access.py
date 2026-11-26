"""The access rule: owner, helper of a conference, or none."""

import itertools
from typing import Any

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.http import Http404
from django.utils import timezone

from conference_drawing.raffle.access import (
    Access,
    access_to,
    conference_or_404,
    drawing_or_404,
    is_owner,
    pick_or_404,
    visible_conferences,
)
from tests.factories import (
    make_conference,
    make_drawing,
    make_entrant,
    make_pick,
    make_prize_line,
)

pytestmark = pytest.mark.django_db

User = get_user_model()


def _user(name: str, **kwargs: Any) -> Any:
    return User.objects.create_user(
        username=name, email=f"{name}@example.com", password="x", **kwargs
    )


def _expected(active: bool, superuser: bool, listed: bool, wrapped: bool) -> Access:
    if not active:
        return Access.NONE
    if superuser:
        return Access.OWNER
    if listed and not wrapped:
        return Access.HELPER
    return Access.NONE


@pytest.mark.parametrize(
    ("active", "superuser", "staff", "listed", "wrapped"),
    list(itertools.product([True, False], repeat=5)),
)
def test_access_table(
    active: bool, superuser: bool, staff: bool, listed: bool, wrapped: bool
) -> None:
    conference = make_conference(wrapped_up_at=timezone.now() if wrapped else None)
    user = _user("u", is_active=active, is_superuser=superuser, is_staff=staff)
    if listed:
        conference.helpers.add(user)
    assert access_to(user, conference) is _expected(active, superuser, listed, wrapped)
    assert is_owner(user) is (active and superuser)


def test_anonymous_has_no_access() -> None:
    conference = make_conference()
    assert access_to(AnonymousUser(), conference) is Access.NONE
    assert is_owner(AnonymousUser()) is False
    assert not visible_conferences(AnonymousUser()).exists()


def test_visible_conferences_per_role() -> None:
    a = make_conference(slug="a", name="A")
    b = make_conference(slug="b", name="B")
    w = make_conference(slug="w", name="W", wrapped_up_at=timezone.now())
    owner = _user("owner", is_superuser=True)
    helper = _user("helper")
    other = _user("other")
    a.helpers.add(helper)
    w.helpers.add(helper)
    assert set(visible_conferences(owner)) == {a, b, w}
    assert list(visible_conferences(helper)) == [a]
    assert not visible_conferences(other).exists()
    inactive = _user("gone", is_active=False)
    a.helpers.add(inactive)
    assert not visible_conferences(inactive).exists()


def test_visible_conferences_order_is_newest_first_then_name() -> None:
    import datetime

    # The oldest conference sorts first by name, so ordering by name alone fails.
    old = make_conference(name="A", starts_on=datetime.date(2026, 1, 1))
    new_b = make_conference(name="C", starts_on=datetime.date(2027, 1, 1))
    new_a = make_conference(name="B", starts_on=datetime.date(2027, 1, 1))
    owner = _user("owner", is_superuser=True)
    helper = _user("helper")
    for c in (old, new_b, new_a):
        c.helpers.add(helper)
    expected = [new_a, new_b, old]
    assert list(visible_conferences(owner)) == expected
    assert list(visible_conferences(helper)) == expected


def test_lookups_404_for_none_and_for_owner_only() -> None:
    a = make_conference(slug="a")
    b = make_conference(slug="b")
    helper = _user("helper")
    owner = _user("owner", is_superuser=True)
    a.helpers.add(helper)
    drawing_a, drawing_b = make_drawing(a), make_drawing(b)
    pick_a = make_pick(make_prize_line(drawing_a), make_entrant(a))
    pick_b = make_pick(make_prize_line(drawing_b), make_entrant(b))

    assert conference_or_404(helper, "a") == a
    assert conference_or_404(owner, "a", owner_only=True) == a
    with pytest.raises(Http404):
        conference_or_404(helper, "b")
    with pytest.raises(Http404):
        conference_or_404(helper, "a", owner_only=True)
    with pytest.raises(Http404):
        conference_or_404(owner, "missing")
    with pytest.raises(Http404):
        conference_or_404(AnonymousUser(), "a")

    assert drawing_or_404(helper, drawing_a.pk) == drawing_a
    with pytest.raises(Http404):
        drawing_or_404(helper, drawing_b.pk)
    with pytest.raises(Http404):
        drawing_or_404(helper, 0)

    assert pick_or_404(helper, pick_a.pk) == pick_a
    with pytest.raises(Http404):
        pick_or_404(helper, pick_b.pk)
    with pytest.raises(Http404):
        pick_or_404(helper, 0)


def test_a_user_without_a_superuser_flag_is_not_the_owner() -> None:
    """is_owner defaults to False, never True, for a user-like object lacking it."""
    bare: Any = type("Bare", (), {"is_active": True})()
    assert is_owner(bare) is False


def test_lookups_load_the_conference_with_the_drawing_or_pick(
    django_assert_num_queries: Any,
) -> None:
    """The conference comes along in one query; reading it later costs none."""
    owner = _user("owner", is_superuser=True)
    conference = make_conference()
    drawing = make_drawing(conference)
    pick = make_pick(make_prize_line(drawing), make_entrant(conference))
    with django_assert_num_queries(1):
        found = drawing_or_404(owner, drawing.pk)
        assert found.conference.pk == conference.pk
    with django_assert_num_queries(1):
        found_pick = pick_or_404(owner, pick.pk)
        assert found_pick.prize_line.drawing.conference.pk == conference.pk
