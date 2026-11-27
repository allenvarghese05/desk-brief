SYNTHETIC FIXTURE (card T65). Invented SEC EDGAR documents for the fictional issuer
"Example Corp (EXMP)", CIK 0009999999, and the fictional 13F filer "Invented Asset
Management", CIK 0007777777. Insiders "Jane Example" (CIK 0008888888) and "John Sample"
(CIK 0008888889; EDGAR writes owners last name first: "Example Jane"), every accession number, date, share count, price and value are made up.
Nothing here was fetched from EDGAR or describes a real company, person or fund. The files
follow the documented shapes: the `data.sec.gov` submissions JSON (`filings.recent` parallel
lists), Section 16 ownership XML (Form 4), a filing `index.json`, the 13F cover page
(`primary_doc.xml`) and information table. Top-level `_synthetic` keys / XML comments are
not part of the real documents. Live probe: `docs/sources/edgar.md`.

Files
- `submissions_0009999999.json`: Example Corp. Two 10-K, two 10-Q, Form 4s on 2026-11-20
  (sale under a 10b5-1 plan plus a tax withholding), 2026-11-10 (director purchase),
  2026-11-12 (a 4/A restating that purchase), 2026-10-01 (a grant with no price) and
  2026-07-01 (outside a 90-day window from 2026-11-26), and an 8-K.
- `form4_sale.xml`, `form4_buy.xml`, `form4a_buy.xml`, `form4_grant.xml`, `form4_old.xml`.
- `submissions_0007777777.json`: Invented Asset Management. 13F-HR for 2026-03-31, 2026-06-30
  and 2026-09-30, and a 13F-HR/A for 2026-09-30.
- `index.json`: one filing index (primary_doc.xml + infotable.xml), reused for every 13F.
- `infotable_<accession without dashes>.xml`: information tables. EXMP's CUSIP is EXMP00010;
  OTHR00019 is another invented security; the 2026-06-30 table also holds a put on EXMP,
  which is not a share position.
- `cover_restatement.xml`, `cover_new_holdings.xml`: 13F-HR/A cover pages.
