from datetime import UTC, date, datetime, time

import pytest

from deskbrief.ids import new_id
from deskbrief.timeutil import (
    CET,
    ET,
    ensure_utc,
    et_date,
    et_to_utc,
    local_to_utc,
    parse_hhmm,
    to_et,
    utc_now,
)


def test_utc_now_is_aware() -> None:
    assert utc_now().tzinfo is not None


def test_ensure_utc_rejects_naive() -> None:
    with pytest.raises(ValueError, match="naive"):
        ensure_utc(datetime(2026, 11, 16, 12, 0))


def test_ensure_utc_converts() -> None:
    dt = datetime(2026, 11, 16, 7, 0, tzinfo=ET)
    assert ensure_utc(dt) == datetime(2026, 11, 16, 12, 0, tzinfo=UTC)


def test_et_date_crosses_midnight() -> None:
    # 02:00 UTC on the 17th is still the 16th in New York
    assert et_date(datetime(2026, 11, 17, 2, 0, tzinfo=UTC)) == date(2026, 11, 16)
    assert to_et(datetime(2026, 11, 17, 2, 0, tzinfo=UTC)).hour == 21


@pytest.mark.parametrize(
    ("d", "expected_utc_hour"),
    [(date(2026, 11, 16), 11), (date(2026, 7, 16), 10)],  # EST vs EDT
)
def test_et_to_utc_handles_dst(d: date, expected_utc_hour: int) -> None:
    assert et_to_utc(d, "06:15").hour == expected_utc_hour


def test_ecb_fix_time_in_cet_across_dst() -> None:
    assert local_to_utc(date(2026, 11, 16), "14:15", CET).hour == 13
    assert local_to_utc(date(2026, 7, 16), "14:15", CET).hour == 12


@pytest.mark.parametrize("bad", ["7:00", "24:00", "07:60", "0700", "aa:bb", "07:00:00"])
def test_parse_hhmm_rejects(bad: str) -> None:
    with pytest.raises(ValueError, match=r"HH:MM|must be in"):
        parse_hhmm(bad)


def test_parse_hhmm_ok() -> None:
    assert parse_hhmm("18:05") == time(18, 5)


def test_new_id_is_unique_and_sortable_length() -> None:
    ids = {new_id() for _ in range(100)}
    assert len(ids) == 100
    assert all(len(i) == 26 for i in ids)
