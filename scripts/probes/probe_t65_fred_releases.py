"""Live probe: FRED release dates (card T65). Needs FRED_API_KEY in the environment.

Usage:
  PYTHONPATH=src python scripts/probes/probe_t65_fred_releases.py RELEASE_ID [RELEASE_ID ...]
  PYTHONPATH=src python scripts/probes/probe_t65_fred_releases.py --list

With ids: fetches each release's dates for config.yaml's events horizon and prints status, row
count and newest date (the ids need not be in config.yaml yet). With --list: prints FRED's
release ids and names (metadata, to choose events.fred_releases); never the key.
"""

from __future__ import annotations

import json
import os
import sys

import httpx
from t65_common import probe_config, run

from deskbrief.config import Config, FredRelease
from deskbrief.sources.fred import RELEASE_DATASET, FredSource

RELEASES_URL = "https://api.stlouisfed.org/fred/releases"


def list_releases() -> int:
    key = os.environ.get("FRED_API_KEY", "").strip()
    if not key:
        print("FRED_API_KEY is not set")
        return 1
    resp = httpx.get(
        RELEASES_URL, params={"api_key": key, "file_type": "json", "limit": "1000"}, timeout=30
    )
    print(f"http={resp.status_code}")
    if resp.is_success:
        for item in json.loads(resp.content).get("releases", []):
            print(f"{item.get('id')}\t{item.get('name')}")
    return 0 if resp.is_success else 1


def main(argv: list[str]) -> int:
    if argv == ["--list"]:
        return list_releases()
    if not argv or not all(a.isdigit() for a in argv):
        print(__doc__)
        return 2
    releases = tuple(
        FredRelease(release_id=int(a), title=f"probe {a}", time_et="08:30") for a in argv
    )

    def with_releases(cfg: Config) -> Config:
        return cfg.model_copy(
            update={"events": cfg.events.model_copy(update={"fred_releases": releases})}
        )

    cfg = probe_config("fred", with_releases)
    return run(FredSource(cfg), cfg, [RELEASE_DATASET], "event_date")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
