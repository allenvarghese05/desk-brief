"""Command-line entry point (card T19).

::

    deskbrief run {morning|intraday|eod|backfill|rebuild} [--date D] [--end-date D]
                  [--dry-run] [--sources a,b] [--config PATH]
    deskbrief history [--start D] [--end D] [--dry-run] [--sources a,b] [--config PATH]
    deskbrief config validate [PATH]
    deskbrief diagnose changes --date D [--time HH:MM] [--no-values] [--config PATH]
    deskbrief --version

Exit codes: 0 = ok, degraded or skipped; 1 = failed (no source succeeded or storage
unavailable); 2 = usage or configuration error.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from deskbrief import __version__
from deskbrief.calendars import business_days
from deskbrief.config import Config, load_config
from deskbrief.config import main as config_main
from deskbrief.logging import configure_logging, get_logger
from deskbrief.models import JobKind
from deskbrief.pipeline.history import DEFAULT_YEARS, default_range, run_history
from deskbrief.pipeline.jobs import build_http, build_store, parse_sources_arg
from deskbrief.pipeline.manifest import RunManifest
from deskbrief.pipeline.runner import RunEnv, run_job, run_rebuild
from deskbrief.sources import catalog
from deskbrief.sources.base import Source
from deskbrief.sources.registry import get_sources
from deskbrief.timeutil import et_date, utc_now

if TYPE_CHECKING:
    import duckdb


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="deskbrief")
    p.add_argument("--version", "-V", action="store_true", help="print the version and exit")
    sub = p.add_subparsers(dest="command")
    run = sub.add_parser("run", help="run a pipeline job")
    run.add_argument("job", choices=[j.value for j in JobKind])
    run.add_argument("--date", type=date.fromisoformat, help="target date (ET), YYYY-MM-DD")
    run.add_argument(
        "--end-date", type=date.fromisoformat, help="backfill only: last date, inclusive"
    )
    run.add_argument("--dry-run", action="store_true", help="plan only: no fetch, no writes")
    run.add_argument("--sources", help="comma-separated source names (default: all enabled)")
    run.add_argument("--config", type=Path, help="config.yaml path (default: $DESKBRIEF_CONFIG)")
    hist = sub.add_parser(
        "history", help=f"one-time history load (P-020; default: {DEFAULT_YEARS} years)"
    )
    hist.add_argument("--start", type=date.fromisoformat, help="first ET date, YYYY-MM-DD")
    hist.add_argument(
        "--end", type=date.fromisoformat, help="last ET date, inclusive (default: yesterday)"
    )
    hist.add_argument("--dry-run", action="store_true", help="print the plan: no fetch, no writes")
    hist.add_argument("--sources", help="comma-separated source names (default: all enabled)")
    hist.add_argument("--config", type=Path, help="config.yaml path (default: $DESKBRIEF_CONFIG)")
    cfg = sub.add_parser("config", help="configuration tools")
    cfg_sub = cfg.add_subparsers(dest="config_command", required=True)
    val = cfg_sub.add_parser("validate", help="validate config.yaml")
    val.add_argument("path", nargs="?", type=Path)
    diag = sub.add_parser("diagnose", help="read-only diagnostics (write nothing)")
    diag_sub = diag.add_subparsers(dest="diagnose_command", required=True)
    chg = diag_sub.add_parser(
        "changes", help="every instrument's last value and the baseline its change used (T61)"
    )
    chg.add_argument("--date", type=date.fromisoformat, required=True, help="report date (ET)")
    chg.add_argument("--time", default="07:00", help="ET wall-clock cut, HH:MM (default 07:00)")
    chg.add_argument(
        "--no-values", action="store_true", help="hide values and changes (dates, sources only)"
    )
    chg.add_argument("--config", type=Path, help="config.yaml path (default: $DESKBRIEF_CONFIG)")
    return p


def _business_days(start: date, end: date) -> list[date]:
    """XNYS business days in ``[start, end]``. One run per day: the source contract plans from a
    single ``target_date``, so month-chunked flushing is not possible without a contract change."""
    return business_days("XNYS", start, end)


#: ``run`` rejects these jobs: they have their own subcommand (or will, once their card lands).
_NOT_RUN_JOBS: dict[JobKind, str] = {
    JobKind.HISTORY: "history is its own subcommand (card T53)",
}


def _cmd_run(args: argparse.Namespace) -> int:
    rejected = _NOT_RUN_JOBS.get(JobKind(args.job))
    if rejected:
        print(rejected, file=sys.stderr)
        return 2
    configure_logging()
    try:
        config = load_config(args.config)
    except (ValueError, OSError) as exc:
        print(f"config: INVALID\n{exc}", file=sys.stderr)
        return 2
    job = JobKind(args.job)
    if args.end_date and (job is not JobKind.BACKFILL or not args.date):
        print("--end-date needs `run backfill --date START`", file=sys.stderr)
        return 2
    if job is JobKind.BACKFILL and not args.date:
        print("backfill needs --date", file=sys.stderr)
        return 2

    env = _build_env(config, args.sources)
    if isinstance(env, int):
        return env
    manifests: list[RunManifest]
    if job is JobKind.REBUILD:
        manifests = [run_rebuild(env, from_date=args.date, dry_run=args.dry_run)]
    elif job is JobKind.BACKFILL:
        days = _business_days(args.date, args.end_date or args.date)
        manifests = [run_job(env, job, target_date=d, dry_run=args.dry_run) for d in days]
    else:
        manifests = [run_job(env, job, target_date=args.date, dry_run=args.dry_run)]
    for m in manifests:
        print(m.to_markdown())
    return 1 if any(m.exit_code for m in manifests) else 0


def _build_env(config: Config, sources_arg: str | None) -> RunEnv | int:
    """Sources, store and HTTP client for a run; an exit code when that is not possible."""
    catalog.load_all()
    wanted = parse_sources_arg(sources_arg)
    sources: list[Source] = list(get_sources(config))
    if wanted is not None:
        unknown = wanted - {s.name for s in sources}
        if unknown:
            print(f"unknown or disabled sources: {sorted(unknown)}", file=sys.stderr)
            return 2
        sources = [s for s in sources if s.name in wanted]
    try:
        store = build_store(config)
    except Exception as exc:  # e.g. missing R2 credentials: storage unavailable
        get_logger().error("storage.unavailable", error=f"{type(exc).__name__}")
        print(f"storage unavailable: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return RunEnv(
        config=config,
        store=store,
        http=build_http(config, store),
        sources=sources,
        git_sha=os.environ.get("DESKBRIEF_GIT_SHA") or os.environ.get("GITHUB_SHA") or "local",
        env=os.environ,
    )


def _cmd_history(args: argparse.Namespace) -> int:
    """``deskbrief history``: P-020's one-time load, through each source's ``plan_range``."""
    configure_logging()
    today = et_date(utc_now())
    start, end = default_range(today)
    if args.end:
        start, end = default_range(args.end + timedelta(days=1))
    if args.start:
        start = args.start
    if end >= today:
        print(f"--end must be before today ({today} ET)", file=sys.stderr)
        return 2
    if start > end:
        print(f"--start {start} is after --end {end}", file=sys.stderr)
        return 2
    try:
        config = load_config(args.config)
    except (ValueError, OSError) as exc:
        print(f"config: INVALID\n{exc}", file=sys.stderr)
        return 2
    env = _build_env(config, args.sources)
    if isinstance(env, int):
        return env
    result = run_history(env, start=start, end=end, dry_run=args.dry_run)
    print(result.to_markdown())
    return result.exit_code


#: how far back ``diagnose changes`` lists each instrument's stored series
DIAGNOSE_SERIES_DAYS = 14


def _cmd_diagnose_changes(args: argparse.Namespace) -> int:
    """``deskbrief diagnose changes``: read-only view of ``query.overnight_changes`` (T61).

    For every instrument it prints the last value and the baseline the change used (source,
    dataset, field, interval, tier, symbol, ET as-of, value), the basis, status and note, then
    the series stored for it in the last ``DIAGNOSE_SERIES_DAYS`` days (newest ET date per
    source, dataset, field and interval), and finally the moves the brief scored as notable.
    It fetches nothing and writes nothing; no credential, URL or bucket key is printed.
    """
    from deskbrief.timeutil import et_to_utc, parse_hhmm

    try:
        parse_hhmm(args.time)
        config = load_config(args.config)
    except (ValueError, OSError) as exc:
        print(f"diagnose: {exc}", file=sys.stderr)
        return 2
    try:
        store = build_store(config)
        from deskbrief.store import duck as duck_store

        conn = duck_store.connect(store, config)
    except Exception as exc:  # e.g. missing R2 credentials: name the error type only
        print(f"storage unavailable: {type(exc).__name__}", file=sys.stderr)
        return 1
    cut = et_to_utc(args.date, args.time)
    for line in diagnose_changes_lines(conn, config, args.date, cut, values=not args.no_values):
        print(line)
    return 0


def diagnose_changes_lines(
    duck: duckdb.DuckDBPyConnection,
    config: Config,
    report_date: date,
    cut: datetime,
    *,
    values: bool = True,
) -> list[str]:
    """The text ``diagnose changes`` prints (one string per line); see ``_cmd_diagnose_changes``."""
    from deskbrief import query
    from deskbrief.report.model import BriefModel
    from deskbrief.timeutil import to_et

    def when(r: query.ValueRow) -> str:
        et = to_et(r.as_of)
        return f"{et:%Y-%m-%d}" if r.interval in query.DATED_INTERVALS else f"{et:%Y-%m-%d %H:%M}"

    def side(r: query.ValueRow | None) -> str:
        if r is None:
            return "none"
        text = (
            f"{r.source}/{r.dataset} field={r.field} interval={r.interval} tier={r.tier} "
            f"symbol={r.symbol} as_of={when(r)} ET"
        )
        return f"{text} value={r.value:.6g}" if values else text

    changes = query.overnight_changes(duck, report_date, cut)
    stored = _stored_series(duck, cut)
    et_cut = to_et(cut)
    out = [
        f"# deskbrief diagnose changes: report date {report_date}, cut {et_cut:%H:%M} ET "
        f"({cut:%Y-%m-%d %H:%M} UTC), storage {config.storage.backend}",
        "# read-only: nothing fetched, nothing written"
        + ("" if values else "; values and changes hidden (--no-values)"),
    ]
    by_id = {c.last.instrument_id: c for c in changes}
    for inst in config.instruments:
        ch = by_id.get(inst.id)
        out.append("")
        if ch is None:
            out.append(f"{inst.id} [{inst.asset_class}] no value as of the cut")
        else:
            change = "n/a" if ch.change is None else f"{ch.change:+.4g} {ch.change_unit}"
            head = f"{inst.id} [{inst.asset_class}/{ch.family}] status={ch.status} "
            head += f'basis="{ch.basis}"'
            head += f" change={change}" if values else ""
            out.append(head)
            out.append(f"  last      {side(ch.last)}")
            out.append(f"  baseline  {side(ch.previous)}")
            out.append(f"  note      {ch.note or '-'}")
        series = stored.get(inst.id, [])
        out.append("  series    " + ("; ".join(series) if series else "none stored"))
    model = BriefModel.from_store(duck, config, report_date, cut)
    moves = [n for n in model.notable if n.kind == "move"]
    out += ["", f"# notable moves the brief scored ({len(moves)}):"]
    out += [f"  {n.text if values else n.instrument_id}" for n in moves] or ["  none"]
    return out


def _stored_series(duck: duckdb.DuckDBPyConnection, cut: datetime) -> dict[str, list[str]]:
    """Per instrument: ``source/dataset field interval newest=<ET> n=<rows>`` for each series
    with a value field stored in the last ``DIAGNOSE_SERIES_DAYS`` days, point-in-time."""
    from deskbrief import query
    from deskbrief.timeutil import to_et

    fields = ", ".join(f"'{f}'" for f in query.VALUE_FIELDS)
    rows = duck.execute(
        "SELECT instrument_id, source, dataset, field, interval, max(as_of), count(*) "  # noqa: S608
        f"FROM obs_pit(?) WHERE as_of <= ? AND as_of > ? AND field IN ({fields}) "
        "GROUP BY ALL ORDER BY instrument_id, max(as_of) DESC, source, dataset, field, interval",
        [cut, cut, cut - timedelta(days=DIAGNOSE_SERIES_DAYS)],
    ).fetchall()
    out: dict[str, list[str]] = {}
    for iid, source, dataset, field, interval, newest, n in rows:
        et = to_et(newest)
        stamp = f"{et:%Y-%m-%d}" if interval in query.DATED_INTERVALS else f"{et:%Y-%m-%d %H:%M}"
        text = f"{source}/{dataset} {field} {interval} newest={stamp} n={n}"
        out.setdefault(iid, []).append(text)
    return out


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(sys.argv[1:] if argv is None else list(argv))
    if args.version:
        print(__version__)
        return 0
    if args.command == "run":
        return _cmd_run(args)
    if args.command == "history":
        return _cmd_history(args)
    if args.command == "config":
        return config_main([str(args.path)] if args.path else [])
    if args.command == "diagnose":
        return _cmd_diagnose_changes(args)
    _parser().print_usage(sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
