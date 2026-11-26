"""Live probe: Treasury upcoming auctions (fiscaldata, card T65). Keyless.

Usage: PYTHONPATH=src python scripts/probes/probe_t65_fiscaldata.py
Prints the HTTP status, fetch status, row count and newest auction date only.
"""

from __future__ import annotations

import sys

from t65_common import probe_config, run

from deskbrief.sources.fiscaldata import DATASET, FiscalDataSource


def main() -> int:
    cfg = probe_config("fiscaldata")
    return run(FiscalDataSource(cfg), cfg, [DATASET], "event_date")


if __name__ == "__main__":
    sys.exit(main())
