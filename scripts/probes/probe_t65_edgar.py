"""Live probe: SEC EDGAR (card T65). Needs DESKBRIEF_SEC_USER_AGENT in the environment (a contact
User-Agent such as "Name email"; SEC fair-access policy). Never printed.

Usage:
  PYTHONPATH=src python scripts/probes/probe_t65_edgar.py INSTRUMENT CIK [CUSIP [HOLDER_CIK ...]]

INSTRUMENT must be a stock in config.yaml; CIK is its 10-digit issuer CIK. With a CUSIP and one
or more 13F filer CIKs the holders dataset is probed too. The CIKs and CUSIP are passed on the
command line so nothing is committed. Prints HTTP status, fetch status, row count and the newest
filed date per fetch only.
"""

from __future__ import annotations

import sys

from t65_common import probe_config, run

from deskbrief.config import Config, FilingsCompany, FilingsHolder
from deskbrief.sources.edgar import COMPANY_DATASET, HOLDERS_DATASET, EdgarSource, pad_cik


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    instrument, cik, *rest = argv
    cusip = rest[0] if rest else None
    holders = tuple(FilingsHolder(cik=pad_cik(h), name=f"holder {h}") for h in rest[1:])

    def with_filings(cfg: Config) -> Config:
        company = FilingsCompany(instrument=instrument, cik=pad_cik(cik), cusip=cusip)
        filings = cfg.filings.model_copy(update={"companies": (company,), "holders": holders})
        return cfg.model_copy(update={"filings": filings})

    cfg = probe_config("edgar", with_filings)
    return run(EdgarSource(cfg), cfg, [COMPANY_DATASET, HOLDERS_DATASET], "filed_date")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
