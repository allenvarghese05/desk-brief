"""DuckDB views over Parquet (spec §2.3, §7.3): obs_latest, obs_pit, daily_close, empties, dims.

SYNTHETIC FIXTURE: every observation below is made up; values are chosen so a wrong view
returns a visibly wrong number.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb
import pytest
from store_fakes import FakeS3, make_ctx, obs_row

from deskbrief.config import Config
from deskbrief.schemas import columns, empty_frame, to_frame
from deskbrief.store import duck
from deskbrief.store.datasets import Datasets
from deskbrief.store.duck import connect
from deskbrief.store.local import LocalBackend
from deskbrief.store.r2 import R2Backend

D = date(2026, 11, 13)
AS_OF = datetime(2026, 11, 13, 5, 0, tzinfo=UTC)  # 00:00 ET (EST) on 2026-11-13
T0 = datetime(2026, 11, 13, 22, 0, tzinfo=UTC)


def write_obs(
    store: LocalBackend, config: Config, run_id: str, rows: list[dict[str, Any]], part: date = D
) -> None:
    Datasets(store).append(
        "observations",
        to_frame("observations", rows),
        make_ctx(config, store, run_id=run_id),
        part,
    )


def rows_of(conn: duckdb.DuckDBPyConnection, sql: str) -> list[tuple[Any, ...]]:
    return conn.execute(sql).fetchall()


# ---------------------------------------------------------------------------
# obs_latest / obs_pit on a multi-version dataset
# ---------------------------------------------------------------------------


@pytest.fixture
def versioned(local: LocalBackend, config: Config) -> LocalBackend:
    """K1 has three versions (ingested T0, T0+10h, T0+14h), written newest-first on purpose.
    K2 has one version. K3 has two versions with the *same* ingested_at (tie -> higher fetch_id).
    """
    k1 = {"instrument_id": "SPY", "source": "stooq"}
    k2 = {"instrument_id": "QQQ", "symbol": "qqq.us"}
    k3 = {"instrument_id": "IWM", "symbol": "iwm.us"}
    write_obs(
        local,
        config,
        "R3",
        [obs_row(**k1, value=102.0, fetch_id="F3", ingested_at=T0 + timedelta(hours=14))],
    )
    write_obs(
        local,
        config,
        "R1",
        [
            obs_row(**k1, value=100.0, fetch_id="F1", ingested_at=T0),
            obs_row(**k2, value=200.0, fetch_id="F1"),
            obs_row(**k3, value=10.0, fetch_id="FA"),
        ],
    )
    write_obs(
        local,
        config,
        "R2",
        [
            obs_row(**k1, value=101.0, fetch_id="F2", ingested_at=T0 + timedelta(hours=10)),
            obs_row(**k3, value=20.0, fetch_id="FB"),
        ],
    )
    return local


def test_obs_all_keeps_every_version(versioned: LocalBackend, config: Config) -> None:
    conn = connect(versioned, config)
    assert rows_of(conn, "SELECT count(*) FROM obs_all") == [(6,)]
    assert rows_of(conn, "SELECT count(*) FROM obs_all WHERE instrument_id='SPY'") == [(3,)]


def test_obs_latest_one_row_per_natural_key(versioned: LocalBackend, config: Config) -> None:
    conn = connect(versioned, config)
    got = rows_of(conn, "SELECT instrument_id, value, fetch_id FROM obs_latest ORDER BY 1")
    assert got == [
        ("IWM", 20.0, "FB"),  # tie on ingested_at -> highest fetch_id
        ("QQQ", 200.0, "F1"),
        ("SPY", 102.0, "F3"),  # latest of three versions, though written first
    ]


@pytest.mark.parametrize(
    ("t_offset_h", "expected_spy", "expected_iwm"),
    [
        pytest.param(-1, None, None, id="before-anything-was-ingested"),
        pytest.param(0, 100.0, 20.0, id="exactly-at-first-ingest-is-inclusive"),
        pytest.param(5, 100.0, 20.0, id="between-v1-and-v2"),
        pytest.param(10, 101.0, 20.0, id="exactly-at-v2"),
        pytest.param(13.9, 101.0, 20.0, id="just-before-v3"),
        pytest.param(14, 102.0, 20.0, id="exactly-at-v3"),
        pytest.param(500, 102.0, 20.0, id="long-after"),
    ],
)
def test_obs_pit_reproduces_what_was_known_at_t(
    versioned: LocalBackend,
    config: Config,
    t_offset_h: float,
    expected_spy: float | None,
    expected_iwm: float | None,
) -> None:
    conn = connect(versioned, config)
    t = T0 + timedelta(hours=t_offset_h)
    got = dict(
        conn.execute(
            "SELECT instrument_id, value FROM obs_pit(?::TIMESTAMPTZ) ORDER BY 1", [t]
        ).fetchall()
    )
    assert got.get("SPY") == expected_spy
    assert got.get("IWM") == expected_iwm
    # QQQ has a single version ingested at T0
    assert got.get("QQQ") == (200.0 if t_offset_h >= 0 else None)


def test_obs_pit_accepts_a_sql_literal(versioned: LocalBackend, config: Config) -> None:
    conn = connect(versioned, config)
    got = rows_of(
        conn,
        "SELECT value FROM obs_pit(TIMESTAMPTZ '2026-11-14 04:00:00+00') WHERE instrument_id='SPY'",
    )
    assert got == [(100.0,)]  # 2026-11-14 04:00Z is 6h after T0, before the 10h version


# ---------------------------------------------------------------------------
# daily_close tier preference
# ---------------------------------------------------------------------------


def _close(tier: int, source: str, value: float, **over: Any) -> dict[str, Any]:
    return obs_row(
        instrument_id="SPY",
        tier=tier,
        source=source,
        symbol=source,
        dataset="daily",
        value=value,
        fetch_id=f"F-{source}-{value}",
        **over,
    )


@pytest.mark.parametrize(
    ("rows", "expected_source", "expected_close", "expected_tier"),
    [
        pytest.param(
            [_close(1, "alpaca", 1.0), _close(2, "yahoo", 2.0), _close(3, "stooq", 3.0)],
            "stooq",
            3.0,
            3,
            id="tier3-beats-2-and-1",
        ),
        pytest.param(
            [_close(1, "alpaca", 1.0), _close(2, "yahoo", 2.0)],
            "yahoo",
            2.0,
            2,
            id="tier2-beats-1",
        ),
        pytest.param([_close(1, "alpaca", 1.0)], "alpaca", 1.0, 1, id="only-tier1"),
        pytest.param(
            [_close(3, "stooq", 3.0), _close(3, "stooq", 3.5, ingested_at=T0 + timedelta(hours=9))],
            "stooq",
            3.5,
            3,
            id="revised-tier3-uses-latest-version",
        ),
        pytest.param(
            [_close(2, "yahoo", 2.0), _close(2, "twelvedata", 2.1)],
            "twelvedata",
            2.1,
            2,
            id="same-tier-ties-break-by-source-name",
        ),
        pytest.param(
            [
                obs_row(instrument_id="SPY", tier=3, source="stooq", field="open", value=99.0),
                _close(1, "alpaca", 1.0),
            ],
            "alpaca",
            1.0,
            1,
            id="non-close-fields-ignored",
        ),
        pytest.param(
            [
                obs_row(instrument_id="SPY", tier=3, source="stooq", interval="1m", value=99.0),
                _close(2, "yahoo", 2.0),
            ],
            "yahoo",
            2.0,
            2,
            id="intraday-bars-ignored",
        ),
    ],
)
def test_daily_close_prefers_highest_tier(
    local: LocalBackend,
    config: Config,
    rows: list[dict[str, Any]],
    expected_source: str,
    expected_close: float,
    expected_tier: int,
) -> None:
    for i, row in enumerate(rows):  # separate runs, as in production
        write_obs(local, config, f"R{i}", [row])
    conn = connect(local, config)
    got = rows_of(conn, "SELECT instrument_id, date, close, tier, chosen_source FROM daily_close")
    assert got == [("SPY", D, expected_close, expected_tier, expected_source)]


def test_daily_close_one_row_per_instrument_and_et_date(
    local: LocalBackend, config: Config
) -> None:
    summer = datetime(2026, 7, 15, 4, 0, tzinfo=UTC)  # 00:00 ET (EDT) on 2026-07-15
    write_obs(
        local,
        config,
        "R1",
        [
            _close(3, "stooq", 3.0),  # 2026-11-13, EST
            obs_row(
                instrument_id="UST_10Y",
                source="treasury",
                dataset="par_curve",
                tier=3,
                field="yield",
                unit="pct",
                value=4.25,
                symbol="10Y",
            ),
            obs_row(
                instrument_id="UST_10Y",
                source="fred",
                dataset="series",
                tier=2,
                field="yield",
                unit="pct",
                value=4.20,
                symbol="DGS10",
            ),
        ],
    )
    # its own partition: observations are partitioned by et_date(as_of) (P-006)
    write_obs(
        local, config, "R2", [_close(3, "stooq", 4.0, as_of=summer)], part=date(2026, 7, 15)
    )  # 2026-07-15, EDT
    conn = connect(local, config)
    got = rows_of(
        conn, "SELECT instrument_id, date, close, chosen_source FROM daily_close ORDER BY 1, 2"
    )
    assert got == [
        ("SPY", date(2026, 7, 15), 4.0, "stooq"),
        ("SPY", D, 3.0, "stooq"),
        ("UST_10Y", D, 4.25, "treasury"),
    ]


# ---------------------------------------------------------------------------
# empty store, types, plain views, dims
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("view", "table"),
    [
        ("obs_all", "observations"),
        ("obs_latest", "observations"),
        ("source_log", "source_log"),
        ("dq", "dq_results"),
        ("xchecks", "crosscheck_results"),
        ("runs", "runs"),
        ("scorecard", "scorecard_daily"),
    ],
)
def test_views_exist_and_are_empty_with_right_columns_on_fresh_store(
    local: LocalBackend, config: Config, view: str, table: str
) -> None:
    conn = connect(local, config)
    cur = conn.execute(f"SELECT * FROM {view}")
    assert tuple(d[0] for d in cur.description) == columns(table)
    assert cur.fetchall() == []


def test_derived_views_empty_on_fresh_store(local: LocalBackend, config: Config) -> None:
    conn = connect(local, config)
    assert rows_of(conn, "SELECT * FROM daily_close") == []
    assert rows_of(conn, "SELECT * FROM obs_pit(TIMESTAMPTZ '2030-01-01 00:00:00+00')") == []
    cur = conn.execute("SELECT * FROM daily_close")
    assert "chosen_source" in [d[0] for d in cur.description]


def test_empty_and_populated_files_union_cleanly(local: LocalBackend, config: Config) -> None:
    ds = Datasets(local)
    ds.append("observations", empty_frame("observations"), make_ctx(config, local, run_id="E"), D)
    write_obs(local, config, "R1", [obs_row(value=7.0)])
    conn = connect(local, config)
    assert rows_of(conn, "SELECT value FROM obs_all") == [(7.0,)]


def test_timestamps_are_utc_instants(versioned: LocalBackend, config: Config) -> None:
    conn = connect(versioned, config)
    (as_of,) = rows_of(conn, "SELECT as_of FROM obs_latest WHERE instrument_id='QQQ'")[0]
    assert as_of == AS_OF
    assert as_of.utcoffset() == timedelta(0)


def test_plain_views_read_their_tables(local: LocalBackend, config: Config) -> None:
    ds = Datasets(local)
    log = to_frame(
        "source_log",
        [
            {
                "fetch_id": "F1",
                "run_id": "R1",
                "source": "yahoo",
                "dataset": "quote",
                "symbols": "SPY",
                "request_redacted": "GET q",
                "requested_at": T0,
                "responded_at": T0 + timedelta(milliseconds=120),
                "latency_ms": 120,
                "status": "ok",
                "bytes": 10,
                "rows_parsed": 1,
                "rows_quarantined": 0,
                "attempt": 1,
            }
        ],
    )
    ds.append("source_log", log, make_ctx(config, local, run_id="R1"), D)
    card = to_frame(
        "scorecard_daily",
        [
            {
                "source": "yahoo",
                "date": D,
                "fetches": 3,
                "ok_rate": 1.0,
                "error_rate": 0.0,
                "n_crosschecks": 0,
            }
        ],
    )
    ds.append("scorecard_daily", card, make_ctx(config, local), D)
    conn = connect(local, config)
    assert rows_of(conn, "SELECT source, latency_ms, http_status FROM source_log") == [
        ("yahoo", 120, None)
    ]
    assert rows_of(conn, "SELECT source, date, fetches FROM scorecard") == [("yahoo", D, 3)]


def test_reads_the_current_clean_version(local: LocalBackend, config: Config) -> None:
    write_obs(local, config, "R1", [obs_row(value=1.0)])
    local.put_bytes("clean/_CURRENT", b"v2")
    assert rows_of(connect(local, config), "SELECT * FROM obs_all") == []
    write_obs(local, config, "R2", [obs_row(value=2.0)])
    assert rows_of(connect(local, config), "SELECT value FROM obs_all") == [(2.0,)]


def test_dims_built_from_config(local: LocalBackend, config: Config) -> None:
    conn = connect(local, config)
    assert rows_of(conn, "SELECT count(*) FROM dim_instrument") == [(len(config.instruments),)]
    n_feeds = sum(len(i.feeds) for i in config.instruments)
    assert rows_of(conn, "SELECT count(*) FROM dim_feed") == [(n_feeds,)]
    assert rows_of(conn, "SELECT count(*) FROM dim_group") == [(len(config.groups),)]
    spy = config.instruments[0]
    assert rows_of(
        conn, f"SELECT name, is_proxy, label FROM dim_instrument WHERE instrument_id='{spy.id}'"
    ) == [(spy.name, spy.is_proxy, spy.label)]
    assert rows_of(
        conn,
        f"SELECT min(tier), max(tier) FROM dim_feed WHERE instrument_id='{spy.id}'",
    ) == [(min(f.tier for f in spy.feeds), max(f.tier for f in spy.feeds))]
    group = config.groups[0]
    assert rows_of(
        conn,
        f"SELECT relation, warn_bps, fail_bps FROM dim_group WHERE group_id='{group.id}'",
    ) == [(group.relation, group.tolerance.warn_bps, group.tolerance.fail_bps)]


def test_dims_join_to_observations(versioned: LocalBackend, config: Config) -> None:
    conn = connect(versioned, config)
    got = rows_of(
        conn,
        "SELECT i.name, count(*) FROM obs_latest o JOIN dim_instrument i USING (instrument_id) "
        "GROUP BY 1 ORDER BY 1",
    )
    assert len(got) == 3


# ---------------------------------------------------------------------------
# connect() inputs
# ---------------------------------------------------------------------------


def test_accepts_a_plain_directory(tmp_path: Path, config: Config) -> None:
    store = LocalBackend(tmp_path / "root")
    write_obs(store, config, "R1", [obs_row(value=5.0)])
    for root in (tmp_path / "root", str(tmp_path / "root")):
        assert rows_of(connect(root, config), "SELECT value FROM obs_all") == [(5.0,)]


def test_path_with_quote_is_escaped(tmp_path: Path, config: Config) -> None:
    store = LocalBackend(tmp_path / "it's")
    write_obs(store, config, "R1", [obs_row(value=6.0)])
    assert rows_of(connect(store, config), "SELECT value FROM obs_all") == [(6.0,)]


def test_rejects_unsupported_backend(config: Config) -> None:
    class Weird:
        def list(self, prefix: str) -> list[str]:
            return []

    with pytest.raises(TypeError, match="LocalBackend and R2Backend"):
        connect(Weird(), config)  # type: ignore[arg-type]


def test_r2_connect_configures_httpfs_secret_and_uses_r2_paths(
    fake_s3: FakeS3, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[dict[str, str]] = []
    monkeypatch.setattr(duck, "_configure_r2", lambda _conn, env: seen.append(dict(env)))
    env = {"R2_ACCOUNT_ID": "acct", "R2_ACCESS_KEY_ID": "k", "R2_SECRET_ACCESS_KEY": "s"}
    conn = connect(R2Backend("bkt", fake_s3), config, env=env)
    assert seen == [env]
    assert rows_of(conn, "SELECT * FROM obs_all") == []  # empty bucket -> empty typed views


def test_r2_secret_sql_shape_and_quoting() -> None:
    from deskbrief.store.r2 import R2Credentials

    sql = duck.r2_secret_sql(R2Credentials("acct", "key'id", "sec"))
    assert sql.startswith("CREATE OR REPLACE TEMPORARY SECRET")
    assert "TYPE r2" in sql
    assert "KEY_ID 'key''id'" in sql
    assert "ACCOUNT_ID 'acct'" in sql


def test_configure_r2_runs_install_load_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    class Rec:
        def __init__(self) -> None:
            self.sql: list[str] = []

        def execute(self, q: str) -> None:
            self.sql.append(q)

    rec = Rec()
    env = {"R2_ACCOUNT_ID": "a", "R2_ACCESS_KEY_ID": "k", "R2_SECRET_ACCESS_KEY": "s"}
    duck._configure_r2(rec, env)  # type: ignore[arg-type]
    assert rec.sql[:2] == ["INSTALL httpfs", "LOAD httpfs"]
    assert "TYPE r2" in rec.sql[2]


def test_table_source_sql_uses_r2_glob_when_files_exist() -> None:
    sql = duck.table_source_sql("observations", "r2://bkt", "v1", has_files=True)
    assert "read_parquet('r2://bkt/clean/v1/observations/date=*/run=*.parquet'" in sql
    assert "hive_partitioning = false" in sql
    card = duck.table_source_sql("scorecard_daily", "r2://bkt", "v1", has_files=True)
    assert "r2://bkt/derived/scorecard_daily/date=*.parquet" in card
