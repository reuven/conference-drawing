"""Plain-function factories for raffle models; exposed as fixtures in conftest.py."""

import datetime
import itertools
from typing import Any

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
    defaults: dict[str, Any] = {
        "name": f"Conference {n}",
        "slug": f"conf-{n}",
        "time_zone": "America/Los_Angeles",
        "booth_location": "Booth 12",
        "starts_on": datetime.date(2027, 5, 10),
        "ends_on": datetime.date(2027, 5, 12),
    }
    return Conference.objects.create(**{**defaults, **kwargs})


def make_drawing(conference: Conference, **kwargs: Any) -> Drawing:
    defaults: dict[str, Any] = {
        "scheduled_at": datetime.datetime(2027, 5, 11, 21, 0, tzinfo=datetime.UTC)
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
    defaults: dict[str, Any] = {"name": f"Entrant {n}", "email": f"e{n}@example.com"}
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
