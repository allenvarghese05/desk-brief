SYNTHETIC FIXTURE. `cot_legacy.json` imitates the CFTC public-reporting (Socrata) answer for the
legacy futures-only Commitments of Traders dataset `6dca-aqww`, restricted to the `$select`
columns that `src/deskbrief/sources/cftc.py` asks for. Every value is invented; nothing was
fetched from CFTC. The real answer is a bare JSON list; the file wraps it as
`{"_synthetic": ..., "rows": [...]}` so it can carry this header, and the tests send only
`rows` as the body.

Four weekly reports (Tuesdays 2026-10-20 to 2026-11-10) for gold (`088691`), the E-mini S&P 500
(`13874A`) and the 10-year note (`043602`). Week i (0..3): open interest = base + 1000·i,
non-commercial long = base + 100·i, short = base − 50·i. Column names and the date format
(`2026-11-10T00:00:00.000`) are the ones the live probe saw on 2026-11-19.
