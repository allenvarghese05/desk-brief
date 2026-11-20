"""Dashboard data-layer benchmark on the synthetic store (T56, P-022 phase rules).

SYNTHETIC FIXTURE: runs only against a store built by ``synth_store.py`` (made-up values). The
one observations file it adds per rebuild step is synthetic too, and it deletes those files
again at the end, so the store is the same before and after a run.

It calls only public functions that exist on main before T56 (``build_snapshot``,
``shared_connection``, ``build_bars``, ``hidden_tiers``), so the same script measures the
baseline and the change. Streamlit's ``st.cache_data`` is not involved: each step is what a page
render costs once those caches miss.

Steps (each timed as one Home render: ``build_snapshot`` plus the selected instrument's bars):

* **cold build**: the first render in a fresh process (nothing cached).
* **warm query**: a render more than a minute later with no change in the store. A fake clock
  moves ``time.monotonic`` forward 61 s before each repeat, as a user clicking a minute apart.
* **rebuild after one file**: one new observations file is written and the clock moves 61 s.
  Timed until the end of the first render whose connection shows the new row: renders repeat
  until one does (one render before T56; after T56 the listing check runs in the background
  and the first render may still be served by the old connection).

Command (from the repo root, after building a store with ``synth_store.py``)::

    .venv/bin/python tests/bench/bench_dashboard.py DIR [--repeat 5]
"""

from __future__ import annotations

import argparse
import platform
import statistics
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))  # `app` is a plain folder, not an installed package

from app.components import data  # noqa: E402

from deskbrief.config import load_config  # noqa: E402
from deskbrief.models import JobKind, RunContext, Trigger  # noqa: E402
from deskbrief.schemas import to_frame  # noqa: E402
from deskbrief.store.datasets import Datasets  # noqa: E402
from deskbrief.store.local import LocalBackend  # noqa: E402
from deskbrief.timeutil import et_date, et_to_utc  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from synth_store import END  # noqa: E402

INSTRUMENT = "SPY"
_real_monotonic = time.monotonic
_offset = [0.0]


def _fake_monotonic() -> float:
    return _real_monotonic() + _offset[0]


def advance(seconds: float = 61.0) -> None:
    _offset[0] += seconds


def timed(fn: Callable[[], object]) -> tuple[float, object]:
    t0 = _real_monotonic()
    out = fn()
    return _real_monotonic() - t0, out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="T56 dashboard benchmark")
    parser.add_argument("store_dir", type=Path, help="directory built by synth_store.py")
    parser.add_argument("--repeat", type=int, default=5)
    args = parser.parse_args(argv)
    config = load_config(args.store_dir / "config.yaml")
    store = LocalBackend(config.storage.local_root)
    hidden = data.hidden_tiers("hosted", config)
    now = et_to_utc(END, "21:00")
    time.monotonic = _fake_monotonic  # type: ignore[assignment]

    def render() -> object:
        snap = data.build_snapshot(config, {}, now, hidden)
        with data.shared_connection(config) as conn:
            data.build_bars(conn, now, INSTRUMENT, hidden)
        return snap

    cold, _ = timed(render)
    warm = []
    for _ in range(args.repeat):
        advance()
        warm.append(timed(render)[0])

    added: list[str] = []
    rebuild = []
    try:
        for n in range(args.repeat):
            run_id = f"SYNBENCH-{n:03d}"
            added.append(_add_one_file(store, config, run_id, now - timedelta(minutes=n + 1)))
            advance()
            t0 = _real_monotonic()
            while not _render_shows(render, config, run_id):
                assert _real_monotonic() - t0 < 120, "the new file never became visible"
            rebuild.append(_real_monotonic() - t0)
    finally:
        for key in added:
            store.delete(key)

    sha = subprocess.run(
        ["git", "-C", str(REPO), "rev-parse", "--short", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    print(f"commit       {sha}")
    print(
        f"machine      {platform.platform()} · {platform.machine()} · py{platform.python_version()}"
    )
    print(f"store        {args.store_dir} ({len(_obs_keys(store))} observations files)")
    print(f"cold build   {cold * 1000:9.1f} ms")
    print(f"warm query   {statistics.median(warm) * 1000:9.1f} ms  (median of {args.repeat})")
    print(f"rebuild +1   {statistics.median(rebuild) * 1000:9.1f} ms  (median of {args.repeat})")
    return 0


def _render_shows(render: Callable[[], object], config: object, run_id: str) -> bool:
    render()
    with data.shared_connection(config) as conn:  # type: ignore[arg-type]
        seen = conn.execute("SELECT count(*) FROM obs_all WHERE run_id = ?", [run_id]).fetchone()
    return seen == (1,)


def _obs_keys(store: LocalBackend) -> list[str]:
    return [k for k in store.list("clean/") if "/observations/" in k and k.endswith(".parquet")]


def _add_one_file(store: LocalBackend, config: object, run_id: str, at: object) -> str:
    """One synthetic tick for INSTRUMENT in a new file of the last ET date; returns its key."""
    from datetime import UTC, datetime

    assert isinstance(at, datetime)
    d = et_date(at)
    row = {
        "instrument_id": INSTRUMENT, "source": "yahoo", "dataset": "quote", "symbol": "SPY",
        "tier": 2, "field": "last", "interval": "tick", "as_of": at, "value": 500.0,
        "unit": "USD", "is_derived": False, "is_fallback": False, "fetch_id": f"F-{run_id}",
        "run_id": run_id, "ingested_at": at, "parser_version": "synthetic-1",
    }  # fmt: skip
    ctx = RunContext(
        run_id=run_id, job=JobKind.INTRADAY, trigger=Trigger.MANUAL, target_date=d,
        now_utc=datetime(2026, 11, 20, 2, 0, tzinfo=UTC), git_sha="synthetic",
        config_hash="synthetic", config=config, store=store,  # type: ignore[arg-type]
    )  # fmt: skip
    return Datasets(store).append("observations", to_frame("observations", [row]), ctx, d)


if __name__ == "__main__":
    sys.exit(main())
