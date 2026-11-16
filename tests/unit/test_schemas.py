"""Schema contract tests. Frames below are SYNTHETIC FIXTURES built in code."""

from datetime import UTC, date, datetime
from typing import Any

import pandas as pd
import pandera.errors
import pytest

from deskbrief.schemas import (
    OBS_NATURAL_KEY,
    OBS_SCHEMA,
    PARSED_OBS_SCHEMA,
    TABLE_SCHEMAS,
    columns,
    validate,
)
from deskbrief.sources.base import RUNNER_FILLED_COLUMNS

T = pd.Timestamp(datetime(2026, 11, 16, 21, 0, tzinfo=UTC))


def _obs(**over: Any) -> pd.DataFrame:
    row: dict[str, Any] = {
        "instrument_id": "SPY",
        "source": "alpaca",
        "dataset": "sip_bars_1m",
        "symbol": "SPY",
        "tier": 2,
        "field": "close",
        "interval": "1m",
        "as_of": T,
        "value": 601.25,
        "unit": "USD",
        "is_derived": False,
        "is_fallback": False,
        "fetch_id": "F1",
        "run_id": "R1",
        "ingested_at": T,
        "parser_version": "1",
    }
    row.update(over)
    df = pd.DataFrame([row])
    for c in ("as_of", "ingested_at"):
        if isinstance(row[c], pd.Timestamp) and row[c].tzinfo is not None:
            df[c] = pd.to_datetime(df[c], utc=True)
    return df


def test_valid_observation_passes() -> None:
    validate("observations", _obs())


def test_parsed_schema_is_obs_minus_runner_columns() -> None:
    assert set(OBS_SCHEMA.columns) - set(PARSED_OBS_SCHEMA.columns) == set(RUNNER_FILLED_COLUMNS)
    assert set(OBS_NATURAL_KEY) <= set(PARSED_OBS_SCHEMA.columns)


@pytest.mark.parametrize(
    "bad",
    [
        {"tier": 4},
        {"field": "price"},
        {"interval": "5m"},
        {"unit": "EUR"},
        {"value": float("nan")},
        {"instrument_id": 5},
    ],
)
def test_bad_values_rejected(bad: dict[str, Any]) -> None:
    with pytest.raises(pandera.errors.SchemaErrors):
        validate("observations", _obs(**bad))


def test_naive_timestamp_rejected() -> None:
    df = _obs()
    df["as_of"] = df["as_of"].dt.tz_localize(None)
    with pytest.raises(pandera.errors.SchemaErrors):
        validate("observations", df)


def test_non_utc_timestamp_rejected() -> None:
    df = _obs()
    df["as_of"] = df["as_of"].dt.tz_convert("America/New_York")
    with pytest.raises(pandera.errors.SchemaErrors):
        validate("observations", df)


def test_float_tier_rejected() -> None:
    df = _obs()
    df["tier"] = df["tier"].astype(float)
    with pytest.raises(pandera.errors.SchemaErrors):
        validate("observations", df)


def test_extra_column_rejected() -> None:
    df = _obs()
    df["surprise"] = 1
    with pytest.raises(pandera.errors.SchemaErrors):
        validate("observations", df)


def test_object_string_columns_allowed() -> None:
    df = _obs()
    df["symbol"] = df["symbol"].astype(object)
    validate("observations", df)


def test_dq_results_dates_and_nullables() -> None:
    df = pd.DataFrame(
        [
            {
                "result_id": "X1",
                "run_id": "R1",
                "check_name": "freshness.fred",
                "check_type": "freshness",
                "source": None,
                "instrument_id": None,
                "target_date": date(2026, 11, 16),
                "evaluated_at": T,
                "status": "skip",
                "observed": None,
                "threshold": None,
                "details_json": "{}",
            }
        ]
    )
    df["evaluated_at"] = pd.to_datetime(df["evaluated_at"], utc=True)
    df["observed"] = df["observed"].astype(float)
    df["threshold"] = df["threshold"].astype(float)
    validate("dq_results", df)
    df["target_date"] = [datetime(2026, 11, 16, tzinfo=UTC)]  # a datetime is not a date
    with pytest.raises(pandera.errors.SchemaErrors):
        validate("dq_results", df)


def test_source_log_nullable_int() -> None:
    row = {c: None for c in columns("source_log")}
    row.update(
        fetch_id="F",
        run_id="R",
        source="fred",
        dataset="series",
        symbols="DGS10",
        request_redacted="GET https://x?api_key=***",
        requested_at=T,
        responded_at=T,
        latency_ms=12,
        status="error",
        error="timeout",
        bytes=0,
        rows_parsed=0,
        rows_quarantined=0,
        attempt=3,
    )
    df = pd.DataFrame([row])
    for c in ("requested_at", "responded_at", "data_min_as_of", "data_max_as_of"):
        df[c] = pd.to_datetime(df[c], utc=True)
    df["http_status"] = pd.array([None], dtype="Int64")
    validate("source_log", df)
    df["status"] = "weird"
    with pytest.raises(pandera.errors.SchemaErrors):
        validate("source_log", df)


def test_all_tables_have_unique_columns_and_names() -> None:
    for name, schema in TABLE_SCHEMAS.items():
        assert schema.name == name
        assert len(columns(name)) == len(set(columns(name)))


# --- helpers: empty_frame / to_frame (review round 1, finding 2) -------------------------
from pathlib import Path  # noqa: E402

from deskbrief.schemas import TABLES, empty_frame, to_frame  # noqa: E402


@pytest.mark.parametrize("table", sorted(TABLES))
def test_empty_frame_is_valid_for_every_table(table: str) -> None:
    df = empty_frame(table)
    assert len(df) == 0
    validate(table, df)


def test_to_frame_fills_missing_nullables_and_validates() -> None:
    df = to_frame(
        "dq_results",
        [
            {
                "result_id": "X1",
                "run_id": "R1",
                "check_name": "freshness.fred",
                "check_type": "freshness",
                "target_date": date(2026, 11, 16),
                "evaluated_at": datetime(2026, 11, 16, 7, 0, tzinfo=UTC),
                "status": "skip",
                "details_json": "{}",
            }
        ],
    )
    validate("dq_results", df)
    assert df["observed"].isna().all()


def test_failed_fetch_row_with_null_http_status_validates() -> None:
    row = {
        "fetch_id": "F",
        "run_id": "R",
        "source": "fred",
        "dataset": "series",
        "symbols": "DGS10",
        "request_redacted": "GET x",
        "requested_at": T,
        "responded_at": T,
        "latency_ms": 0,
        "status": "error",
        "error": "timeout",
        "bytes": 0,
        "rows_parsed": 0,
        "rows_quarantined": 0,
        "attempt": 3,
    }
    validate("source_log", to_frame("source_log", [row]))


def test_parquet_round_trip_keeps_validity(tmp_path: Path) -> None:
    df = to_frame(
        "alerts",
        [{"alert_id": "A", "run_id": "R", "kind": "stale", "severity": "warn", "message": "m"}],
    )
    path = tmp_path / "a.parquet"
    df.to_parquet(path)
    back = pd.read_parquet(path)
    validate("alerts", back)


def test_all_null_object_column_accepted_for_nullable() -> None:
    df = to_frame(
        "alerts",
        [{"alert_id": "A", "run_id": "R", "kind": "stale", "severity": "warn", "message": "m"}],
    )
    df["as_of"] = pd.Series([None], dtype=object)  # what a parquet null column can look like
    validate("alerts", df)


def test_to_frame_rejects_naive_and_unknown() -> None:
    with pytest.raises(ValueError, match="naive"):
        to_frame("alerts", [{"alert_id": "A", "as_of": datetime(2026, 1, 1)}])
    with pytest.raises(KeyError, match="unknown"):
        to_frame("alerts", [{"nope": 1}])


def test_inf_value_and_empty_ids_rejected() -> None:
    with pytest.raises(pandera.errors.SchemaErrors):
        validate("observations", _obs(value=float("inf")))
    with pytest.raises(pandera.errors.SchemaErrors):
        validate("observations", _obs(fetch_id=""))


def test_bool_is_not_an_int_tier() -> None:
    with pytest.raises(pandera.errors.SchemaErrors):
        validate("observations", _obs(tier=True))
