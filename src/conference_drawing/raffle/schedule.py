"""Drawing times and books as printed for attendees (entry page, thanks, flyer).

One place decides which drawings are public, how a time is worded and what the
prize rule says, so the web pages and the printed flyer cannot drift apart.
"""

import datetime

from django.db.models import QuerySet

from .models import Book, Conference, Drawing, PrizeLine
from .timezones import to_local

MUST_BE_PRESENT = "You must be present to win a printed book."


def list_join(parts: list[str]) -> str:
    """Join with commas and a final "and": "a", "a and b", "a, b and c"."""
    if len(parts) <= 1:
        return "".join(parts)
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def format_time(local: datetime.datetime) -> str:
    """Short weekday and 12-hour time without a leading zero: "Tue 2:00 PM"."""
    return local.strftime("%a %I:%M %p").replace(" 0", " ")


def upcoming_drawings(conference: Conference) -> QuerySet[Drawing]:
    """Drawings an attendee can still hope to win in, earliest first."""
    return Drawing.objects.filter(
        conference=conference,
        status__in=[Drawing.Status.SCHEDULED, Drawing.Status.OPEN],
    ).order_by("scheduled_at", "id")


def books_in(drawings: QuerySet[Drawing]) -> list[Book]:
    """Each book in `drawings`' prize lines once, in order of first appearance."""
    book_ids = (
        PrizeLine.objects.filter(drawing__in=drawings)
        .order_by("drawing__scheduled_at", "drawing_id", "position", "id")
        .values_list("book_id", flat=True)
    )
    ordered_ids = list(dict.fromkeys(book_ids))
    books_by_id = Book.objects.in_bulk(ordered_ids)
    return [books_by_id[i] for i in ordered_ids]


def schedule_context(conference: Conference) -> dict[str, object]:
    """Drawing times (in the conference's zone) and books for the entry/thanks pages."""
    drawings = upcoming_drawings(conference)
    times = [
        format_time(to_local(when, conference.time_zone))
        for when in drawings.values_list("scheduled_at", flat=True)
    ]
    return {
        "conference": conference,
        "drawing_times": list_join(times),
        "books": books_in(drawings),
        "must_be_present": MUST_BE_PRESENT,
    }


def days(conference: Conference) -> list[tuple[str, list[str]]]:
    """Upcoming drawing times by local day: [("Tuesday, May 11", ["2:00 PM"])]."""
    grouped: dict[str, list[str]] = {}
    for when in upcoming_drawings(conference).values_list("scheduled_at", flat=True):
        local = to_local(when, conference.time_zone)
        label = local.strftime("%A, %B ") + str(local.day)
        grouped.setdefault(label, []).append(format_time(local).split(" ", 1)[1])
    return list(grouped.items())
