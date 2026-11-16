from datetime import UTC, date, datetime, timedelta

import pytest

from deskbrief import calendars as c
from deskbrief.timeutil import et_to_utc


def test_thanksgiving_2026_closed_and_black_friday_early_close():
    assert not c.is_business_day("XNYS", date(2026, 11, 26))
    assert c.session_bounds_utc(date(2026, 11, 26)) is None
    assert c.is_business_day("XNYS", date(2026, 11, 27))
    assert c.is_early_close(date(2026, 11, 27))
    open_, close = c.session_bounds_utc(date(2026, 11, 27)) or (None, None)
    assert open_ == et_to_utc(date(2026, 11, 27), "09:30")
    assert close == et_to_utc(date(2026, 11, 27), "13:00")


def test_regular_day_is_not_early_close():
    d = date(2026, 11, 25)
    assert not c.is_early_close(d)
    assert c.session_bounds_utc(d) == (et_to_utc(d, "09:30"), et_to_utc(d, "16:00"))


def test_good_friday_target_holiday_and_easter_monday():
    assert not c.is_business_day("TARGET", date(2026, 4, 3))
    assert not c.is_business_day("TARGET", date(2026, 4, 6))
    assert c.is_business_day("TARGET", date(2026, 4, 7))
    # Easter 2025 = Apr 20; 2024 = Mar 31 (Good Friday is in March)
    assert not c.is_business_day("TARGET", date(2025, 4, 18))
    assert not c.is_business_day("TARGET", date(2025, 4, 21))
    assert not c.is_business_day("TARGET", date(2024, 3, 29))
    assert not c.is_business_day("TARGET", date(2024, 4, 1))


@pytest.mark.parametrize(
    "d",
    [date(2026, 1, 1), date(2026, 5, 1), date(2026, 12, 25), date(2026, 12, 26)],
)
def test_target_fixed_holidays(d):
    assert not c.is_business_day("TARGET", d)


def test_target_open_when_us_closed():
    # Thanksgiving, Independence Day observed, Juneteenth: TARGET open
    assert c.is_business_day("TARGET", date(2026, 11, 26))
    assert c.is_business_day("TARGET", date(2026, 6, 19))
    # May 1 2026 is a Friday: TARGET closed, XNYS open
    assert c.is_business_day("XNYS", date(2026, 5, 1))


def test_weekends_closed_everywhere():
    for cal in ("XNYS", "SIFMA_US", "TARGET"):
        assert not c.is_business_day(cal, date(2026, 11, 14))  # Saturday
        assert not c.is_business_day(cal, date(2026, 11, 15))  # Sunday


def test_juneteenth_xnys_closed():
    assert not c.is_business_day("XNYS", date(2026, 6, 19))
    assert not c.is_business_day("SIFMA_US", date(2026, 6, 19))


def test_good_friday_xnys_closed_but_sifma_2026_early_close():
    assert not c.is_business_day("XNYS", date(2026, 4, 3))
    # SIFMA recommended a noon close on 2026-04-03 (jobs report day): open, not a holiday
    assert c.is_business_day("SIFMA_US", date(2026, 4, 3))


def test_sifma_differs_from_xnys_on_columbus_day():
    assert c.is_business_day("XNYS", date(2026, 10, 12))
    assert not c.is_business_day("SIFMA_US", date(2026, 10, 12))


def test_dst_boundary_sessions():
    # US spring-forward Sun 2026-03-08: Fri open at 14:30Z (EST), Mon open at 13:30Z (EDT)
    fri = c.session_bounds_utc(date(2026, 3, 6))
    mon = c.session_bounds_utc(date(2026, 3, 9))
    assert fri == (
        datetime(2026, 3, 6, 14, 30, tzinfo=UTC),
        datetime(2026, 3, 6, 21, 0, tzinfo=UTC),
    )
    assert mon == (
        datetime(2026, 3, 9, 13, 30, tzinfo=UTC),
        datetime(2026, 3, 9, 20, 0, tzinfo=UTC),
    )
    # fall-back Sun 2026-11-01
    fri = c.session_bounds_utc(date(2026, 10, 30))
    mon = c.session_bounds_utc(date(2026, 11, 2))
    assert fri == (
        datetime(2026, 10, 30, 13, 30, tzinfo=UTC),
        datetime(2026, 10, 30, 20, 0, tzinfo=UTC),
    )
    assert mon == (
        datetime(2026, 11, 2, 14, 30, tzinfo=UTC),
        datetime(2026, 11, 2, 21, 0, tzinfo=UTC),
    )


def test_session_bounds_are_utc_aware():
    open_, close = c.session_bounds_utc(date(2026, 7, 1)) or (None, None)
    assert open_ is not None
    assert close is not None
    assert open_.utcoffset() == timedelta(0)
    assert close.utcoffset() == timedelta(0)


def test_session_bounds_none_on_weekend():
    assert c.session_bounds_utc(date(2026, 11, 14)) is None


def test_sifma_early_closes_exposed_by_library():
    # Day after Thanksgiving is a SIFMA early close too; plain day is not
    assert c.is_early_close(date(2026, 11, 27), cal="SIFMA_US")
    assert not c.is_early_close(date(2026, 11, 25), cal="SIFMA_US")
    assert not c.is_early_close(date(2026, 11, 26), cal="SIFMA_US")  # closed, not early


def test_target_has_no_early_closes():
    assert not c.is_early_close(date(2026, 12, 24), cal="TARGET")


def test_prev_business_day_strictly_before():
    assert c.prev_business_day("XNYS", date(2026, 11, 27)) == date(2026, 11, 25)
    assert c.prev_business_day("XNYS", date(2026, 11, 25)) == date(2026, 11, 24)
    assert c.prev_business_day("XNYS", date(2026, 11, 15)) == date(2026, 11, 13)  # Sunday
    assert c.prev_business_day("XNYS", date(2026, 11, 16)) == date(2026, 11, 13)  # Monday


def test_next_business_day_strictly_after():
    assert c.next_business_day("XNYS", date(2026, 11, 25)) == date(2026, 11, 27)
    assert c.next_business_day("XNYS", date(2026, 11, 13)) == date(2026, 11, 16)
    assert c.next_business_day("TARGET", date(2026, 4, 2)) == date(2026, 4, 7)


def test_year_boundaries():
    # Jan 1 2027 is a Friday (holiday); Dec 31 2026 Thursday open
    assert c.prev_business_day("XNYS", date(2027, 1, 4)) == date(2026, 12, 31)
    assert c.next_business_day("XNYS", date(2026, 12, 31)) == date(2027, 1, 4)
    # 2022-12-31 Sat; NYSE observes New Year 2022 on... Jan 1 2022 was Saturday: no Friday close
    assert c.is_business_day("XNYS", date(2021, 12, 31))
    # TARGET: Dec 24 2026 open, Dec 25/26 closed, Dec 28 open
    assert c.next_business_day("TARGET", date(2026, 12, 24)) == date(2026, 12, 28)
    assert c.prev_business_day("TARGET", date(2027, 1, 4)) == date(2026, 12, 31)


def test_business_days_inclusive_range():
    got = c.business_days("XNYS", date(2026, 11, 23), date(2026, 11, 30))
    assert got == [
        date(2026, 11, 23),
        date(2026, 11, 24),
        date(2026, 11, 25),
        date(2026, 11, 27),
        date(2026, 11, 30),
    ]


def test_business_days_across_years_and_empty():
    got = c.business_days("TARGET", date(2026, 12, 23), date(2027, 1, 5))
    assert got == [
        date(2026, 12, 23),
        date(2026, 12, 24),
        date(2026, 12, 28),
        date(2026, 12, 29),
        date(2026, 12, 30),
        date(2026, 12, 31),
        date(2027, 1, 4),
        date(2027, 1, 5),
    ]
    assert c.business_days("XNYS", date(2026, 11, 14), date(2026, 11, 15)) == []
    assert c.business_days("XNYS", date(2026, 11, 20), date(2026, 11, 19)) == []


@pytest.mark.parametrize("fn", [c.is_business_day, c.prev_business_day, c.next_business_day])
def test_datetime_rejected_where_date_expected(fn):
    with pytest.raises(TypeError):
        fn("XNYS", datetime(2026, 11, 25, 12, tzinfo=UTC))


def test_datetime_rejected_elsewhere():
    with pytest.raises(TypeError):
        c.session_bounds_utc(datetime(2026, 11, 25, tzinfo=UTC))
    with pytest.raises(TypeError):
        c.is_early_close(datetime(2026, 11, 25, tzinfo=UTC))
    with pytest.raises(TypeError):
        c.business_days("XNYS", datetime(2026, 11, 25, tzinfo=UTC), date(2026, 11, 30))


def test_unknown_calendar_rejected():
    with pytest.raises(ValueError, match="unknown calendar"):
        c.is_business_day("LSE", date(2026, 11, 25))  # type: ignore[arg-type]
