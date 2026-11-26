"""Conversions between a conference's local wall-clock time and stored UTC."""

import datetime
import zoneinfo
from collections.abc import Iterable
from zoneinfo import ZoneInfo

# The IANA areas a conference can be in. Names outside them ("US/Eastern",
# "Etc/GMT+3", "EST5EDT", "Israel") are legacy aliases that only clutter the
# dropdown; each one duplicates a city zone listed here.
REGIONS = frozenset(
    {
        "Africa",
        "America",
        "Antarctica",
        "Arctic",
        "Asia",
        "Atlantic",
        "Australia",
        "Europe",
        "Indian",
        "Pacific",
    }
)


def to_utc(local: datetime.datetime, tz: str) -> datetime.datetime:
    """Interpret a naive wall-clock `local` time in zone `tz` and return it in UTC.

    Organizers type drawing times as "2 pm at the venue"; storing UTC while
    resolving the offset on the date in question keeps a DST-change day correct.
    """
    return local.replace(tzinfo=ZoneInfo(tz)).astimezone(datetime.UTC)


def to_local(utc: datetime.datetime, tz: str) -> datetime.datetime:
    """Convert an aware UTC instant to zone `tz` for display."""
    return utc.astimezone(ZoneInfo(tz))


def group_time_zones(zones: Iterable[str]) -> dict[str, str | dict[str, str]]:
    """Group IANA zone names by region and label each by its city.

    The admin's dropdown shows "Jerusalem" under "Asia", so an organizer can
    type "Jer" to jump to it without knowing the name "Asia/Jerusalem".
    "UTC" comes first; groups, and the labels within each, are sorted.
    """
    names = set(zones)  # a generator could only be read once
    groups: dict[str, dict[str, str]] = {}
    for zone in names:
        region, slash, place = zone.partition("/")
        if slash and region in REGIONS and place:
            groups.setdefault(region, {})[zone] = place.replace("_", " ").replace(
                "/", " / "
            )
    choices: dict[str, str | dict[str, str]] = {}
    if "UTC" in names:
        choices["UTC"] = "UTC"
    for region in sorted(groups):
        choices[region] = dict(
            sorted(groups[region].items(), key=lambda item: item[1].casefold())
        )
    return choices


def time_zone_choices() -> dict[str, str | dict[str, str]]:
    """The conference time zone dropdown, built from the zones installed here.

    A callable, so the list follows the server's tz database and a tzdata
    update never needs a migration.
    """
    return group_time_zones(zoneinfo.available_timezones())
