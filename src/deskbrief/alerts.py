"""Alerts (T40, spec §5.4 "checks → alerts", §7.2 ``alerts``): this run's evidence as rows.

:func:`build_alerts` turns what the run already wrote into ``alerts`` rows; :func:`alerts_step`
makes the one append for the run (P-008). Kinds produced, and where each comes from:

``zscore_move``    the ``anomaly`` check's rows for this run (``dq``). That check owns the
                   z-score maths (``checks.anomaly.daily_moves`` and ``MIN_MOVES``), so a move is
                   alerted exactly when the check scored it; nothing is recomputed here.
                   ``fail`` -> crit, ``warn`` -> warn, a passing ``z >= Z_INFO`` -> info.
``volume_spike``   latest daily ``volume`` against the mean of the ``window_days`` days before it
                   (``obs_pit``), ratio ``>= VOLUME_RATIO_INFO/WARN/CRIT``. Same guards as the
                   anomaly check: at least ``MIN_MOVES`` earlier days, and only a series that has
                   reached the expected data day (an old spike is not news). Only Stooq daily bars
                   carry ``volume`` today (Yahoo's is kept in raw only, Alpaca's is 1-minute and
                   IEX-only); a store with no daily volume simply yields no rows.
``crosscheck_fail``  this run's ``crosscheck_results`` rows: ``fail`` -> crit, ``warn`` -> warn.
``source_down``    ``source_log`` of this run: share of a source's fetches that ended
                   ``error`` / ``rate_limited`` / ``budget_exhausted``: all failed -> crit,
                   ``>= SOURCE_DOWN_WARN_FRACTION`` -> warn, any failure -> info.
``stale``          this run's ``freshness`` rows: ``fail`` -> crit, ``warn`` -> warn.

``sla_miss`` is not produced: it needs the SLA workflow's data (T24B), a different card.

The step runs after the checks hook has flushed (so ``dq`` / ``xchecks`` hold this run) and
before serving. A crash is isolated: :func:`with_alerts` still runs the next hook, then re-raises
so the runner marks the run ``degraded``.
"""

from __future__ import annotations

import functools
import json
from collections.abc import Callable
from datetime import date, timedelta
from typing import Any

import duckdb
import numpy as np
import pandas as pd

from deskbrief.checks.anomaly import MIN_MOVES, expected_last_day
from deskbrief.checks.base import ET_DATE_SQL, query
from deskbrief.ids import new_id
from deskbrief.logging import get_logger
from deskbrief.models import RunContext
from deskbrief.schemas import empty_frame, to_frame
from deskbrief.store.backend import KeyExistsError
from deskbrief.store.datasets import Datasets
from deskbrief.store.duck import connect
from deskbrief.timeutil import et_date

# --- thresholds (severity ladders) ------------------------------------------------------------
Z_INFO = 2.0  # a passing anomaly score at or above this is worth a line (warn/fail are the check's)
VOLUME_RATIO_INFO = 2.0  # latest volume / mean of the window before it
VOLUME_RATIO_WARN = 3.0
VOLUME_RATIO_CRIT = 5.0
SOURCE_DOWN_WARN_FRACTION = 0.5  # share of a source's fetches that failed
SOURCE_DOWN_CRIT_FRACTION = 1.0  # every fetch failed
_FAILED_FETCH = ("error", "rate_limited", "budget_exhausted")
_SEVERITY_OF_STATUS = {"fail": "crit", "warn": "warn"}
Row = dict[str, Any]


def _row(run_id: str, kind: str, severity: str, message: str, **fields: Any) -> Row:
    return {
        "alert_id": new_id(), "run_id": run_id, "kind": kind, "severity": severity,
        "message": message, **fields,
    }  # fmt: skip


def _details(raw: object) -> dict[str, Any]:
    try:
        loaded = json.loads(str(raw))
    except ValueError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _num(value: object) -> float | None:
    return None if value is None or value != value else float(value)  # type: ignore[arg-type]


def _dq(duck: duckdb.DuckDBPyConnection, ctx: RunContext, check_type: str) -> list[Row]:
    df = query(
        duck,
        "SELECT check_name, source, instrument_id, status, observed, threshold, details_json "
        "FROM dq WHERE run_id = ? AND check_type = ?",
        [ctx.run_id, check_type],
    )
    return [{str(k): v for k, v in r.items()} for r in df.to_dict("records")]


# --- z-score moves ----------------------------------------------------------------------------


def zscore_alerts(duck: duckdb.DuckDBPyConnection, ctx: RunContext) -> list[Row]:
    out: list[Row] = []
    for r in _dq(duck, ctx, "anomaly"):
        z = _num(r["observed"])
        status = str(r["status"])
        severity = _SEVERITY_OF_STATUS.get(status)
        threshold = _num(r["threshold"])
        if severity is None and status == "pass" and z is not None and z >= Z_INFO:
            severity, threshold = "info", Z_INFO
        if severity is None or z is None:
            continue
        info = _details(r["details_json"])
        day = info.get("as_of_day")
        unit = "bp" if info.get("move_unit") == "bp" else "%"
        move = _num(info.get("move"))
        shown = "" if move is None else f", {move * (1 if unit == 'bp' else 100):.2f}{unit}"
        out.append(
            _row(
                ctx.run_id, "zscore_move", severity,
                f"{r['instrument_id']} ({r['source']}) moved z={z:.1f} vs its recent range{shown}",
                instrument_id=r["instrument_id"], source=r["source"],
                as_of=_day_ts(day), observed=z, threshold=threshold,
            )
        )  # fmt: skip
    return out


def _day_ts(day: object) -> pd.Timestamp | None:
    if not day:
        return None
    from deskbrief.timeutil import ET

    return pd.Timestamp(date.fromisoformat(str(day)[:10]), tz=ET).tz_convert("UTC")


# --- volume spikes ----------------------------------------------------------------------------


def _volume_severity(ratio: float) -> str | None:
    if ratio >= VOLUME_RATIO_CRIT:
        return "crit"
    if ratio >= VOLUME_RATIO_WARN:
        return "warn"
    return "info" if ratio >= VOLUME_RATIO_INFO else None


def volume_alerts(duck: duckdb.DuckDBPyConnection, ctx: RunContext) -> list[Row]:
    window = ctx.config.checks.anomaly.window_days
    cutoff = min(ctx.target_date, et_date(ctx.now_utc) - timedelta(days=1))
    df = query(
        duck,
        f"SELECT instrument_id, source, dataset, symbol, {ET_DATE_SQL} AS d, value "  # noqa: S608
        f"FROM obs_pit(?) WHERE interval = '1d' AND field = 'volume' AND {ET_DATE_SQL} <= ? "
        "ORDER BY instrument_id, source, dataset, d",
        [ctx.now_utc, cutoff],
    )
    out: list[Row] = []
    for (inst, source, dataset), g in df.groupby(["instrument_id", "source", "dataset"], sort=True):
        latest = date.fromisoformat(str(g["d"].iloc[-1])[:10])
        if latest < expected_last_day(
            ctx, str(source), str(dataset), str(g["symbol"].iloc[-1]), cutoff
        ):
            continue  # stale series: an old spike is not news
        values = g["value"].to_numpy(dtype=float)
        hist = values[-(window + 1) : -1]
        hist = hist[np.isfinite(hist)]
        if len(hist) < MIN_MOVES or not np.isfinite(values[-1]) or hist.mean() <= 0:
            continue
        ratio = float(values[-1] / hist.mean())
        severity = _volume_severity(ratio)
        if severity is None:
            continue
        threshold = {"crit": VOLUME_RATIO_CRIT, "warn": VOLUME_RATIO_WARN}.get(
            severity, VOLUME_RATIO_INFO
        )
        out.append(
            _row(
                ctx.run_id, "volume_spike", severity,
                f"{inst} ({source}) volume {ratio:.1f}x its {len(hist)}-day average",
                instrument_id=str(inst), source=str(source), as_of=_day_ts(latest),
                observed=ratio, threshold=threshold,
            )
        )  # fmt: skip
    return out


# --- cross-check fails ------------------------------------------------------------------------


def crosscheck_alerts(duck: duckdb.DuckDBPyConnection, ctx: RunContext) -> list[Row]:
    df = query(
        duck,
        "SELECT check_name, left_instrument, left_source, right_instrument, right_source, "
        "at_time, diff_bps, warn_bps, fail_bps, status FROM xchecks "
        "WHERE run_id = ? AND status IN ('warn', 'fail')",
        [ctx.run_id],
    )
    out: list[Row] = []
    for r in df.to_dict("records"):
        fail = r["status"] == "fail"
        diff = _num(r["diff_bps"])
        out.append(
            _row(
                ctx.run_id, "crosscheck_fail", "crit" if fail else "warn",
                f"{r['check_name']}: {r['left_instrument']} ({r['left_source']}) vs "
                f"{r['right_instrument']} ({r['right_source']}) differ"
                + ("" if diff is None else f" by {diff:.1f} bp"),
                instrument_id=r["left_instrument"], source=r["left_source"],
                as_of=pd.Timestamp(r["at_time"]).tz_convert("UTC"),
                observed=diff, threshold=float(r["fail_bps"] if fail else r["warn_bps"]),
            )
        )  # fmt: skip
    return out


# --- source down ------------------------------------------------------------------------------


def source_down_alerts(duck: duckdb.DuckDBPyConnection, ctx: RunContext) -> list[Row]:
    marks = ", ".join(f"'{s}'" for s in _FAILED_FETCH)
    df = query(
        duck,
        f"SELECT source, count(*) AS n, sum(CASE WHEN status IN ({marks}) THEN 1 ELSE 0 END) "  # noqa: S608
        "AS failed FROM source_log WHERE run_id = ? GROUP BY source ORDER BY source",
        [ctx.run_id],
    )
    out: list[Row] = []
    for r in df.to_dict("records"):
        n, failed = int(r["n"]), int(r["failed"])
        if not failed:
            continue
        frac = failed / n
        if frac >= SOURCE_DOWN_CRIT_FRACTION:
            severity, threshold = "crit", SOURCE_DOWN_CRIT_FRACTION
        elif frac >= SOURCE_DOWN_WARN_FRACTION:
            severity, threshold = "warn", SOURCE_DOWN_WARN_FRACTION
        else:
            severity, threshold = "info", 0.0
        out.append(
            _row(
                ctx.run_id, "source_down", severity,
                f"{r['source']}: {failed} of {n} fetches failed",
                source=r["source"], as_of=pd.Timestamp(ctx.now_utc),
                observed=frac, threshold=threshold,
            )
        )  # fmt: skip
    return out


# --- stale ------------------------------------------------------------------------------------


def stale_alerts(duck: duckdb.DuckDBPyConnection, ctx: RunContext) -> list[Row]:
    out: list[Row] = []
    for r in _dq(duck, ctx, "freshness"):
        severity = _SEVERITY_OF_STATUS.get(str(r["status"]))
        if severity is None:
            continue
        info = _details(r["details_json"])
        what = info.get("dataset") or info.get("feed") or "data"
        late = _num(r["observed"])
        out.append(
            _row(
                ctx.run_id, "stale", severity,
                f"{r['source']}/{what} stale: expected data day {info.get('data_day', '?')}"
                + ("" if late is None else f", {late:.0f} min late"),
                instrument_id=r["instrument_id"], source=r["source"],
                as_of=pd.Timestamp(ctx.now_utc), observed=late, threshold=_num(r["threshold"]),
            )
        )  # fmt: skip
    return out


# --- assembly and the hook --------------------------------------------------------------------


def build_alerts(ctx: RunContext, duck: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """All alert rows for ``ctx.run_id`` as a valid ``alerts`` frame (empty when quiet)."""
    rows: list[Row] = []
    for build in (
        zscore_alerts,
        volume_alerts,
        crosscheck_alerts,
        source_down_alerts,
        stale_alerts,
    ):
        rows += build(duck, ctx)
    return to_frame("alerts", rows) if rows else empty_frame("alerts")


def alerts_step(ctx: RunContext) -> None:
    """Hook: build this run's alerts and append them once (P-008); a repeat call is a no-op."""
    log = get_logger(run_id=ctx.run_id)
    duck = connect(ctx.store, ctx.config)
    try:
        frame = build_alerts(ctx, duck)
    finally:
        duck.close()
    if frame.empty:
        log.info("alerts.none")
        return
    try:
        Datasets(ctx.store).append("alerts", frame, ctx, ctx.target_date)
    except KeyExistsError:
        log.warning("alerts.already_written")
        return
    log.info("alerts.written", n=len(frame))


def with_alerts(next_hook: Callable[[RunContext], None]) -> Callable[[RunContext], None]:
    """``next_hook`` preceded by the alerts step; a failing alerts step never blocks it."""

    @functools.wraps(next_hook)  # `inspect.unwrap(hook)` is the wrapped hook
    def hook(ctx: RunContext) -> None:
        error: Exception | None = None
        try:
            alerts_step(ctx)
        except Exception as exc:  # isolate: serving must still run
            error = exc
        next_hook(ctx)
        if error is not None:
            raise RuntimeError(f"alerts: {type(error).__name__}: {error}") from error

    return hook


__all__ = ["alerts_step", "build_alerts", "with_alerts"]
