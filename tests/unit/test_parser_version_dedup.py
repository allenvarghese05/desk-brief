"""A re-parsed row replaces its first parse everywhere it is read (card T55, PR #78 finding 2).

A Cboe VIX close is written at ``parser_version`` 1, then the same raw file is re-parsed at
version 2 (same natural key, same ``fetch_id``, later ``ingested_at``, corrected value). Every
reader must show exactly one value, the re-parsed one: ``obs_latest``, the query snapshot,
``BriefModel`` and the dashboard's ``build_bars``.

SYNTHETIC FIXTURE: values and fetch ids are invented. Sessions Thu 12 and Fri 13 Nov 2026;
the report runs Monday 16 Nov 07:00 ET.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from deskbrief.config import Config, load_config
from deskbrief.query import latest_snapshot
from deskbrief.report.model import BriefModel
from deskbrief.store.duck import connect
from deskbrief.store.local import LocalBackend
from deskbrief.timeutil import et_to_utc
from weekly_synth import row, write

REPO = Path(__file__).resolve().parents[2]
THU, FRI, MON = date(2026, 11, 12), date(2026, 11, 13), date(2026, 11, 16)
AT = et_to_utc(MON, "07:00")
V1, V2 = 18.40, 18.25  # first parse, corrected re-parse of the same Friday close
CBOE = {"source": "cboe", "dataset": "vix_history", "symbol": "VIX"}


def vix(d: date, value: float, *, version: str, run_id: str, ingested_hhmm: str) -> dict:  # type: ignore[type-arg]
    r = row(
        "VIX", d, value, field="close", unit="index_pts", interval="1d",
        ingested=et_to_utc(d, ingested_hhmm), run_id=run_id, **CBOE,
    )  # fmt: skip
    return {**r, "parser_version": version, "fetch_id": f"F-cboe-VIX-{d:%Y%m%d}"}


@pytest.fixture(scope="module")
def config() -> Config:
    return load_config(REPO / "config.yaml")


@pytest.fixture
def store(tmp_path: Path, config: Config) -> LocalBackend:
    s = LocalBackend(tmp_path / "data")
    write(s, config, [
        vix(THU, 18.00, version="1", run_id="R1", ingested_hhmm="20:10"),
        vix(FRI, V1, version="1", run_id="R2", ingested_hhmm="20:10"),
        vix(FRI, V2, version="2", run_id="R3", ingested_hhmm="21:30"),  # the re-parse
    ])  # fmt: skip
    return s


def test_obs_latest_keeps_only_the_reparsed_row(store: LocalBackend, config: Config) -> None:
    duck = connect(store, config)
    stored = duck.execute(
        "SELECT count(*) FROM obs_all WHERE instrument_id = 'VIX' AND field = 'close'"
    ).fetchone()
    assert stored == (3,)  # both versions are on disk (write-once)
    rows = duck.execute(
        "SELECT value, parser_version FROM obs_latest "
        "WHERE instrument_id = 'VIX' AND field = 'close' "
        "AND CAST(timezone('America/New_York', as_of) AS DATE) = DATE '2026-11-13'"
    ).fetchall()
    assert rows == [(V2, "2")]


def test_query_snapshot_has_one_vix_value(store: LocalBackend, config: Config) -> None:
    snap = [v for v in latest_snapshot(connect(store, config), AT) if v.instrument_id == "VIX"]
    assert len(snap) == 1
    assert snap[0].value == V2


def test_brief_model_shows_the_reparsed_value(store: LocalBackend, config: Config) -> None:
    model = BriefModel.from_store(connect(store, config), config, MON, AT)
    (vix_row,) = [r for r in model.rows() if r.instrument_id == "VIX"]
    assert vix_row.available
    assert vix_row.value == V2


def test_build_bars_has_one_bar_per_session(
    store: LocalBackend, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.syspath_prepend(str(REPO))  # `app` is a plain folder, not an installed package
    from app.components.data import build_bars

    bars = build_bars(connect(store, config), AT, "VIX", frozenset())
    assert [d.date() for d in bars["d"]] == [THU, FRI]
    assert bars.iloc[-1]["close"] == pytest.approx(V2)
