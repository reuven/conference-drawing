"""Conversions between a conference's local wall-clock time and stored UTC."""

import datetime
from zoneinfo import ZoneInfo


def to_utc(local: datetime.datetime, tz: str) -> datetime.datetime:
    """Interpret a naive wall-clock `local` time in zone `tz` and return it in UTC.

    Organizers type drawing times as "2 pm at the venue"; storing UTC while
    resolving the offset on the date in question keeps a DST-change day correct.
    """
    return local.replace(tzinfo=ZoneInfo(tz)).astimezone(datetime.UTC)


def to_local(utc: datetime.datetime, tz: str) -> datetime.datetime:
    """Convert an aware UTC instant to zone `tz` for display."""
    return utc.astimezone(ZoneInfo(tz))
