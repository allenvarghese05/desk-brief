"""DuckDB connection with the views of spec §7.3 over the Parquet layout of §6.

``connect(store, config)`` returns an in-memory DuckDB connection. Files are read in place:
local backends through file globs, R2 through ``httpfs`` and a ``CREATE SECRET (TYPE r2, ...)``
built from the ``R2_*`` environment variables.

Views are bound to the files that exist **when you connect**. A table with no files yet still
gets a view, empty and with the declared columns and types, so queries never fail on a fresh
store. Reconnect to see files written afterwards (connecting is cheap).

========================  =====================================================================
``obs_all``               every version of every observation (all ingested rows)
``obs_latest``            latest ``ingested_at`` per natural key (ties: highest ``fetch_id``)
``obs_pit(t)``            macro: latest version with ``ingested_at <= t`` per natural key
``daily_close``           one daily value per instrument and ET date, from ``obs_latest``;
                          prefers tier 3, then 2, then 1; ``chosen_source`` names the winner
``source_log``, ``dq``,   plain unions of the corresponding tables (``dq_results``,
``xchecks``, ``runs``,    ``crosscheck_results``, ``scorecard_daily``)
``scorecard``
``dim_instrument``,       small tables built from ``config.yaml`` (not views, so they carry
``dim_feed``,             no file dependency)
``dim_group``
========================  =====================================================================

``daily_close`` reads ``interval = '1d'`` rows whose field is one of ``DAILY_CLOSE_FIELDS``
(``close`` for prices, ``yield`` for Treasury par yields, ``rate`` for fixes), and exposes the
value as column ``close``; the original ``field`` is kept alongside.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

import duckdb

from deskbrief.config import Config
from deskbrief.schemas import OBS_NATURAL_KEY, TABLES, columns
from deskbrief.store.backend import DAILY_TABLES, StorageBackend, table_prefix
from deskbrief.store.datasets import current_clean_version
from deskbrief.store.local import LocalBackend
from deskbrief.store.r2 import R2Backend, R2Credentials, r2_credentials_from_env

#: view name -> table it reads
TABLE_VIEWS: dict[str, str] = {
    "obs_all": "observations",
    "source_log": "source_log",
    "dq": "dq_results",
    "xchecks": "crosscheck_results",
    "runs": "runs",
    "scorecard": "scorecard_daily",
}

#: fields that count as "the day's value" in ``daily_close`` (priority order)
DAILY_CLOSE_FIELDS: tuple[str, ...] = ("close", "yield", "rate")

_SQL_TYPES = {"str": "VARCHAR", "ts": "TIMESTAMPTZ", "date": "DATE", "int": "BIGINT",
              "float": "DOUBLE", "bool": "BOOLEAN"}  # fmt: skip


def _q(ident: str) -> str:
    return '"' + ident.replace('"', '""') + '"'


def _lit(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _sql_type(kind: str) -> str:
    base = kind.rstrip("?")
    return _SQL_TYPES["str" if base.startswith("enum:") else base]


def r2_secret_sql(creds: R2Credentials) -> str:
    """The DuckDB statement that lets ``httpfs`` read ``r2://`` paths. Contains the secret."""
    return (
        "CREATE OR REPLACE TEMPORARY SECRET deskbrief_r2 (TYPE r2, "
        f"KEY_ID {_lit(creds.access_key_id)}, SECRET {_lit(creds.secret_access_key)}, "
        f"ACCOUNT_ID {_lit(creds.account_id)})"
    )


def _configure_r2(conn: duckdb.DuckDBPyConnection, env: Mapping[str, str]) -> None:
    creds = r2_credentials_from_env(env)
    conn.execute("INSTALL httpfs")
    conn.execute("LOAD httpfs")
    conn.execute(r2_secret_sql(creds))


def _glob(table: str, base: str, clean_version: str) -> str:
    prefix = table_prefix(table, clean_version)
    pattern = "date=*.parquet" if table in DAILY_TABLES else "date=*/run=*.parquet"
    return f"{base}/{prefix}{pattern}"


def table_source_sql(table: str, base: str, clean_version: str, *, has_files: bool) -> str:
    """``SELECT`` that yields ``table``'s declared columns (empty and typed if no files)."""
    cols = columns(table)
    if has_files:
        select = ", ".join(_q(c) for c in cols)
        return (
            # identifiers come from the declared schema, the path is quoted by _lit
            f"SELECT {select} FROM read_parquet({_lit(_glob(table, base, clean_version))}, "  # noqa: S608
            "union_by_name = true, hive_partitioning = false)"
        )
    typed = ", ".join(f"CAST(NULL AS {_sql_type(k)}) AS {_q(c)}" for c, k in TABLES[table].items())
    return f"SELECT {typed} WHERE false"


def _has_files(store: StorageBackend, table: str, clean_version: str) -> bool:
    return any(k.endswith(".parquet") for k in store.list(table_prefix(table, clean_version)))


_KEY = ", ".join(_q(c) for c in OBS_NATURAL_KEY)
_LATEST_RANK = (
    f"row_number() OVER (PARTITION BY {_KEY} ORDER BY ingested_at DESC, fetch_id DESC) = 1"
)
_ET_DATE = "CAST(timezone('America/New_York', as_of) AS DATE)"
_FIELD_RANK = " ".join(f"WHEN {_lit(f)} THEN {i}" for i, f in enumerate(DAILY_CLOSE_FIELDS))
_FIELD_LIST = ", ".join(_lit(f) for f in DAILY_CLOSE_FIELDS)

# Everything interpolated below is a module constant (quoted by _q/_lit); no user input.
_DERIVED_VIEWS = (
    f"CREATE VIEW obs_latest AS SELECT * FROM obs_all QUALIFY {_LATEST_RANK}",  # noqa: S608
    f"CREATE MACRO obs_pit(t) AS TABLE SELECT * FROM obs_all WHERE ingested_at <= t QUALIFY {_LATEST_RANK}",  # noqa: S608, E501
    f"""CREATE VIEW daily_close AS
        SELECT instrument_id, {_ET_DATE} AS "date", value AS close, unit, tier,
               source AS chosen_source, dataset, symbol, field, as_of, ingested_at, fetch_id,
               is_fallback
        FROM obs_latest
        WHERE interval = '1d' AND field IN ({_FIELD_LIST})
        QUALIFY row_number() OVER (
            PARTITION BY instrument_id, {_ET_DATE}
            ORDER BY tier DESC, CASE field {_FIELD_RANK} END, source, dataset, ingested_at DESC
        ) = 1""",  # noqa: S608
)

_DIM_DDL = {
    "dim_instrument": (
        "instrument_id VARCHAR, name VARCHAR, asset_class VARCHAR, kind VARCHAR, "
        "unit VARCHAR, is_proxy BOOLEAN, label VARCHAR"
    ),
    "dim_feed": (
        "instrument_id VARCHAR, source VARCHAR, dataset VARCHAR, symbol VARCHAR, "
        "tier INTEGER, scale DOUBLE"
    ),
    "dim_group": (
        'group_id VARCHAR, label VARCHAR, proxy VARCHAR, "exact" VARCHAR, official VARCHAR, '
        "relation VARCHAR, duration DOUBLE, warn_bps DOUBLE, fail_bps DOUBLE"
    ),
}


def _create_dims(conn: duckdb.DuckDBPyConnection, config: Config) -> None:
    for name, ddl in _DIM_DDL.items():
        conn.execute(f"CREATE TABLE {name} ({ddl})")
    conn.executemany(
        "INSERT INTO dim_instrument VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (i.id, i.name, i.asset_class, i.kind, i.unit, i.is_proxy, i.label)
            for i in config.instruments
        ],
    )
    conn.executemany(
        "INSERT INTO dim_feed VALUES (?, ?, ?, ?, ?, ?)",
        [
            (i.id, f.source, f.dataset, f.symbol, f.tier, f.scale)
            for i in config.instruments
            for f in i.feeds
        ],
    )
    conn.executemany(
        "INSERT INTO dim_group VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                g.id,
                g.label,
                g.proxy,
                g.exact,
                g.official,
                g.relation,
                g.duration,
                g.tolerance.warn_bps,
                g.tolerance.fail_bps,
            )
            for g in config.groups
        ],
    )


def connect(
    store_or_root: StorageBackend | str | Path,
    config: Config,
    *,
    env: Mapping[str, str] | None = None,
) -> duckdb.DuckDBPyConnection:
    """Open an in-memory DuckDB with all §7.3 views over ``store_or_root``.

    ``store_or_root`` is a ``LocalBackend`` / ``R2Backend``, or a directory (a local root).
    ``env`` supplies the ``R2_*`` variables for R2 (default: ``os.environ``).
    """
    store = LocalBackend(store_or_root) if isinstance(store_or_root, str | Path) else store_or_root

    conn = duckdb.connect(":memory:")
    conn.execute("SET TimeZone = 'UTC'")
    if isinstance(store, LocalBackend):
        base = store.root.as_posix()
    elif isinstance(store, R2Backend):
        _configure_r2(conn, os.environ if env is None else env)
        base = f"r2://{store.bucket}"
    else:
        raise TypeError(f"duck.connect supports LocalBackend and R2Backend, not {type(store)!r}")

    version = current_clean_version(store)
    for view, table in TABLE_VIEWS.items():
        source = table_source_sql(table, base, version, has_files=_has_files(store, table, version))
        conn.execute(f"CREATE VIEW {view} AS {source}")
    for statement in _DERIVED_VIEWS:
        conn.execute(statement)
    _create_dims(conn, config)
    return conn


__all__ = ["DAILY_CLOSE_FIELDS", "TABLE_VIEWS", "connect", "r2_secret_sql", "table_source_sql"]
