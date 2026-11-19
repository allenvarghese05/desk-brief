"""Command-line entry point (card T19).

::

    deskbrief run {morning|intraday|eod|backfill|rebuild} [--date D] [--end-date D]
                  [--dry-run] [--sources a,b] [--config PATH]
    deskbrief history [--start D] [--end D] [--dry-run] [--sources a,b] [--config PATH]
    deskbrief config validate [PATH]
    deskbrief --version

Exit codes: 0 = ok, degraded or skipped; 1 = failed (no source succeeded or storage
unavailable); 2 = usage or configuration error.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from datetime import date, timedelta
from pathlib import Path

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
    _parser().print_usage(sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
