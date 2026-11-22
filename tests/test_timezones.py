import datetime
from zoneinfo import ZoneInfo

from hypothesis import given
from hypothesis import strategies as st

from conference_drawing.raffle.timezones import to_local, to_utc

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
def test_utc_instant_survives_local_round_trip(
    naive: datetime.datetime, tz: str
) -> None:
    utc = naive.replace(tzinfo=UTC)
    # to_local never changes the instant, and to_utc always returns UTC.
    assert to_local(utc, tz) == utc
    assert to_utc(naive, tz).utcoffset() == datetime.timedelta(0)
