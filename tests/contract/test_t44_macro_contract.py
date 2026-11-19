"""Contract tests for card T44 (rulings P-020 and P-021): history job, range planning, weekly
cadence, new units and fields. Every frame and config below is a SYNTHETIC FIXTURE built in code.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any, get_args
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
from pydantic import ValidationError

from deskbrief import cli
from deskbrief.config import (
    KNOWN_SOURCES,
    ChecksSettings,
    DatasetSchedule,
    Instrument,
    Unit,
    load_config,
)
from deskbrief.models import FetchRequest, JobKind, RawResponse, RunContext
from deskbrief.schemas import ENUMS, OBS_SCHEMA, PARSED_OBS_SCHEMA
from deskbrief.sources.base import RangeSource, Source, supports_range
from deskbrief.store import backend
from deskbrief.timeutil import et_date

ET = ZoneInfo("America/New_York")


# ---------------------------------------------------------------------------------------------
# enums and literals pinned
# ---------------------------------------------------------------------------------------------


def test_job_enum_gains_history() -> None:
    assert JobKind.HISTORY.value == "history"
    assert ENUMS["job"] == ("morning", "intraday", "eod", "backfill", "rebuild", "history")


def test_interval_enum_gains_weekly() -> None:
    assert ENUMS["interval"] == ("tick", "1m", "1d", "1w", "1mo")


def test_unit_enum_gains_index_and_contracts() -> None:
    assert ENUMS["unit"] == ("USD", "pct", "fx_rate", "index_pts", "shares", "index", "contracts")


def test_field_enum_gains_positioning_and_level() -> None:
    for field in ("net_position", "open_interest", "level"):
        assert field in ENUMS["field"]
    # the existing fields are unchanged and keep their order
    assert ENUMS["field"][:11] == (
        "open",
        "high",
        "low",
        "close",
        "last",
        "bid",
        "ask",
        "mid",
        "volume",
        "yield",
        "rate",
    )


def test_config_unit_literal_gains_index_and_contracts() -> None:
    assert get_args(Unit) == ("USD", "pct", "fx_rate", "index_pts", "index", "contracts")
    # every config unit is a schema unit, so a configured instrument always writes a valid row
    assert set(get_args(Unit)) <= set(ENUMS["unit"])


def test_known_sources_gains_cftc() -> None:
    assert "cftc" in KNOWN_SOURCES
    assert {"treasury", "fred", "ecb", "stooq", "cboe", "alpaca", "twelvedata", "yahoo"} <= (
        KNOWN_SOURCES
    )


# ---------------------------------------------------------------------------------------------
# weekly rows (P-021: as_of = 00:00 America/New_York on the reference date)
# ---------------------------------------------------------------------------------------------

#: Reference Tuesday of a synthetic COT report.
REF = date(2026, 11, 17)
AS_OF = pd.Timestamp(datetime(REF.year, REF.month, REF.day, tzinfo=ET)).tz_convert(UTC)
T = pd.Timestamp(datetime(2026, 11, 20, 20, 30, tzinfo=UTC))


def _weekly_row(**over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "instrument_id": "COT_GC_NET",
        "source": "cftc",
        "dataset": "cot",
        "symbol": "088691",
        "tier": 3,
        "field": "net_position",
        "interval": "1w",
        "as_of": AS_OF,
        "value": -12345.0,
        "unit": "contracts",
        "is_derived": False,
        "is_fallback": False,
        "fetch_id": "F1",
        "run_id": "R1",
        "ingested_at": T,
        "parser_version": "1",
    }
    row.update(over)
    return row


def test_weekly_row_validates_at_midnight_et_of_reference_date() -> None:
    frame = pd.DataFrame([_weekly_row()])
    OBS_SCHEMA.validate(frame)
    assert et_date(AS_OF.to_pydatetime()) == REF
    assert AS_OF.tz_convert(ET).hour == 0


@pytest.mark.parametrize(
    ("field", "unit", "value"),
    [
        ("net_position", "contracts", -250000.0),
        ("open_interest", "contracts", 500000.0),
        ("level", "index", -0.52),  # NFCI is negative when conditions are loose
        ("level", "index_pts", 121.4),  # broad dollar index
    ],
)
def test_new_field_unit_pairs_validate(field: str, unit: str, value: float) -> None:
    row = _weekly_row(field=field, unit=unit, value=value)
    parsed = {k: v for k, v in row.items() if k not in ("fetch_id", "run_id", "ingested_at")}
    parsed.pop("parser_version")
    PARSED_OBS_SCHEMA.validate(pd.DataFrame([parsed]))
    OBS_SCHEMA.validate(pd.DataFrame([row]))


def test_backend_docstring_records_weekly_as_of_rule() -> None:
    doc = backend.__doc__ or ""
    assert "``1w`` = 00:00 America/New_York on the reference date" in doc


# ---------------------------------------------------------------------------------------------
# config: release_weekday, validity bounds, macro instruments
# ---------------------------------------------------------------------------------------------


def _schedule(**over: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "source": "cftc",
        "dataset": "cot",
        "expected_by_et": "15:30",
        "lag_bdays": 0,
        "calendar": "XNYS",
    }
    raw.update(over)
    return raw


def test_schedule_with_release_weekday_validates() -> None:
    s = DatasetSchedule.model_validate(_schedule(release_weekday=4))
    assert s.release_weekday == 4


def test_existing_schedules_have_no_release_weekday() -> None:
    assert DatasetSchedule.model_validate(_schedule()).release_weekday is None
    cfg = load_config("config.yaml")
    assert cfg.datasets, "the real config has freshness entries"
    # Weekly datasets added by later cards (T46 FRED NFCI) carry one; the daily ones never do.
    weekly = {("fred", "series_weekly")}
    assert all(
        s.release_weekday is None for s in cfg.datasets if (s.source, s.dataset) not in weekly
    )


@pytest.mark.parametrize("bad", [-1, 7, True, "4"])
def test_release_weekday_out_of_range_rejected(bad: object) -> None:
    with pytest.raises(ValidationError):
        DatasetSchedule.model_validate(_schedule(release_weekday=bad))


def test_real_config_has_validity_bounds_for_new_units() -> None:
    validity = load_config("config.yaml").checks.validity
    assert validity["index"] == (-100, 100000)
    assert validity["contracts"] == (-10000000, 100000000)
    lo, hi = validity["index"]
    assert lo < -0.52 < hi, "a negative NFCI reading is inside the index bounds"
    lo, hi = validity["contracts"]
    assert lo < -250000 < hi, "a net short position is inside the contracts bounds"


def test_validity_accepts_new_units() -> None:
    s = ChecksSettings.model_validate(
        {
            "anomaly": {"window_days": 20, "z_warn": 3, "z_fail": 5},
            "validity": {"index": [-100, 100000], "contracts": [-10000000, 100000000]},
        }
    )
    assert set(s.validity) == {"index", "contracts"}


@pytest.mark.parametrize(
    ("kind", "unit"),
    [("macro", "index_pts"), ("macro", "index"), ("macro", "contracts")],
)
def test_macro_instruments_with_new_units_validate(kind: str, unit: str) -> None:
    inst = Instrument.model_validate(
        {
            "id": "SYNTH_MACRO",
            "name": "synthetic macro series",
            "asset_class": "macro",
            "kind": kind,
            "unit": unit,
            "feeds": [{"source": "fred", "dataset": "series", "symbol": "X", "tier": 3}],
        }
    )
    assert inst.unit == unit


# ---------------------------------------------------------------------------------------------
# sources/base.py: optional plan_range
# ---------------------------------------------------------------------------------------------


class _NoRange:
    name = "synth"
    parser_version = "1"

    def plan(self, job: JobKind, ctx: RunContext) -> list[FetchRequest]:
        return []

    def fetch(self, req: FetchRequest, http: Any) -> RawResponse:
        raise NotImplementedError

    def parse(self, raw: RawResponse) -> pd.DataFrame:
        raise NotImplementedError


class _WithRange(_NoRange):
    def plan_range(self, start: date, end: date, ctx: RunContext) -> list[FetchRequest]:
        return []


class _NotCallable(_NoRange):
    plan_range = None


def test_adapter_without_plan_range_has_no_range_support() -> None:
    src = _NoRange()
    assert isinstance(src, Source)
    assert not supports_range(src)
    assert not isinstance(src, RangeSource)


def test_adapter_with_plan_range_has_range_support() -> None:
    src = _WithRange()
    assert isinstance(src, Source)
    assert supports_range(src)
    assert isinstance(src, RangeSource)


def test_non_callable_plan_range_is_not_range_support() -> None:
    assert not supports_range(_NotCallable())


# ---------------------------------------------------------------------------------------------
# cli: `run history` is rejected until T53 adds its own subcommand
# ---------------------------------------------------------------------------------------------


def test_run_history_rejected_without_running_anything(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _boom(*_a: object, **_k: object) -> None:
        raise AssertionError("no pipeline step may run for `run history`")

    for name in ("load_config", "build_store", "build_http", "run_job", "run_rebuild"):
        monkeypatch.setattr(cli, name, _boom)

    assert cli.main(["run", "history"]) == 2
    assert "history is its own subcommand (card T53)" in capsys.readouterr().err
