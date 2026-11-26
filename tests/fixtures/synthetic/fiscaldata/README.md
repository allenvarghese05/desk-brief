SYNTHETIC FIXTURE (card T65). `upcoming_auctions.json` imitates the documented shape of the
Fiscal Data API `v1/accounting/od/upcoming_auctions` response (`data`, `meta`, `links`; every
value a string, `"null"` when not given). Dates, CUSIPs and offering amounts are invented;
nothing here was fetched from fiscaldata.treasury.gov. The top-level `_synthetic` key is not part
of the real payload (the parser ignores it). Live probe: `docs/sources/fiscaldata.md`.

Contents: a 4-Week Bill auction published on two record dates (the newer record must win), a
26-Week Bill auction, a 2-Year Note auction with no CUSIP yet (`"null"`), and one auction after
the requested window (dropped by the parser).
