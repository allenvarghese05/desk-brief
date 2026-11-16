SYNTHETIC FIXTURE. These files imitate the documented Twelve Data `/quote` and `/time_series`
JSON responses (batch form, symbols separated by commas). Every value is invented; nothing here
was fetched from Twelve Data. Fields the provider may not return for FX (`bid`, `ask`) are
included in one fixture only to exercise the optional-field path. Replace or complement them with
the owner's live probe (see `docs/sources/twelvedata.md`); real captures from this commercial
provider must not be committed.
