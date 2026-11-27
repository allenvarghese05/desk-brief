"""Shared helpers for the T65 live probes (owner-run; never in CI or tests).

Each probe builds the real source from ``config.yaml`` with that source switched on in memory
(the file is not changed), runs plan -> fetch -> parse_table through the project's own HTTP
client (rate limit, retries, secret handling), and prints only the HTTP status, the fetch
status, the row count and the newest date. It never prints a key, a User-Agent, a URL with
parameters or any provider value.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from deskbrief.config import Config, load_config
from deskbrief.models import JobKind, RunContext, Trigger
from deskbrief.sources.http import DefaultHttpClient
from deskbrief.sources.tables import TableSource
from deskbrief.timeutil import et_date

ROOT = Path(__file__).resolve().parents[2]


def probe_config(source: str, update: Callable[[Config], Config] | None = None) -> Config:
    cfg = load_config(ROOT / "config.yaml")
    sources = dict(cfg.sources)
    sources[source] = sources[source].model_copy(update={"enabled": True})
    cfg = cfg.model_copy(update={"sources": sources})
    return update(cfg) if update else cfg


def morning_context(cfg: Config) -> RunContext:
    now = datetime.now(UTC)
    return RunContext(
        run_id="probe-t65",
        job=JobKind.MORNING,
        trigger=Trigger.MANUAL,
        target_date=et_date(now),
        now_utc=now,
        git_sha="probe",
        config_hash="probe",
        config=cfg,
        store=None,  # type: ignore[arg-type]  # plan() never touches the store
    )


def run(source: TableSource, cfg: Config, datasets: Iterable[str], date_col: str) -> int:
    """Fetch and parse every planned request of ``datasets``; print one line each."""
    failures = 0
    requests = [
        r for r in source.plan(JobKind.MORNING, morning_context(cfg)) if r.dataset in datasets
    ]
    if not requests:
        print("nothing planned: check the config section this source reads")
        return 1
    with DefaultHttpClient(cfg.sources) as http:
        for req in requests:
            raw = source.fetch(req, http)
            label = f"{req.source}/{req.dataset} {','.join(req.symbols)}".strip()
            if not raw.ok:
                failures += 1
                print(f"{label}: http={raw.http_status} status={raw.fetch_status.value} (failed)")
                continue
            try:
                rows: pd.DataFrame = source.parse_table(raw)
            except Exception as exc:  # report the class only, never the payload
                failures += 1
                print(f"{label}: http={raw.http_status} parse failed ({type(exc).__name__})")
                continue
            newest = max(rows[date_col]) if not rows.empty else None
            print(
                f"{label}: http={raw.http_status} status={raw.fetch_status.value} "
                f"rows={len(rows)} newest_{date_col}={newest} bytes={len(raw.body)}"
            )
    return 1 if failures else 0
