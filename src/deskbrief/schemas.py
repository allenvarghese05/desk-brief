"""Table schemas (spec §7.2), enforced with pandera on every write.

CONTRACT FILE. Changing anything here needs a labeled ``contract:`` PR.

Each table is declared once as ``{column: kind}``. From that single declaration we derive
both the pandera schema and the pandas dtypes used by :func:`empty_frame` / :func:`to_frame`,
so writers never have to hand-build correctly typed frames (empty fetches and all-null
nullable columns are normal, e.g. a skipped DQ check or a failed fetch with no HTTP status).

Kinds:

* ``str`` / ``str?``      strings (``str``/``string``/``object`` dtype)
* ``ts`` / ``ts?``        tz-aware **UTC** datetime64 (any unit); naive is rejected
* ``date`` / ``date?``    ``datetime.date`` values (not datetimes)
* ``int`` / ``int?``      integers (``int64`` / nullable ``Int64``)
* ``float`` / ``float?``  float64 (non-nullable floats must also be finite)
* ``bool``
* ``enum:<name>``         string restricted to ``ENUMS[name]``

Observation time semantics (``as_of``): ``tick`` = trade/quote instant; ``1m`` = bar *start*;
``1d`` = 00:00 America/New_York on the reference date; ``1w`` = 00:00 America/New_York on the
reference date (e.g. the COT report's Tuesday, P-021); ``1mo`` = 00:00 America/New_York on
the first day of the reference month. ``ingested_at`` = ``RawResponse.responded_at`` of the
fetch that produced the row. Units: ``pct`` = percent (4.25 = 4.25%), ``fx_rate`` = quote
currency per unit of base, ``index_pts``, ``USD``, ``shares`` (volume), ``index`` (an index
value that may be negative, e.g. NFCI), ``contracts`` (futures contracts; a net position may
be negative). Fields: ``level`` = an index value that is neither a price nor a rate;
``net_position`` = net speculative position; ``open_interest`` = open interest.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date, datetime
from typing import Any

import numpy as np
import pandas as pd
import pandera.pandas as pa

from deskbrief.models import CheckType, DQStatus, FetchStatus, JobKind, RunStatus, Trigger

ENUMS: dict[str, tuple[str, ...]] = {
    "field": (
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
        "net_position",
        "open_interest",
        "level",
    ),
    "interval": ("tick", "1m", "1d", "1w", "1mo"),
    "unit": ("USD", "pct", "fx_rate", "index_pts", "shares", "index", "contracts"),
    "fetch_status": tuple(s.value for s in FetchStatus),
    "check_type": tuple(c.value for c in CheckType),
    "dq_status": tuple(s.value for s in DQStatus),
    "job": tuple(j.value for j in JobKind),
    "trigger": tuple(t.value for t in Trigger),
    "run_status": tuple(s.value for s in RunStatus),
    "alert_kind": (
        "zscore_move",
        "volume_spike",
        "crosscheck_fail",
        "source_down",
        "sla_miss",
        "stale",
    ),
    "severity": ("info", "warn", "crit"),
}

# Backwards-friendly names
FIELDS = ENUMS["field"]
INTERVALS = ENUMS["interval"]
UNITS = ENUMS["unit"]

# ---------------------------------------------------------------------------
# table declarations: column -> kind
# ---------------------------------------------------------------------------

_OBS_PARSED: dict[str, str] = {
    "instrument_id": "str",
    "source": "str",
    "dataset": "str",
    "symbol": "str",
    "tier": "int",
    "field": "enum:field",
    "interval": "enum:interval",
    "as_of": "ts",
    "value": "float",
    "unit": "enum:unit",
    "is_derived": "bool",
    "is_fallback": "bool",
}

TABLES: dict[str, dict[str, str]] = {
    "parsed_observations": dict(_OBS_PARSED),
    "observations": {
        **_OBS_PARSED,
        "fetch_id": "str",
        "run_id": "str",
        "ingested_at": "ts",
        "parser_version": "str",
    },
    "quarantine": {
        "fetch_id": "str",
        "run_id": "str",
        "source": "str",
        "dataset": "str",
        "reason": "str",
        "detail": "str?",
        "raw_key": "str?",
        "detected_at": "ts",
    },
    "source_log": {
        "fetch_id": "str",
        "run_id": "str",
        "source": "str",
        "dataset": "str",
        "symbols": "str",  # comma-joined
        "request_redacted": "str",
        "requested_at": "ts",
        "responded_at": "ts",
        "latency_ms": "int",
        "http_status": "int?",
        "status": "enum:fetch_status",
        "error": "str?",
        "bytes": "int",
        "sha256": "str?",
        "raw_key": "str?",
        "rows_parsed": "int",
        "rows_quarantined": "int",
        "data_min_as_of": "ts?",
        "data_max_as_of": "ts?",
        "attempt": "int",
    },
    "dq_results": {
        "result_id": "str",
        "run_id": "str",
        "check_name": "str",
        "check_type": "enum:check_type",
        "source": "str?",
        "instrument_id": "str?",
        "target_date": "date",
        "evaluated_at": "ts",
        "status": "enum:dq_status",
        "observed": "float?",
        "threshold": "float?",
        "details_json": "str",
    },
    "crosscheck_results": {
        "result_id": "str",
        "run_id": "str",
        "check_name": "str",
        "group_id": "str?",
        "target_date": "date",
        "at_time": "ts",
        "relation": "str",
        "left_instrument": "str",
        "left_source": "str",
        "left_value": "float?",
        "right_instrument": "str",
        "right_source": "str",
        "right_value": "float?",
        "expected_value": "float?",
        "diff_bps": "float?",
        "warn_bps": "float",
        "fail_bps": "float",
        "status": "enum:dq_status",
        "details_json": "str",
    },
    "revisions": {
        "instrument_id": "str",
        "source": "str",
        "dataset": "str",  # part of the observation natural key
        "field": "enum:field",
        "interval": "enum:interval",
        "as_of": "ts",
        "old_value": "float",
        "new_value": "float",
        "old_fetch_id": "str",
        "new_fetch_id": "str",
        "detected_at": "ts",
        "run_id": "str",
    },
    "alerts": {
        "alert_id": "str",
        "run_id": "str",
        "kind": "enum:alert_kind",
        "severity": "enum:severity",
        "instrument_id": "str?",
        "source": "str?",
        "as_of": "ts?",
        "observed": "float?",
        "threshold": "float?",
        "message": "str",
    },
    "runs": {
        "run_id": "str",
        "job": "enum:job",
        "trigger": "enum:trigger",
        "target_date": "date",
        "scheduled_for": "ts?",
        "started_at": "ts",
        "finished_at": "ts?",
        "status": "enum:run_status",
        "git_sha": "str",
        "config_hash": "str",
        "n_fetch": "int",
        "n_fetch_failed": "int",
        "n_obs": "int",
        "n_dq_fail": "int",
        "n_dq_warn": "int",
        "report_key": "str?",
        "schedule_delay_s": "float?",
    },
    "scorecard_daily": {
        "source": "str",
        "date": "date",
        "fetches": "int",
        "ok_rate": "float",
        "median_latency_ms": "float?",
        "p95_latency_ms": "float?",
        "median_data_age_s": "float?",
        "error_rate": "float",
        "disagreement_rate": "float?",
        "n_crosschecks": "int",
    },
}

_NONNEG_INT = {
    "latency_ms",
    "bytes",
    "rows_parsed",
    "rows_quarantined",
    "n_fetch",
    "n_fetch_failed",
    "n_obs",
    "n_dq_fail",
    "n_dq_warn",
    "fetches",
    "n_crosschecks",
}
_EXTRA_CHECKS: dict[str, list[pa.Check]] = {
    "tier": [pa.Check.isin([1, 2, 3])],
    "attempt": [pa.Check.ge(1)],
    **{c: [pa.Check.ge(0)] for c in _NONNEG_INT},
    "result_id": [pa.Check.str_length(min_value=1)],
    "fetch_id": [pa.Check.str_length(min_value=1)],
    "run_id": [pa.Check.str_length(min_value=1)],
}

#: Natural key of an observation; multiple ingested versions of a key are revisions.
OBS_NATURAL_KEY: tuple[str, ...] = (
    "instrument_id",
    "source",
    "dataset",
    "field",
    "interval",
    "as_of",
)

# ---------------------------------------------------------------------------
# kind -> pandera checks / pandas dtype
# ---------------------------------------------------------------------------


def _all_null(s: pd.Series) -> bool:
    return bool(s.isna().all())


def _is_utc_ts(s: pd.Series) -> bool:
    if isinstance(s.dtype, pd.DatetimeTZDtype):
        return str(s.dtype.tz) == "UTC"
    return False


def _is_str_col(s: pd.Series) -> bool:
    if not (pd.api.types.is_string_dtype(s.dtype) or s.dtype == object):
        return False
    return bool(s.dropna().map(lambda v: isinstance(v, str)).all())


def _is_date_col(s: pd.Series) -> bool:
    if not (s.dtype == object or len(s) == 0):
        return False
    return bool(s.dropna().map(lambda v: isinstance(v, date) and not isinstance(v, datetime)).all())


def _is_int_col(s: pd.Series) -> bool:
    return bool(pd.api.types.is_integer_dtype(s.dtype)) and not pd.api.types.is_bool_dtype(s.dtype)


def _is_float_col(s: pd.Series) -> bool:
    return bool(pd.api.types.is_float_dtype(s.dtype))


def _finite(s: pd.Series) -> bool:
    return bool(np.isfinite(s.dropna().to_numpy(dtype=float)).all())


def _nullable_ok(pred: Any) -> Any:
    """For nullable columns, an all-null column of any dtype is accepted (parquet null type)."""
    return lambda s: _all_null(s) or pred(s)


def _column(name: str, kind: str) -> pa.Column:
    nullable = kind.endswith("?")
    base = kind.rstrip("?")
    checks: list[pa.Check] = []

    def typed(pred: Any, msg: str) -> pa.Check:
        return pa.Check(_nullable_ok(pred) if nullable else pred, error=msg)

    if base == "str":
        checks.append(typed(_is_str_col, "must be strings"))
    elif base == "ts":
        checks.append(typed(_is_utc_ts, "must be tz-aware UTC datetime"))
    elif base == "date":
        checks.append(typed(_is_date_col, "must be datetime.date values"))
    elif base == "int":
        checks.append(typed(_is_int_col, "must be integer dtype"))
    elif base == "float":
        checks.append(typed(_is_float_col, "must be float dtype"))
        if not nullable:
            checks.append(pa.Check(_finite, error="must be finite"))
    elif base == "bool":
        checks.append(pa.Check(lambda s: pd.api.types.is_bool_dtype(s.dtype), error="bool"))
    elif base.startswith("enum:"):
        values = ENUMS[base.split(":", 1)[1]]
        checks.append(typed(_is_str_col, "must be strings"))
        checks.append(pa.Check.isin(list(values)))
    else:  # pragma: no cover - declaration error caught by tests
        raise ValueError(f"unknown kind {kind!r} for {name}")
    checks.extend(_EXTRA_CHECKS.get(name, []))
    return pa.Column(checks=checks, nullable=nullable, required=True)


def _dtype(kind: str) -> Any:
    base = kind.rstrip("?")
    nullable = kind.endswith("?")
    if base in ("str", "date") or base.startswith("enum:"):
        return object
    if base == "ts":
        return pd.DatetimeTZDtype(unit="us", tz="UTC")
    if base == "int":
        return "Int64" if nullable else "int64"
    if base == "float":
        return "float64"
    if base == "bool":
        return "bool"
    raise ValueError(kind)  # pragma: no cover


def _schema(name: str, decl: Mapping[str, str]) -> pa.DataFrameSchema:
    return pa.DataFrameSchema(
        {c: _column(c, k) for c, k in decl.items()},
        strict=True,
        ordered=False,
        name=name,
        coerce=False,
    )


#: Table name -> schema. Names match ``deskbrief.store.backend`` tables.
TABLE_SCHEMAS: dict[str, pa.DataFrameSchema] = {n: _schema(n, d) for n, d in TABLES.items()}

PARSED_OBS_SCHEMA = TABLE_SCHEMAS["parsed_observations"]
OBS_SCHEMA = TABLE_SCHEMAS["observations"]
QUARANTINE_SCHEMA = TABLE_SCHEMAS["quarantine"]
SOURCE_LOG_SCHEMA = TABLE_SCHEMAS["source_log"]
DQ_SCHEMA = TABLE_SCHEMAS["dq_results"]
XCHECK_SCHEMA = TABLE_SCHEMAS["crosscheck_results"]
REVISIONS_SCHEMA = TABLE_SCHEMAS["revisions"]
ALERTS_SCHEMA = TABLE_SCHEMAS["alerts"]
RUNS_SCHEMA = TABLE_SCHEMAS["runs"]
SCORECARD_SCHEMA = TABLE_SCHEMAS["scorecard_daily"]


def columns(table: str) -> tuple[str, ...]:
    """Column names of a table, in declaration order."""
    return tuple(TABLES[table])


def empty_frame(table: str) -> pd.DataFrame:
    """A zero-row frame with the table's columns and dtypes (valid against the schema)."""
    return pd.DataFrame({c: pd.Series([], dtype=_dtype(k)) for c, k in TABLES[table].items()})


def to_frame(table: str, rows: Iterable[Mapping[str, Any]]) -> pd.DataFrame:
    """Build a correctly typed frame from row dicts. Missing nullable columns become null.

    Timestamps may be aware ``datetime`` / ``pd.Timestamp`` in any zone (converted to UTC);
    naive timestamps raise. Unknown keys raise ``KeyError``.
    """
    decl = TABLES[table]
    rows = list(rows)
    for r in rows:
        extra = set(r) - set(decl)
        if extra:
            raise KeyError(f"{table}: unknown columns {sorted(extra)}")
    data: dict[str, pd.Series] = {}
    for col, kind in decl.items():
        vals = [r.get(col) for r in rows]
        if not kind.endswith("?") and any(v is None for v in vals):
            raise ValueError(f"{table}.{col}: required (non-nullable) value missing")
        base = kind.rstrip("?")
        if base == "ts":
            if any(isinstance(v, datetime) and v.tzinfo is None for v in vals):
                raise ValueError(f"{table}.{col}: naive datetime")
            ser = pd.Series(pd.to_datetime(pd.Series(vals, dtype=object), utc=True))
            ser = ser.astype(_dtype(kind))
        else:
            ser = pd.Series(vals, dtype=_dtype(kind))
        data[col] = ser
    return pd.DataFrame(data)


def validate(table: str, df: pd.DataFrame) -> pd.DataFrame:
    """Validate ``df`` against the table schema; raises ``pandera.errors.SchemaErrors``."""
    return TABLE_SCHEMAS[table].validate(df, lazy=True)
