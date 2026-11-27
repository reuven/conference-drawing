"""Plain-function factories for raffle models; exposed as fixtures in conftest.py."""

import datetime
import itertools
from typing import Any
from zoneinfo import ZoneInfo

from django.utils import timezone

from conference_drawing.raffle.models import (
    Book,
    Conference,
    Drawing,
    Entrant,
    Pick,
    PrizeLine,
    Question,
)

_counter = itertools.count(1)


def make_book(**kwargs: Any) -> Book:
    n = next(_counter)
    return Book.objects.create(**{"title": f"Book {n}", **kwargs})


def make_conference(**kwargs: Any) -> Conference:
    n = next(_counter)
    zone = kwargs.get("time_zone", "America/Los_Angeles")
    # The entry form only accepts entries during the conference, so by default
    # "today" in the conference's zone is the middle day of a three-day event.
    today = timezone.localdate(timezone=ZoneInfo(zone))
    defaults: dict[str, Any] = {
        "name": f"Conference {n}",
        "slug": f"conf-{n}",
        "time_zone": zone,
        "booth_location": "Booth 12",
        "starts_on": today - datetime.timedelta(days=1),
        "ends_on": today + datetime.timedelta(days=1),
    }
    return Conference.objects.create(**{**defaults, **kwargs})


def make_drawing(conference: Conference, **kwargs: Any) -> Drawing:
    defaults: dict[str, Any] = {
        # Now or earlier, so helpers may start it; tests that need a future
        # drawing (early-start rules) set scheduled_at explicitly.
        "scheduled_at": timezone.now() - datetime.timedelta(hours=1)
    }
    return Drawing.objects.create(conference=conference, **{**defaults, **kwargs})


def make_prize_line(drawing: Drawing, **kwargs: Any) -> PrizeLine:
    defaults: dict[str, Any] = {
        "book": make_book(),
        "kind": PrizeLine.Kind.PHYSICAL,
        "quantity": 1,
    }
    return PrizeLine.objects.create(drawing=drawing, **{**defaults, **kwargs})


def make_entrant(conference: Conference, **kwargs: Any) -> Entrant:
    n = next(_counter)
    defaults: dict[str, Any] = {
        "name": f"Entrant {n}",
        "email": f"e{n}@example.com",
        # Before the default drawing's scheduled time (an hour ago), so a
        # drawing started with defaults still finds them eligible.
        "created_at": timezone.now() - datetime.timedelta(hours=2),
    }
    return Entrant.objects.create(conference=conference, **{**defaults, **kwargs})


def make_pick(prize_line: PrizeLine, entrant: Entrant, **kwargs: Any) -> Pick:
    defaults: dict[str, Any] = {"slot": 1, "pool_size": 1}
    return Pick.objects.create(
        prize_line=prize_line, entrant=entrant, **{**defaults, **kwargs}
    )


def make_questions(conference: Conference) -> list[Question]:
    """Create three neutral optional questions: two text, one with two choices.

    Tests that need entry-form questions use these in place of any shipped
    defaults, so they keep working whatever a new conference starts with.
    """
    return Question.objects.bulk_create(
        [
            Question(
                conference=conference,
                key="want_to_learn",
                prompt="What do you most want to learn?",
                kind=Question.Kind.TEXT,
                position=0,
            ),
            Question(
                conference=conference,
                key="coding_style",
                prompt="How do you code these days?",
                kind=Question.Kind.CHOICE,
                choices=["Mostly by hand", "With some help"],
                position=1,
            ),
            Question(
                conference=conference,
                key="anything_else",
                prompt="Anything else to tell me?",
                kind=Question.Kind.TEXT,
                position=2,
            ),
        ]
    )
