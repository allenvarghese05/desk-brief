SYNTHETIC FIXTURE. These files imitate the documented FRED `series/observations` JSON
response (`file_type=json`). Every value is invented; nothing here was fetched from FRED.
The top-level `_synthetic` key is not part of the real payload (the parser ignores unknown keys).
Replace or complement them with a real capture after the owner runs the live probe
(see `docs/sources/fred.md`).

T46 added `dfii10.json`, `t10yie.json`, `dtwexbgs.json`, `bamlh0a0hym2.json` and `nfci.json`
(same rules: invented values, schema-faithful). `dfii10.json` holds a real yield of −1.98 to
exercise the low end of the `pct` bound; `nfci.json` holds negative weekly values dated on
Fridays.
