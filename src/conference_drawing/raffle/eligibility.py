"""Who may be drawn: the fairness rules every pick relies on."""

from django.db.models import QuerySet

from .models import Conference, Drawing, Entrant, Pick

WINNING_STATUSES = (Pick.Status.CLAIMED, Pick.Status.DIGITAL)


def winning_picks(conference: Conference) -> QuerySet[Pick]:
    """Return the picks that make someone a winner in `conference`.

    The one definition of "winner" (a claimed or digital pick; pending, not-here
    and void picks don't count), shared by the pool, the CSV export and the
    wrap-up tags so the three cannot disagree.
    """
    return Pick.objects.filter(
        prize_line__drawing__conference=conference, status__in=WINNING_STATUSES
    )


def eligible_entrant_ids(drawing: Drawing) -> list[int]:
    """Return the sorted ids of entrants who may be picked in `drawing`.

    One place defines the pool so the drawing service cannot drift from the
    rules. An entrant is out if they entered at or after the cutoff, are on the
    conference's exclusion list, already won (claimed or digital) anywhere in
    this conference, or already hold a live pick here (pending, claimed,
    not_here, digital). A voided pick frees the entrant again.

    Raises ValueError if the drawing has no cutoff yet (it has not been opened).
    """
    if drawing.cutoff_at is None:
        raise ValueError("Drawing has no cutoff_at; open it before computing a pool.")
    conference = drawing.conference
    won = winning_picks(conference).values("entrant_id")
    picked_here = Pick.objects.filter(
        prize_line__drawing=drawing,
        status__in=[
            Pick.Status.PENDING,
            Pick.Status.CLAIMED,
            Pick.Status.NOT_HERE,
            Pick.Status.DIGITAL,
        ],
    ).values("entrant_id")
    return list(
        Entrant.objects.filter(conference=conference, created_at__lt=drawing.cutoff_at)
        .exclude(email__in=conference.excluded_emails)
        .exclude(id__in=won)
        .exclude(id__in=picked_here)
        .order_by("id")
        .values_list("id", flat=True)
    )
