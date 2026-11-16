"""Command-line entry point (card T19).

::

    deskbrief run {morning|intraday|eod|backfill|rebuild} [--date D] [--end-date D]
                  [--dry-run] [--sources a,b] [--config PATH]
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
from datetime import date
from pathlib import Path

from deskbrief import __version__
from deskbrief.calendars import business_days
from deskbrief.config import load_config
from deskbrief.config import main as config_main
from deskbrief.logging import configure_logging, get_logger
from deskbrief.models import JobKind
from deskbrief.pipeline.jobs import build_http, build_store, parse_sources_arg
from deskbrief.pipeline.manifest import RunManifest
from deskbrief.pipeline.runner import RunEnv, run_job, run_rebuild
from deskbrief.sources import catalog
from deskbrief.sources.registry import get_sources


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
    cfg = sub.add_parser("config", help="configuration tools")
    cfg_sub = cfg.add_subparsers(dest="config_command", required=True)
    val = cfg_sub.add_parser("validate", help="validate config.yaml")
    val.add_argument("path", nargs="?", type=Path)
    return p


def _business_days(start: date, end: date) -> list[date]:
    """XNYS business days in ``[start, end]``. One run per day: the source contract plans from a
    single ``target_date``, so month-chunked flushing is not possible without a contract change."""
    return business_days("XNYS", start, end)


def _cmd_run(args: argparse.Namespace) -> int:
    configure_logging()
    log = get_logger()
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

    catalog.load_all()
    wanted = parse_sources_arg(args.sources)
    sources = get_sources(config)
    if wanted is not None:
        unknown = wanted - {s.name for s in sources}
        if unknown:
            print(f"unknown or disabled sources: {sorted(unknown)}", file=sys.stderr)
            return 2
        sources = [s for s in sources if s.name in wanted]

    try:
        store = build_store(config)
    except Exception as exc:  # e.g. missing R2 credentials: storage unavailable
        log.error("storage.unavailable", error=f"{type(exc).__name__}")
        print(f"storage unavailable: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    env = RunEnv(
        config=config,
        store=store,
        http=build_http(config, store),
        sources=sources,
        git_sha=os.environ.get("DESKBRIEF_GIT_SHA") or os.environ.get("GITHUB_SHA") or "local",
        env=os.environ,
    )
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


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(sys.argv[1:] if argv is None else list(argv))
    if args.version:
        print(__version__)
        return 0
    if args.command == "run":
        return _cmd_run(args)
    if args.command == "config":
        return config_main([str(args.path)] if args.path else [])
    _parser().print_usage(sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
