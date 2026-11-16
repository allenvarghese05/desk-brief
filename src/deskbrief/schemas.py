"""Table schemas (spec §7.2), enforced with pandera on every write.

CONTRACT FILE. Changing anything here needs a labeled ``contract:`` PR.

Type rules (independent of pandas' default string/datetime resolution):
* timestamps: tz-aware **UTC** datetime64 (any unit); naive timestamps are rejected
* strings: ``str``/``string``/``object`` columns whose non-null values are all ``str``
* dates (``target_date``, ``date``): ``datetime.date`` values (not datetimes)
* integers: any integer dtype (nullable ``Int64`` where noted); floats: float64
Schemas are strict: unknown columns are rejected.
"""

from __future__ import annotations

from datetime import date, datetime

import pandas as pd
import pandera.pandas as pa

from deskbrief.models import CheckType, DQStatus, FetchStatus, JobKind, RunStatus, Trigger

FIELDS: tuple[str, ...] = (
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
INTERVALS: tuple[str, ...] = ("tick", "1m", "1d", "1mo")
UNITS: tuple[str, ...] = ("USD", "pct", "fx_rate", "index_pts", "shares")
ALERT_KINDS: tuple[str, ...] = (
    "zscore_move",
    "volume_spike",
    "crosscheck_fail",
    "source_down",
    "sla_miss",
    "stale",
)
ALERT_SEVERITIES: tuple[str, ...] = ("info", "warn", "crit")


# ---------------------------------------------------------------------------
# column helpers
# ---------------------------------------------------------------------------


def _is_utc_ts(s: pd.Series) -> bool:
    return isinstance(s.dtype, pd.DatetimeTZDtype) and str(s.dtype.tz) == "UTC"


def _is_str_col(s: pd.Series) -> bool:
    if not (pd.api.types.is_string_dtype(s.dtype) or s.dtype == object):
        return False
    return bool(s.dropna().map(lambda v: isinstance(v, str)).all())


def _is_date_col(s: pd.Series) -> bool:
    vals = s.dropna()
    return bool(vals.map(lambda v: isinstance(v, date) and not isinstance(v, datetime)).all())


def _ts(nullable: bool = False) -> pa.Column:
    return pa.Column(
        checks=pa.Check(_is_utc_ts, error="must be tz-aware UTC datetime"), nullable=nullable
    )


def _str(nullable: bool = False) -> pa.Column:
    return pa.Column(checks=pa.Check(_is_str_col, error="must be strings"), nullable=nullable)


def _enum(values: tuple[str, ...] | list[str], nullable: bool = False) -> pa.Column:
    return pa.Column(
        checks=[pa.Check(_is_str_col, error="must be strings"), pa.Check.isin(list(values))],
        nullable=nullable,
    )


def _date(nullable: bool = False) -> pa.Column:
    return pa.Column(
        checks=pa.Check(_is_date_col, error="must be datetime.date values"), nullable=nullable
    )


def _int(nullable: bool = False, ge: int | None = None) -> pa.Column:
    checks = [
        pa.Check(lambda s: pd.api.types.is_integer_dtype(s.dtype), error="must be integer dtype")
    ]
    if ge is not None:
        checks.append(pa.Check.ge(ge))
    return pa.Column(checks=checks, nullable=nullable)


def _float(nullable: bool = False) -> pa.Column:
    return pa.Column(float, nullable=nullable)


def _schema(name: str, columns: dict[str, pa.Column]) -> pa.DataFrameSchema:
    return pa.DataFrameSchema(columns, strict=True, ordered=False, name=name, coerce=False)


# ---------------------------------------------------------------------------
# tables
# ---------------------------------------------------------------------------

_OBS_PARSED: dict[str, pa.Column] = {
    "instrument_id": _str(),
    "source": _str(),
    "dataset": _str(),
    "symbol": _str(),
    "tier": pa.Column(
        checks=[
            pa.Check(lambda s: pd.api.types.is_integer_dtype(s.dtype), error="must be integer"),
            pa.Check.isin([1, 2, 3]),
        ]
    ),
    "field": _enum(FIELDS),
    "interval": _enum(INTERVALS),
    "as_of": _ts(),
    "value": _float(),
    "unit": _enum(UNITS),
    "is_derived": pa.Column(bool),
    "is_fallback": pa.Column(bool),
}

#: What a ``Source.parse`` must return (lineage columns are added by the runner).
PARSED_OBS_SCHEMA = _schema("parsed_observations", dict(_OBS_PARSED))

OBS_SCHEMA = _schema(
    "observations",
    {
        **_OBS_PARSED,
        "fetch_id": _str(),
        "run_id": _str(),
        "ingested_at": _ts(),
        "parser_version": _str(),
    },
)

#: Natural key of an observation; multiple ingested versions of a key are revisions.
OBS_NATURAL_KEY: tuple[str, ...] = (
    "instrument_id",
    "source",
    "dataset",
    "field",
    "interval",
    "as_of",
)

QUARANTINE_SCHEMA = _schema(
    "quarantine",
    {
        "fetch_id": _str(),
        "run_id": _str(),
        "source": _str(),
        "dataset": _str(),
        "reason": _str(),
        "detail": _str(nullable=True),
        "raw_key": _str(nullable=True),
        "detected_at": _ts(),
    },
)

SOURCE_LOG_SCHEMA = _schema(
    "source_log",
    {
        "fetch_id": _str(),
        "run_id": _str(),
        "source": _str(),
        "dataset": _str(),
        "symbols": _str(),  # comma-joined
        "request_redacted": _str(),
        "requested_at": _ts(),
        "responded_at": _ts(),
        "latency_ms": _int(ge=0),
        "http_status": _int(nullable=True),
        "status": _enum([s.value for s in FetchStatus]),
        "error": _str(nullable=True),
        "bytes": _int(ge=0),
        "sha256": _str(nullable=True),
        "raw_key": _str(nullable=True),
        "rows_parsed": _int(ge=0),
        "rows_quarantined": _int(ge=0),
        "data_min_as_of": _ts(nullable=True),
        "data_max_as_of": _ts(nullable=True),
        "attempt": _int(ge=1),
    },
)

DQ_SCHEMA = _schema(
    "dq_results",
    {
        "result_id": _str(),
        "run_id": _str(),
        "check_name": _str(),
        "check_type": _enum([c.value for c in CheckType]),
        "source": _str(nullable=True),
        "instrument_id": _str(nullable=True),
        "target_date": _date(),
        "evaluated_at": _ts(),
        "status": _enum([s.value for s in DQStatus]),
        "observed": _float(nullable=True),
        "threshold": _float(nullable=True),
        "details_json": _str(),
    },
)

XCHECK_SCHEMA = _schema(
    "crosscheck_results",
    {
        "result_id": _str(),
        "run_id": _str(),
        "check_name": _str(),
        "group_id": _str(nullable=True),
        "target_date": _date(),
        "at_time": _ts(),
        "relation": _str(),
        "left_instrument": _str(),
        "left_source": _str(),
        "left_value": _float(nullable=True),
        "right_instrument": _str(),
        "right_source": _str(),
        "right_value": _float(nullable=True),
        "expected_value": _float(nullable=True),
        "diff_bps": _float(nullable=True),
        "warn_bps": _float(),
        "fail_bps": _float(),
        "status": _enum([s.value for s in DQStatus]),
        "details_json": _str(),
    },
)

REVISIONS_SCHEMA = _schema(
    "revisions",
    {
        "instrument_id": _str(),
        "source": _str(),
        "dataset": _str(),
        "field": _enum(FIELDS),
        "interval": _enum(INTERVALS),
        "as_of": _ts(),
        "old_value": _float(),
        "new_value": _float(),
        "old_fetch_id": _str(),
        "new_fetch_id": _str(),
        "detected_at": _ts(),
        "run_id": _str(),
    },
)

ALERTS_SCHEMA = _schema(
    "alerts",
    {
        "alert_id": _str(),
        "run_id": _str(),
        "kind": _enum(ALERT_KINDS),
        "severity": _enum(ALERT_SEVERITIES),
        "instrument_id": _str(nullable=True),
        "source": _str(nullable=True),
        "as_of": _ts(nullable=True),
        "observed": _float(nullable=True),
        "threshold": _float(nullable=True),
        "message": _str(),
    },
)

RUNS_SCHEMA = _schema(
    "runs",
    {
        "run_id": _str(),
        "job": _enum([j.value for j in JobKind]),
        "trigger": _enum([t.value for t in Trigger]),
        "target_date": _date(),
        "scheduled_for": _ts(nullable=True),
        "started_at": _ts(),
        "finished_at": _ts(nullable=True),
        "status": _enum([s.value for s in RunStatus]),
        "git_sha": _str(),
        "config_hash": _str(),
        "n_fetch": _int(ge=0),
        "n_fetch_failed": _int(ge=0),
        "n_obs": _int(ge=0),
        "n_dq_fail": _int(ge=0),
        "n_dq_warn": _int(ge=0),
        "report_key": _str(nullable=True),
        "schedule_delay_s": _float(nullable=True),
    },
)

SCORECARD_SCHEMA = _schema(
    "scorecard_daily",
    {
        "source": _str(),
        "date": _date(),
        "fetches": _int(ge=0),
        "ok_rate": _float(),
        "median_latency_ms": _float(nullable=True),
        "p95_latency_ms": _float(nullable=True),
        "median_data_age_s": _float(nullable=True),
        "error_rate": _float(),
        "disagreement_rate": _float(nullable=True),
        "n_crosschecks": _int(ge=0),
    },
)

#: Table name -> schema. Names match ``deskbrief.store.backend.TABLE_LAYER`` (+ scorecard).
TABLE_SCHEMAS: dict[str, pa.DataFrameSchema] = {
    "observations": OBS_SCHEMA,
    "quarantine": QUARANTINE_SCHEMA,
    "source_log": SOURCE_LOG_SCHEMA,
    "dq_results": DQ_SCHEMA,
    "crosscheck_results": XCHECK_SCHEMA,
    "revisions": REVISIONS_SCHEMA,
    "alerts": ALERTS_SCHEMA,
    "runs": RUNS_SCHEMA,
    "scorecard_daily": SCORECARD_SCHEMA,
}


def columns(table: str) -> tuple[str, ...]:
    """Column names of a table, in declaration order."""
    return tuple(TABLE_SCHEMAS[table].columns)


def validate(table: str, df: pd.DataFrame) -> pd.DataFrame:
    """Validate ``df`` against the table schema; raises ``pandera.errors.SchemaErrors``."""
    return TABLE_SCHEMAS[table].validate(df, lazy=True)
