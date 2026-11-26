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

Two tables are not observation tables (P-024, card T64): ``events`` (Today's scheduled releases
and auctions) and ``filings`` (SEC EDGAR filings, Form 4 trades and 13F holder changes per
instrument). Neither feeds ``obs_all``/``obs_latest``, the checks, sigma or the change ranking.
Both are re-fetched each run, and a later fetch may no longer contain a key an earlier one had
(a rescheduled release, a re-parse with fewer facts). So the newest run *replaces* a whole scope
(``REPLACE_SCOPE``) rather than single keys; every reader goes through :func:`current_rows`.
Writer rules:

* within one run a natural key (``EVENTS_NATURAL_KEY`` / ``FILINGS_NATURAL_KEY``) appears once;
* a run writes a scope only when every fetch of it succeeded, and writes all of it (``events``:
  every event of that source and dataset in the horizon; ``filings``: the filing row and every
  fact parsed from that accession);
* ``events.source`` / ``events.dataset`` are exactly the ``source_log`` source and dataset of
  the fetch, so a genuinely empty fetch can be matched to the scope it empties;
* an ``/A`` filing names the accession it amends in ``amends_accession`` when the filing says
  which one; readers then drop the amended filing (:func:`current_rows`).

Reader rules (:func:`current_rows`): the newest run replaces a scope; for ``events`` only a
fetch logged ``empty`` with nothing quarantined can empty it; old accessions are never replaced
by newer ones, so readers filter by date (Form 4 by ``filed_date`` within
``filings.insider_days``; 13F by the latest ``period``; the latest 10-Q/10-K by ``filed_date``).

``filings`` units: ``shares`` in shares, ``price`` in USD per share, ``value_usd`` in USD.
"""

from __future__ import annotations

from collections.abc import Hashable, Iterable, Mapping
from datetime import date, datetime
from typing import Any

import numpy as np
import pandas as pd
import pandera.pandas as pa

from deskbrief.models import (
    CheckType,
    DQStatus,
    EventKind,
    FetchStatus,
    FilingForm,
    FilingRowKind,
    JobKind,
    RunStatus,
    TradeSide,
    Trigger,
)

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
    "event_kind": tuple(k.value for k in EventKind),
    "filing_form": tuple(f.value for f in FilingForm),
    "filing_row": tuple(r.value for r in FilingRowKind),
    "trade_side": tuple(s.value for s in TradeSide),
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
    # P-024 / T64: Today's calendar. One row per scheduled event per run.
    "events": {
        "event_id": "str",  # unique per source+dataset in a run; includes the event date
        "source": "str",  # fiscaldata | fred
        "dataset": "str",
        "kind": "enum:event_kind",
        "title": "str",  # e.g. "Consumer Price Index", "7-Year Note auction"
        "event_date": "date",  # ET calendar date of the event
        "scheduled_at": "ts?",  # UTC instant when the time is known, else null
        "period": "str?",  # reference period as the source names it, e.g. "2026-02"
        "url": "str?",  # the source page for the event
        "details_json": "str",  # source-specific extras (term, CUSIP, offering, release id)
        "fetch_id": "str",
        "run_id": "str",
        "ingested_at": "ts",
    },
    # P-024 / T64: SEC EDGAR, per instrument of kind stock. Row seq 0 is the filing itself;
    # rows 1.. are the facts parsed from it (Form 4 transactions, 13F holder positions).
    "filings": {
        "instrument_id": "str",
        "cik": "str",  # issuer CIK, 10 digits, zero-padded
        "accession": "str",  # EDGAR accession number, 0000000000-00-000000
        "seq": "int",  # 0 = the filing; 1.. = parsed facts in document order
        "row_kind": "enum:filing_row",
        "form": "enum:filing_form",
        "filed_date": "date",  # EDGAR filing date (ET)
        "period": "date?",  # period of report: quarter or year end; 13F as-of quarter end
        "url": "str",  # EDGAR link to the filing
        "party": "str?",  # Form 4 reporting owner, or 13F filer (holder) name
        "party_cik": "str?",  # that party's CIK, 10 digits
        "party_role": "str?",  # Form 4 relationship, e.g. "Director", "Officer: CFO"
        "trade_date": "date?",  # Form 4 transaction date
        "trade_code": "str?",  # Form 4 transaction code (P, S, A, M, F, G, ...)
        "side": "enum:trade_side?",  # Form 4 only
        "shares": "float?",  # Form 4 shares traded; 13F shares held at period end
        "shares_change": "float?",  # 13F: shares held minus the previous quarter's
        "price": "float?",  # Form 4 price per share, USD
        "value_usd": "float?",  # Form 4 shares x price; 13F market value, USD
        "plan_10b5_1": "bool",  # Form 4 row marked as under a Rule 10b5-1 plan; else false
        "details_json": "str",  # extras (security title, ownership form, 13F class)
        "amends_accession": "str?",  # an /A filing: the accession it amends, when known
        "source": "str",  # edgar
        "dataset": "str",
        "fetch_id": "str",
        "run_id": "str",
        "ingested_at": "ts",
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
    "seq",
}
_EXTRA_CHECKS: dict[str, list[pa.Check]] = {
    "tier": [pa.Check.isin([1, 2, 3])],
    "attempt": [pa.Check.ge(1)],
    **{c: [pa.Check.ge(0)] for c in _NONNEG_INT},
    "result_id": [pa.Check.str_length(min_value=1)],
    "fetch_id": [pa.Check.str_length(min_value=1)],
    "run_id": [pa.Check.str_length(min_value=1)],
    "event_id": [pa.Check.str_length(min_value=1)],
    "cik": [pa.Check.str_matches(r"^\d{10}$")],
    "party_cik": [pa.Check.str_matches(r"^\d{10}$")],
    "accession": [pa.Check.str_matches(r"^\d{10}-\d{2}-\d{6}$")],
    "amends_accession": [pa.Check.str_matches(r"^\d{10}-\d{2}-\d{6}$")],
    "shares": [pa.Check.ge(0)],
    "price": [pa.Check.ge(0)],
    "value_usd": [pa.Check.ge(0)],
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

#: Key of a calendar event (``events``), unique within one run. Not a cross-run dedup key:
#: readers use :func:`current_rows`.
EVENTS_NATURAL_KEY: tuple[str, ...] = ("source", "dataset", "event_id")

#: Key of a filing row (``filings``), unique within one run. Readers use :func:`current_rows`.
FILINGS_NATURAL_KEY: tuple[str, ...] = ("instrument_id", "accession", "seq")

#: The unit one run replaces as a whole (P-024 review B1): the newest run that wrote a scope
#: is its only current version, so keys a later fetch dropped do not linger.
REPLACE_SCOPE: dict[str, tuple[str, ...]] = {
    "events": ("source", "dataset"),
    "filings": ("instrument_id", "accession"),
}

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


_ET = "America/New_York"
_FORM4 = ("4", "4/A")
_FORM13F = ("13F-HR", "13F-HR/A")


def _scheduled_on_event_date(df: pd.DataFrame) -> pd.Series:
    """``scheduled_at``, when known, falls on ``event_date`` in New York."""
    known = df["scheduled_at"].notna()
    if not known.any():
        return pd.Series(True, index=df.index)
    et_day = pd.to_datetime(df["scheduled_at"], utc=True).dt.tz_convert(_ET).dt.date
    return ~known | (et_day == df["event_date"])


def _required_where(mask: pd.Series, df: pd.DataFrame, cols: tuple[str, ...]) -> pd.Series:
    ok = pd.Series(True, index=df.index)
    for c in cols:
        ok &= df[c].notna()
    return ~mask | ok


def _filing_row_shape(df: pd.DataFrame) -> pd.Series:
    """seq 0 is exactly the filing row; each fact kind sits on its own form."""
    kind, form = df["row_kind"], df["form"]
    return (
        ((df["seq"] == 0) == (kind == "filing"))
        & ((kind != "insider_trade") | form.isin(_FORM4))
        & ((kind != "holder_change") | form.isin(_FORM13F))
    )


def _insider_trade_fields(df: pd.DataFrame) -> pd.Series:
    cols = ("party", "trade_date", "trade_code", "side", "shares")
    return _required_where(df["row_kind"] == "insider_trade", df, cols)


def _holder_change_fields(df: pd.DataFrame) -> pd.Series:
    cols = ("party", "party_cik", "shares", "shares_change")
    return _required_where(df["row_kind"] == "holder_change", df, cols)


def _thirteen_f_period(df: pd.DataFrame) -> pd.Series:
    return _required_where(df["form"].isin(_FORM13F), df, ("period",))


def _amends_only_from_amendments(df: pd.DataFrame) -> pd.Series:
    """``amends_accession`` only on an ``/A`` form, and never the filing's own accession."""
    named = df["amends_accession"].notna()
    return ~named | (df["form"].str.endswith("/A") & (df["amends_accession"] != df["accession"]))


def _plan_flag_on_trades_only(df: pd.DataFrame) -> pd.Series:
    return ~df["plan_10b5_1"].astype(bool) | (df["row_kind"] == "insider_trade")


#: Cross-column (row-wise) rules, P-024 review round 1.
_FRAME_CHECKS: dict[str, list[pa.Check]] = {
    "events": [
        pa.Check(_scheduled_on_event_date, error="scheduled_at must fall on event_date (ET)"),
    ],
    "filings": [
        pa.Check(_filing_row_shape, error="seq 0 is the filing row; facts match their form"),
        pa.Check(_insider_trade_fields, error="insider_trade rows need party, trade fields"),
        pa.Check(_holder_change_fields, error="holder_change rows need party, holdings"),
        pa.Check(_thirteen_f_period, error="13F rows need period (the as-of quarter end)"),
        pa.Check(_plan_flag_on_trades_only, error="plan_10b5_1 only on insider_trade rows"),
        pa.Check(_amends_only_from_amendments, error="amends_accession only on an /A form"),
    ],
}


def _schema(name: str, decl: Mapping[str, str]) -> pa.DataFrameSchema:
    return pa.DataFrameSchema(
        {c: _column(c, k) for c, k in decl.items()},
        checks=_FRAME_CHECKS.get(name, []),
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
EVENTS_SCHEMA = TABLE_SCHEMAS["events"]
FILINGS_SCHEMA = TABLE_SCHEMAS["filings"]


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


def _utc_column(df: pd.DataFrame, col: str, table: str) -> pd.Series:
    """``df[col]`` as tz-aware UTC; a naive column is a writer bug, so it raises."""
    ser = df[col]
    if not isinstance(ser.dtype, pd.DatetimeTZDtype):
        raise ValueError(f"{table}.{col} must be tz-aware UTC timestamps, got {ser.dtype}")
    utc: pd.Series = ser.dt.tz_convert("UTC")
    return utc


def _newest_by(
    records: Iterable[Mapping[Hashable, Any]], key: tuple[str, ...], at: str
) -> dict[tuple[Any, ...], tuple[Any, str]]:
    """``{key values: (latest at, run_id)}``; ties go to the larger ``run_id``."""
    best: dict[tuple[Any, ...], tuple[Any, str]] = {}
    for rec in records:
        k = tuple(rec[c] for c in key)
        cand = (rec[at], str(rec["run_id"]))
        if k not in best or cand > best[k]:
            best[k] = cand
    return best


def _genuinely_empty_runs(source_log: pd.DataFrame) -> list[dict[Hashable, Any]]:
    """One record per ``(source, dataset, run_id)`` whose every fetch was ``empty`` with nothing
    quarantined: the provider said "nothing", so the scope is replaced by no rows.

    Anything else leaves the previous rows current: a failed fetch, a quarantined parse (logged
    ``ok`` with ``rows_quarantined > 0``; P-024 review N1), or ``ok`` with no rows written.
    """
    log = source_log.assign(responded_at=_utc_column(source_log, "responded_at", "source_log"))
    log = log.assign(
        _empty=(log["status"] == FetchStatus.EMPTY.value) & (log["rows_quarantined"] == 0)
    )
    runs = log.groupby(["source", "dataset", "run_id"], sort=False).agg(
        empty=("_empty", "all"), responded_at=("responded_at", "max")
    )
    out: list[dict[Hashable, Any]] = runs[runs["empty"]].reset_index().to_dict("records")
    return out


def current_rows(
    table: str, df: pd.DataFrame, source_log: pd.DataFrame | None = None
) -> pd.DataFrame:
    """The current version of ``events`` or ``filings``. Every reader uses this (P-024 B1, N1).

    1. Per ``REPLACE_SCOPE``, only the rows of the newest run that wrote that scope survive.
       "Newest" is the run with the latest ``ingested_at`` in the scope. Scheduled runs are
       serialised (one ``deskbrief-pipeline`` concurrency group), so that is the latest run
       start; runs that overlap (a manual local run beside CI) are not, and ties go to the
       larger ``run_id`` (a ULID, which sorts by run start).
    2. ``events`` with ``source_log``: a newer run that wrote no rows for a scope empties it only
       when every fetch of that scope was ``empty`` with zero rows quarantined. A failed fetch,
       a quarantined parse (logged ``ok``) or ``ok`` with no rows leaves the previous rows. This
       matches on ``(source, dataset)``, so ``events.dataset`` must be the fetch's dataset.
    3. ``filings``: a filing is superseded, and all its rows dropped, when a current row of the
       same instrument names it in ``amends_accession``.

    Old accessions are never replaced by newer ones (each is its own scope): readers also filter
    by date (Form 4 by ``filed_date``, the last ``filings.insider_days``; 13F by the latest
    ``period``; the latest 10-Q/10-K by ``filed_date``). Timestamps must be tz-aware (naive
    raises). Returns the same columns and dtypes.
    """
    scope = REPLACE_SCOPE[table]
    if df.empty:
        return df.reset_index(drop=True)
    ingested = _utc_column(df, "ingested_at", table)
    keys = df[[*scope, "run_id"]].assign(ingested_at=ingested)
    records: list[dict[Hashable, Any]] = keys.to_dict("records")
    newest = _newest_by(records, scope, "ingested_at")
    current: dict[tuple[Any, ...], str | None] = {k: run for k, (_, run) in newest.items()}
    if table == "events" and source_log is not None and not source_log.empty:
        empties = _genuinely_empty_runs(source_log)
        for k, (at, run) in _newest_by(empties, ("source", "dataset"), "responded_at").items():
            if k in newest and (at, run) > newest[k]:
                current[k] = None  # a newer complete fetch said nothing is scheduled
    mask = pd.Series(
        [current.get(tuple(r[c] for c in scope)) == r["run_id"] for r in records], index=df.index
    )
    if table == "filings":
        live = df[mask]
        amended = {
            (i, a)
            for i, a in zip(live["instrument_id"], live["amends_accession"], strict=True)
            if isinstance(a, str)
        }
        not_amended = [
            (i, a) not in amended for i, a in zip(df["instrument_id"], df["accession"], strict=True)
        ]
        mask &= pd.Series(not_amended, index=df.index)
    return df[mask].reset_index(drop=True)
