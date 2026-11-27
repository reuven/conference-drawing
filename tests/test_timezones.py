import datetime
import zoneinfo
from zoneinfo import ZoneInfo

from hypothesis import example, given
from hypothesis import strategies as st

from conference_drawing.raffle.timezones import (
    REGIONS,
    group_time_zones,
    time_zone_choices,
    to_local,
    to_utc,
)

UTC = datetime.UTC
LA = "America/Los_Angeles"


def test_scheduled_local_time_across_dst() -> None:
    # US clocks spring forward on 2027-03-14; 14:00 local is already PDT (UTC-7).
    assert to_utc(datetime.datetime(2027, 3, 14, 14, 0), LA) == datetime.datetime(
        2027, 3, 14, 21, 0, tzinfo=UTC
    )


def test_day_before_dst_uses_standard_offset() -> None:
    assert to_utc(datetime.datetime(2027, 3, 13, 14, 0), LA) == datetime.datetime(
        2027, 3, 13, 22, 0, tzinfo=UTC
    )


def test_to_local_known_answer() -> None:
    local = to_local(datetime.datetime(2027, 3, 14, 21, 0, tzinfo=UTC), LA)
    assert local.replace(tzinfo=None) == datetime.datetime(2027, 3, 14, 14, 0)
    assert local.tzinfo == ZoneInfo(LA)


@given(
    st.datetimes(
        min_value=datetime.datetime(2000, 1, 1), max_value=datetime.datetime(2100, 1, 1)
    ),
    st.sampled_from([LA, "Europe/Berlin", "Asia/Kolkata", "UTC"]),
)
# Los Angeles falls back at 09:00 UTC on 2000-10-29: both sides of the repeated
# 1 am hour. Python's inter-zone `==` is False for a time inside a fold (PEP 495).
@example(datetime.datetime(2000, 10, 29, 8, 0), LA)
@example(datetime.datetime(2000, 10, 29, 9, 0), LA)
def test_utc_instant_survives_local_round_trip(
    naive: datetime.datetime, tz: str
) -> None:
    utc = naive.replace(tzinfo=UTC)
    # to_local never changes the instant, and to_utc always returns UTC. Compare
    # instants by converting back to UTC: `==` across zones is deliberately False
    # for a time inside a DST fold, so it cannot state this invariant.
    local = to_local(utc, tz)
    assert local.astimezone(UTC) == utc
    assert local.timestamp() == utc.timestamp()
    assert to_utc(naive, tz).utcoffset() == datetime.timedelta(0)


# --- the time zone dropdown ---------------------------------------------------

ALL_ZONES = sorted(zoneinfo.available_timezones())


Choices = dict[str, str | dict[str, str]]


def _flat(choices: Choices) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in choices.items():
        if isinstance(value, dict):
            out.update(value)
        else:
            out[key] = value
    return out


def _group(choices: Choices, region: str) -> dict[str, str]:
    group = choices[region]
    assert isinstance(group, dict)
    return group


def test_choices_label_zones_by_city_under_their_region() -> None:
    choices = time_zone_choices()
    assert _group(choices, "Asia")["Asia/Jerusalem"] == "Jerusalem"
    assert _group(choices, "America")["America/Los_Angeles"] == "Los Angeles"
    assert (
        _group(choices, "America")["America/Argentina/Buenos_Aires"]
        == "Argentina / Buenos Aires"
    )
    assert list(choices)[0] == "UTC"
    assert choices["UTC"] == "UTC"


def test_choices_leave_out_legacy_aliases() -> None:
    flat = _flat(time_zone_choices())
    for alias in ("US/Eastern", "Etc/GMT+3", "EST5EDT", "Israel", "GMT", "Zulu"):
        assert alias not in flat


def test_choices_are_sorted_for_typing_to_jump() -> None:
    choices = time_zone_choices()
    groups = [k for k in choices if k != "UTC"]
    assert groups == sorted(groups)
    for group in groups:
        labels = list(_group(choices, group).values())
        assert labels == sorted(labels, key=str.casefold)


@given(
    st.lists(st.one_of(st.sampled_from(ALL_ZONES), st.text(max_size=30)), max_size=40)
)
def test_grouping_keeps_exactly_the_regional_zones(zones: list[str]) -> None:
    flat = _flat(group_time_zones(zones))
    expected = {
        z for z in zones if z == "UTC" or z.split("/", 1)[0] in REGIONS and "/" in z
    }
    assert set(flat) == expected
    assert all(label.strip() for label in flat.values())


def test_grouping_reads_a_generator_once() -> None:
    zones = ["UTC", "Asia/Jerusalem"]
    assert group_time_zones(z for z in zones) == {
        "UTC": "UTC",
        "Asia": {"Asia/Jerusalem": "Jerusalem"},
    }


def test_to_utc_labels_the_result_utc_not_the_machine_zone() -> None:
    # The same instant in the server's local zone compares equal, so pin the
    # label itself: stored and logged times must read as UTC.
    result = to_utc(datetime.datetime(2027, 5, 11, 14, 0), LA)
    assert result.tzinfo is UTC
