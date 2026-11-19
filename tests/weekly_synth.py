"""Builders for the weekly-cadence tests of card T45 (importable as ``weekly_synth``).

SYNTHETIC FIXTURE: every instrument, symbol, value and timestamp here is made up. The three
instruments and two schedules stand in for what cards T46 (FRED NFCI, broad dollar) and T49
(CFTC positioning) will add to ``config.yaml``; nothing is read from a provider.

Calendar used by the tests (November 2026): CFTC-like releases on Fridays at 15:30 ET with
data as of the Tuesday (``lag_bdays`` 3); Thanksgiving (Thu 26 Nov) delays the 27 Nov release.
NFCI-like releases on Wednesdays at 08:30 ET with data as of the Friday before.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from datetime import UTC, date, datetime
from typing import Any

from brief_synth import REPO_ROOT
from config_support import enable_all_sources
from deskbrief.config import Config, load_config
from deskbrief.models import JobKind, RunContext, Trigger
from deskbrief.schemas import to_frame
from deskbrief.store.datasets import Datasets
from deskbrief.store.local import LocalBackend
from deskbrief.timeutil import et_date, et_to_utc

COT = {"source": "cftc", "dataset": "cot"}
NFCI_FEED = {"source": "fred", "dataset": "series_weekly", "symbol": "NFCI"}
BROAD_FEED = {"source": "fred", "dataset": "series", "symbol": "DTWEXBGS"}

_SCHEDULES = [
    {**COT, "expected_by_et": "15:30", "lag_bdays": 3, "calendar": "SIFMA_US",
     "grace_minutes": 60, "release_weekday": 4},
    {"source": "fred", "dataset": "series_weekly", "expected_by_et": "08:30", "lag_bdays": 3,
     "calendar": "SIFMA_US", "grace_minutes": 60, "release_weekday": 2},
]  # fmt: skip

_INSTRUMENTS = [
    {"id": "GC_NET", "name": "Gold futures positioning", "asset_class": "commodity",
     "kind": "macro", "unit": "contracts",
     "feeds": [{**COT, "symbol": "SYN_GC_NET", "tier": 3}]},
    {"id": "GC_OI", "name": "Gold futures interest", "asset_class": "commodity",
     "kind": "macro", "unit": "contracts",
     "feeds": [{**COT, "symbol": "SYN_GC_OI", "tier": 3}]},
    {"id": "NFCI", "name": "Financial conditions", "asset_class": "macro", "kind": "macro",
     "unit": "index", "feeds": [{**NFCI_FEED, "tier": 3}]},
    {"id": "USD_BROAD", "name": "Broad dollar", "asset_class": "fx", "kind": "index",
     "unit": "index_pts", "feeds": [{**BROAD_FEED, "tier": 3}]},
]  # fmt: skip


def weekly_config() -> Config:
    """``config.yaml`` (all sources on) plus the synthetic weekly schedules and instruments.

    The synthetic entries replace any real ones with the same key (T46 added the real NFCI
    schedule and the NFCI and broad-dollar instruments; T49 the real CFTC schedule and the six
    ``COT_*`` instruments on ``cftc/cot``), so these tests keep their own timings.
    """
    base = enable_all_sources(load_config(REPO_ROOT / "config.yaml"))
    raw = base.model_dump(mode="json")
    raw["sources"]["cftc"] = {"rate_per_min": 10, "timeout_s": 15, "retries": 2}
    keys = {(s["source"], s["dataset"]) for s in _SCHEDULES}
    ids = {i["id"] for i in _INSTRUMENTS}
    symbols = {(f["source"], f["symbol"]) for i in _INSTRUMENTS for f in i["feeds"]}
    raw["datasets"] = [
        *(d for d in raw["datasets"] if (d["source"], d["dataset"]) not in keys),
        *_SCHEDULES,
    ]
    raw["instruments"] = [
        *(
            i
            for i in raw["instruments"]
            if i["id"] not in ids
            and not any((f["source"], f["symbol"]) in symbols for f in i["feeds"])
            # T49's real COT instruments share the synthetic cftc/cot dataset
            and not any(
                (f["source"], f["dataset"]) == (COT["source"], COT["dataset"]) for f in i["feeds"]
            )
        ),
        *_INSTRUMENTS,
    ]
    return Config.model_validate(raw)


def row(
    instrument_id: str,
    d: date,
    value: float,
    *,
    field: str,
    unit: str,
    interval: str,
    ingested: datetime,
    run_id: str = "R1",
    **feed: Any,
) -> dict[str, Any]:
    """SYNTHETIC FIXTURE: one observation dated 00:00 ET on ``d`` (the 1d and 1w rule)."""
    return {
        "instrument_id": instrument_id,
        "tier": 3,
        "field": field,
        "interval": interval,
        "as_of": et_to_utc(d, "00:00"),
        "value": value,
        "unit": unit,
        "is_derived": False,
        "is_fallback": False,
        "fetch_id": f"F-{instrument_id}-{d.isoformat()}-{field}",
        "run_id": run_id,
        "ingested_at": ingested,
        "parser_version": "synthetic-1",
        **feed,
    }


def net(d: date, value: float, ingested: datetime, **kw: Any) -> dict[str, Any]:
    return row("GC_NET", d, value, field="net_position", unit="contracts", interval="1w",
               ingested=ingested, **COT, symbol="SYN_GC_NET", **kw)  # fmt: skip


def oi(d: date, value: float, ingested: datetime, **kw: Any) -> dict[str, Any]:
    return row("GC_OI", d, value, field="open_interest", unit="contracts", interval="1w",
               ingested=ingested, **COT, symbol="SYN_GC_OI", **kw)  # fmt: skip


def nfci(d: date, value: float, ingested: datetime, **kw: Any) -> dict[str, Any]:
    return row("NFCI", d, value, field="level", unit="index", interval="1w",
               ingested=ingested, **NFCI_FEED, **kw)  # fmt: skip


def broad(d: date, value: float, ingested: datetime, **kw: Any) -> dict[str, Any]:
    return row("USD_BROAD", d, value, field="level", unit="index_pts", interval="1d",
               ingested=ingested, **BROAD_FEED, **kw)  # fmt: skip


def ctx_at(config: Config, store: LocalBackend, now: datetime, *, run_id: str = "R1") -> RunContext:
    return RunContext(
        run_id=run_id, job=JobKind.EOD, trigger=Trigger.MANUAL, target_date=et_date(now),
        now_utc=now, git_sha="deadbeef", config_hash="cafe", config=config, store=store,
    )  # fmt: skip


def write(store: LocalBackend, config: Config, rows: list[dict[str, Any]]) -> None:
    """Append rows, one file per (run_id, ET date of as_of), like the runner's flush."""
    groups: dict[tuple[str, date], list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        groups[(r["run_id"], et_date(r["as_of"]))].append(r)
    for (run_id, part), chunk in groups.items():
        ctx = ctx_at(config, store, datetime(2026, 1, 1, tzinfo=UTC), run_id=run_id)
        Datasets(store).append("observations", to_frame("observations", chunk), replace(ctx), part)
