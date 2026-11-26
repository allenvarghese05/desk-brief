"""SEC EDGAR filings for the single stocks in ``config.filings`` (card T65, P-024 Company).

A table source (``sources/tables.py``) for ``filings``; it produces no observations. Two
datasets, both planned in the ``morning`` job only:

* ``company``: one fetch per ``filings.companies`` entry (symbol = the instrument id). It reads
  ``data.sec.gov/submissions/CIK##########.json`` and then the XML of every Form 4 / 4/A filed
  in the last ``filings.insider_days`` (from the target date). Rows: the latest 10-K and the
  latest 10-Q (a filing row each: form, filed date, period, link), and each Form 4 with one
  ``insider_trade`` row per non-derivative transaction.
* ``holders``: one fetch per ``filings.holders`` entry (symbol = the holder's CIK). It reads the
  holder's submissions, then for the latest 13F period and the one before it every 13F-HR and
  13F-HR/A: the filing ``index.json``, the information table (kept only for the configured
  companies' CUSIPs) and, for an amendment, the cover page (``amendmentType``). Rows: for each
  company the holder held in either quarter, the 13F filing row and one ``holder_change`` row
  (shares at the latest period end, the change against the previous period, market value).

One fetch makes several HTTP requests, so ``fetch`` returns ONE ``RawResponse`` whose body is a
JSON bundle of every document it read (``{"bundle": ..., "documents": [{"role", "url",
"accession", "text"}, ...]}``); ``parse_table`` is a pure function of that bundle. The bundle
keeps each document's text as fetched, except 13F information tables, which are reduced to the
``infoTable`` entries of the configured CUSIPs (large filers' tables run to tens of MB); the
full document's size and sha256 are recorded next to it. If a request fails, the fetch fails as
a whole (its status and error are those of the failed request), with one exception: a 404 on a
document of one filing (a Form 4 XML, a 13F ``index.json``, information table or cover page) skips
that filing, logged (``edgar.document_skipped``) and listed under ``skipped`` in the bundle,
instead of failing the company or holder every day (PR #102 review). Each accession is still
written whole or not at all: a skipped Form 4 gets no rows, and a holder whose latest or previous
13F period has a skipped filing gets no rows that run (its holdings would be wrong). The
submissions JSON itself, and any other failure (5xx, 429), still fail the whole fetch.

User-Agent (SEC fair access): read from the environment variable named by
``sources.edgar.user_agent_env`` (``DESKBRIEF_SEC_USER_AGENT``) and passed ONLY as
``secret_headers={"User-Agent": ...}``: it is never in a ``FetchRequest``, ``url_redacted``, the
bundle, ``source_log`` or a log line. **Missing**: no request is made and the fetch returns an
error naming the variable (not its value), like FRED's missing key; the run continues degraded.
Rate: the shared client's limiter at ``sources.edgar.rate_per_min`` (300, half SEC's 10/s).

Rules for the rows (``schemas`` writer rules):

* ``amends_accession`` only for a full restatement, on seq 0 only: a Form 4/A (a complete
  amended form) names the Form 4 of the same reporting owner filed on its
  ``dateOfOriginalSubmission`` when exactly one is in the bundle, else the one Form 4 of that
  owner filed on that date or with the same ``periodOfReport`` (never one filed after the
  amendment). When that is still not exactly one, ``amends_accession`` stays null and the seq 0
  ``details_json`` carries ``amendment_unmatched: true`` and ``amends_candidates`` (the owner's
  possible originals in the bundle); ``query.latest_amendments_only`` then drops those
  candidates, so only the amendment is counted, never both. A 13F-HR/A whose cover says
  ``RESTATEMENT`` names its period's original 13F-HR. A 13F-HR/A ``NEW HOLDINGS`` amendment
  adds positions to the original, so it amends nothing: its positions are added to the
  period's holdings and it gets a filing row only (``details_json.merged_into``). 10-K/A and
  10-Q/A are not fetched (often partial; the latest original is shown).
* ``plan_10b5_1`` is true for a transaction whose footnotes mention Rule 10b5-1, or, when the
  form's 10b5-1 checkbox (``aff10b5One``) is ticked, for its open-market trades (codes P, S).
* 13F: only share positions (``SH``, no put/call) count; ``value`` is USD (13F tables have
  reported whole dollars since 2023). The previous period missing means zero shares then.

Probe status and terms: ``docs/sources/edgar.md``. Tests use INVENTED filings only.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import pandas as pd

from deskbrief.config import Config, FilingsCompany
from deskbrief.ids import new_id
from deskbrief.logging import get_logger
from deskbrief.models import (
    FetchRequest,
    FetchStatus,
    FilingRowKind,
    JobKind,
    RawResponse,
    RunContext,
    TradeSide,
)
from deskbrief.schemas import empty_frame
from deskbrief.sources.base import HttpClient, ParseError
from deskbrief.sources.registry import register
from deskbrief.sources.tables import EDGAR_COMPANY, EDGAR_HOLDERS, TableName, table_frame
from deskbrief.timeutil import utc_now

SOURCE = "edgar"
COMPANY_DATASET = EDGAR_COMPANY.dataset
HOLDERS_DATASET = EDGAR_HOLDERS.dataset
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{nodash}/{name}"
INDEX_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{nodash}/{accession}-index.htm"
BUNDLE_VERSION = "edgar-bundle/1"
FORM4 = ("4", "4/A")
FORM13F = ("13F-HR", "13F-HR/A")
PERIODIC = ("10-K", "10-Q")
RESTATEMENT = "RESTATEMENT"
NEW_HOLDINGS = "NEW HOLDINGS"
_PLAN_RE = re.compile(r"10b5-?1", re.IGNORECASE)
_TRUE = {"1", "true"}


class _FetchFailedError(Exception):
    """A request inside a bundle failed; carries the response that failed."""

    def __init__(self, role: str, raw: RawResponse) -> None:
        super().__init__(role)
        self.role = role
        self.raw = raw


def pad_cik(cik: str | int) -> str:
    """10-digit zero-padded CIK."""
    return f"{int(cik):010d}"


def _nodash(accession: str) -> str:
    return accession.replace("-", "")


def index_url(cik: str, accession: str) -> str:
    """EDGAR filing index page (the link shown for a filing)."""
    return INDEX_URL.format(cik=int(cik), nodash=_nodash(accession), accession=accession)


def _strip_xsl(primary_document: str) -> str:
    """``xslF345X05/form4.xml`` (the rendered view) -> ``form4.xml`` (the raw XML)."""
    head, sep, tail = primary_document.partition("/")
    return tail if sep and head.lower().startswith("xsl") else primary_document


# ---------------------------------------------------------------------------------------------
# XML helpers (stdlib ElementTree; documents with a DTD or entity declarations are refused)
# ---------------------------------------------------------------------------------------------


def _check_xml(text: str | bytes) -> None:
    head = text[:4096] if isinstance(text, str) else text[:4096].decode("utf-8", "replace")
    if "<!doctype" in head.lower() or "<!entity" in head.lower():
        raise ParseError("EDGAR XML with a DTD or entity declarations is refused")


def _xml(text: str) -> ET.Element:
    _check_xml(text)
    try:
        return ET.fromstring(text)  # noqa: S314 - DTD/entities refused above
    except ET.ParseError as exc:
        raise ParseError(f"EDGAR XML does not parse: {exc}") from exc


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child(elem: ET.Element | None, name: str) -> ET.Element | None:
    if elem is None:
        return None
    for c in elem:
        if _local(c.tag) == name:
            return c
    return None


def _children(elem: ET.Element | None, name: str) -> list[ET.Element]:
    return [] if elem is None else [c for c in elem if _local(c.tag) == name]


def _path(elem: ET.Element | None, *names: str) -> ET.Element | None:
    for n in names:
        elem = _child(elem, n)
    return elem


def _text(elem: ET.Element | None, *names: str) -> str | None:
    node = _path(elem, *names)
    if node is None or node.text is None:
        return None
    text = node.text.strip()
    return text or None


def _value(elem: ET.Element | None, *names: str) -> str | None:
    """Ownership XML wraps most values: ``<transactionShares><value>10</value>``."""
    return _text(elem, *names, "value")


def _descendants(elem: ET.Element, name: str) -> Iterable[ET.Element]:
    return (e for e in elem.iter() if _local(e.tag) == name)


def _float(text: str | None, what: str) -> float | None:
    if text is None:
        return None
    try:
        value = float(text.replace(",", ""))
    except ValueError as exc:
        raise ParseError(f"EDGAR {what} is not a number: {text!r}") from exc
    if value != value or value in (float("inf"), float("-inf")) or value < 0:
        raise ParseError(f"EDGAR {what} is not a non-negative number: {text!r}")
    return value


def _day(text: str | None, what: str) -> date | None:
    if text is None:
        return None
    try:
        return date.fromisoformat(text.strip()[:10])
    except ValueError as exc:
        raise ParseError(f"EDGAR {what} is not a date: {text!r}") from exc


def filter_info_table(body: bytes, cusips: Iterable[str]) -> str:
    """The ``infoTable`` entries of a 13F information table whose CUSIP is in ``cusips``."""
    wanted = {c.upper() for c in cusips}
    _check_xml(body)
    kept: list[str] = []
    try:
        for _, elem in ET.iterparse(io.BytesIO(body), events=("end",)):  # noqa: S314
            if _local(elem.tag) != "infoTable":
                continue
            cusip = (_text(elem, "cusip") or "").upper()
            if cusip in wanted:
                kept.append(ET.tostring(elem, encoding="unicode"))
            elem.clear()
    except ET.ParseError as exc:
        raise ParseError(f"13F information table does not parse: {exc}") from exc
    return "<informationTable>" + "".join(kept) + "</informationTable>"


# ---------------------------------------------------------------------------------------------
# submissions JSON
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Filing:
    """One entry of ``filings.recent`` in a submissions JSON."""

    accession: str
    form: str
    filed: date
    period: date | None
    primary_document: str


def recent_filings(doc: Any) -> list[Filing]:
    """The ``filings.recent`` table of a submissions JSON as :class:`Filing` rows."""
    if not isinstance(doc, dict):
        raise ParseError("EDGAR submissions is not a JSON object")
    recent = doc.get("filings", {}).get("recent") if isinstance(doc.get("filings"), dict) else None
    if not isinstance(recent, dict):
        raise ParseError("EDGAR submissions has no filings.recent table")
    cols = ("accessionNumber", "form", "filingDate", "reportDate", "primaryDocument")
    arrays: list[list[Any]] = []
    for c in cols:
        values = recent.get(c)
        if not isinstance(values, list):
            raise ParseError(f"EDGAR submissions filings.recent needs the lists {cols}")
        arrays.append(values)
    if len({len(a) for a in arrays}) != 1:
        raise ParseError("EDGAR submissions filings.recent lists differ in length")
    out: list[Filing] = []
    for acc, form, filed, period, primary in zip(*arrays, strict=True):
        if not isinstance(acc, str) or not isinstance(form, str) or not isinstance(filed, str):
            raise ParseError(f"EDGAR submissions entry is malformed: {acc!r}")
        filed_day = _day(filed, "filingDate")
        assert filed_day is not None
        out.append(
            Filing(
                accession=acc,
                form=form,
                filed=filed_day,
                period=_day(period, "reportDate") if isinstance(period, str) and period else None,
                primary_document=primary if isinstance(primary, str) else "",
            )
        )
    return out


def _latest(filings: Iterable[Filing], form: str) -> Filing | None:
    same = [f for f in filings if f.form == form]
    return max(same, key=lambda f: (f.filed, f.accession)) if same else None


def thirteen_f_periods(filings: list[Filing]) -> tuple[date | None, date | None]:
    """``(latest period, the period before it)`` among the holder's 13F filings."""
    periods = sorted({f.period for f in filings if f.form in FORM13F and f.period}, reverse=True)
    latest = periods[0] if periods else None
    previous = periods[1] if len(periods) > 1 else None
    return latest, previous


# ---------------------------------------------------------------------------------------------
# the source
# ---------------------------------------------------------------------------------------------


class EdgarSource:
    """SEC EDGAR; a table source for ``filings``. See the module docstring."""

    name = SOURCE
    parser_version = "1"
    table: TableName = "filings"

    def __init__(self, config: Config) -> None:
        self.config = config
        self.settings = config.sources.get(SOURCE)
        self._companies: dict[str, FilingsCompany] = {
            c.instrument: c for c in config.filings.companies
        }
        self._holders = {h.cik: h for h in config.filings.holders}

    @property
    def user_agent_env(self) -> str | None:
        return self.settings.user_agent_env if self.settings is not None else None

    def table_for(self, req: FetchRequest) -> TableName | None:
        return "filings" if req.dataset in (COMPANY_DATASET, HOLDERS_DATASET) else None

    # -- plan -----------------------------------------------------------------------------

    def plan(self, job: JobKind, ctx: RunContext) -> list[FetchRequest]:
        """``morning`` only: one ``company`` fetch per company, one ``holders`` fetch per
        holder (when some company has a CUSIP to look for)."""
        if job is not JobKind.MORNING:
            return []
        since = ctx.target_date - timedelta(days=self.config.filings.insider_days)
        out = [
            FetchRequest(
                SOURCE,
                COMPANY_DATASET,
                params={"cik": c.cik, "since": since.isoformat()},
                symbols=(c.instrument,),
                target_date=ctx.target_date,
            )
            for c in self.config.filings.companies
        ]
        if any(c.cusip for c in self.config.filings.companies):
            out.extend(
                FetchRequest(
                    SOURCE,
                    HOLDERS_DATASET,
                    params={"cik": h.cik},
                    symbols=(h.cik,),
                    target_date=ctx.target_date,
                )
                for h in self.config.filings.holders
            )
        return out

    # -- fetch ----------------------------------------------------------------------------

    def _user_agent(self) -> str | None:
        env = self.user_agent_env
        value = os.environ.get(env, "").strip() if env else ""
        return value or None

    def fetch(self, req: FetchRequest, http: HttpClient) -> RawResponse:
        ua = self._user_agent()
        if ua is None:
            now = utc_now()
            get_logger(source=self.name, dataset=req.dataset).error(
                "edgar.fetch.no_user_agent", env=self.user_agent_env
            )
            return RawResponse(
                request=req,
                fetch_id=new_id(),
                requested_at=now,
                responded_at=now,
                http_status=None,
                content_type="",
                body=b"",
                error=f"{self.user_agent_env or 'user_agent_env'} is not set; no request made",
                status=FetchStatus.ERROR,
            )
        bundle = _Bundle(req, http, ua)
        try:
            if req.dataset == COMPANY_DATASET:
                self._fetch_company(req, bundle)
            elif req.dataset == HOLDERS_DATASET:
                self._fetch_holder(req, bundle)
            else:
                raise ValueError(f"edgar has no dataset {req.dataset!r}")
        except _FetchFailedError as failed:
            return bundle.failure(failed)
        return bundle.response()

    def _fetch_company(self, req: FetchRequest, bundle: _Bundle) -> None:
        cik = pad_cik(req.params["cik"])
        since = date.fromisoformat(req.params["since"])
        doc = bundle.get_json("submissions", SUBMISSIONS_URL.format(cik=cik))
        if doc is None:
            return
        try:
            filings = recent_filings(doc)
        except ParseError as exc:
            bundle.incomplete = str(exc)
            return
        for f in filings:
            if f.form in FORM4 and f.filed >= since and f.primary_document:
                url = ARCHIVE_URL.format(
                    cik=int(cik), nodash=_nodash(f.accession), name=_strip_xsl(f.primary_document)
                )
                bundle.get_text("form4", url, accession=f.accession)  # None: 404, skipped

    def _fetch_holder(self, req: FetchRequest, bundle: _Bundle) -> None:
        cik = pad_cik(req.params["cik"])
        cusips = [c.cusip for c in self.config.filings.companies if c.cusip]
        doc = bundle.get_json("submissions", SUBMISSIONS_URL.format(cik=cik))
        if doc is None:
            return
        try:
            filings = recent_filings(doc)
        except ParseError as exc:
            bundle.incomplete = str(exc)
            return
        latest, previous = thirteen_f_periods(filings)
        for f in filings:
            if f.form not in FORM13F or f.period not in (latest, previous) or f.period is None:
                continue
            base = ARCHIVE_URL.format(cik=int(cik), nodash=_nodash(f.accession), name="")
            index = bundle.get_json("index", base + "index.json", accession=f.accession)
            if f.accession in bundle.skipped_accessions:
                continue  # 404: the holder gets no rows this run (see _holder_rows)
            table = _info_table_name(index, f.primary_document)
            if table is None:
                bundle.incomplete = f"{f.accession}: no information table in index.json"
                return
            if not bundle.get_info_table(base + table, f.accession, cusips):
                continue
            if f.form.endswith("/A") and f.primary_document:
                bundle.get_text(
                    "cover", base + _strip_xsl(f.primary_document), accession=f.accession
                )

    # -- parse ----------------------------------------------------------------------------

    def parse(self, raw: RawResponse) -> pd.DataFrame:
        """No observations: filings go to the ``filings`` table through :meth:`parse_table`."""
        return empty_frame("parsed_observations")

    def parse_table(self, raw: RawResponse) -> pd.DataFrame:
        if not raw.ok:
            raise ParseError(f"EDGAR fetch failed: {raw.error or raw.http_status}")
        docs, skipped = _read_bundle(raw)
        submissions = _one(docs, "submissions")
        try:
            subs = json.loads(submissions["text"])
        except ValueError as exc:
            raise ParseError("EDGAR submissions is not valid JSON") from exc
        filings = recent_filings(subs)
        req = raw.request
        if req.dataset == COMPANY_DATASET:
            rows = self._company_rows(req, filings, docs, skipped)
        elif req.dataset == HOLDERS_DATASET:
            name = subs.get("name") if isinstance(subs.get("name"), str) else None
            rows = self._holder_rows(req, filings, docs, name, skipped)
        else:
            raise ParseError(f"edgar has no dataset {req.dataset!r}")
        return table_frame("filings", rows)

    def _base(self, req: FetchRequest, instrument: str, cik: str, f: Filing) -> dict[str, Any]:
        return {
            "instrument_id": instrument,
            "cik": cik,
            "accession": f.accession,
            "form": f.form,
            "filed_date": f.filed,
            "period": f.period,
            "url": index_url(cik, f.accession),
            "plan_10b5_1": False,
            "amends_accession": None,
            "source": SOURCE,
            "dataset": req.dataset,
        }

    def _company_rows(
        self,
        req: FetchRequest,
        filings: list[Filing],
        docs: list[dict[str, Any]],
        skipped: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        instrument = req.symbols[0] if req.symbols else ""
        company = self._companies.get(instrument)
        if company is None:
            raise ParseError(f"EDGAR fetch for unconfigured instrument {instrument!r}")
        cik = company.cik
        rows: list[dict[str, Any]] = []
        for form in PERIODIC:
            latest = _latest(filings, form)
            if latest is not None:
                url = ARCHIVE_URL.format(
                    cik=int(cik), nodash=_nodash(latest.accession), name=latest.primary_document
                )
                rows.append(
                    {
                        **self._base(req, instrument, cik, latest),
                        "url": url if latest.primary_document else index_url(cik, latest.accession),
                        "seq": 0,
                        "row_kind": FilingRowKind.FILING.value,
                        "details_json": _json({"primary_document": latest.primary_document}),
                    }
                )
        by_acc = {f.accession: f for f in filings}
        forms = [(d, by_acc.get(str(d.get("accession")))) for d in docs if d["role"] == "form4"]
        parsed: list[tuple[Filing, Form4]] = []
        for doc, filing in forms:
            if filing is None:
                raise ParseError(f"Form 4 {doc.get('accession')!r} is not in the submissions")
            parsed.append((filing, parse_form4(doc["text"], filing)))
        originals = [(f, p) for f, p in parsed if f.form == "4"]
        for filing, form4 in parsed:
            amends, candidates = None, None
            if filing.form == "4/A":
                amends, candidates = _form4_original(filing, form4, originals)
                missing = _skipped_originals(filing, form4, filings, skipped or set())
                strict = (
                    amends is not None
                    and form4.original_submission is not None
                    and by_acc[amends].filed == form4.original_submission
                )
                if missing and not strict:
                    # PR #102 review B3: a Form 4 that could be its original was not readable
                    # this run (404). Writing the amendment now would match it more weakly
                    # than the stored row, which names the original; skip it, so the stored
                    # rows stay current and nothing is counted twice.
                    get_logger().warning(
                        "edgar.amendment_skipped",
                        accession=filing.accession,
                        missing_originals=missing,
                    )
                    continue
            rows.extend(form4.rows(self._base(req, instrument, cik, filing), amends, candidates))
        return rows

    def _holder_rows(
        self,
        req: FetchRequest,
        filings: list[Filing],
        docs: list[dict[str, Any]],
        filer_name: str | None,
        skipped: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        holder_cik = pad_cik(req.params.get("cik", req.symbols[0] if req.symbols else "0"))
        holder = self._holders.get(holder_cik)
        party = holder.name if holder is not None else (filer_name or holder_cik)
        latest, previous = thirteen_f_periods(filings)
        if latest is None:
            return []
        missing = sorted(
            f.accession
            for f in filings
            if f.form in FORM13F
            and f.period in (latest, previous)
            and f.accession in (skipped or ())
        )
        if missing:  # a 13F document was not found: these periods' holdings would be wrong
            get_logger(source=self.name, dataset=req.dataset).warning(
                "edgar.holder_skipped", holder_cik=holder_cik, accessions=missing
            )
            return []
        tables = {str(d["accession"]): d["text"] for d in docs if d["role"] == "infotable"}
        covers = {str(d["accession"]): d["text"] for d in docs if d["role"] == "cover"}
        cur = _period_holdings(filings, latest, tables, covers)
        prev = _period_holdings(filings, previous, tables, covers) if previous else None
        rows: list[dict[str, Any]] = []
        for company in self.config.filings.companies:
            if not company.cusip:
                continue
            cusip = company.cusip.upper()
            now_sh, now_val, title = cur.holdings.get(cusip, (0.0, 0.0, None))
            was_sh = prev.holdings.get(cusip, (0.0, 0.0, None))[0] if prev else 0.0
            if now_sh == 0 and was_sh == 0:
                continue
            carrier = cur.carrier
            if carrier is None:
                continue
            base = self._base(req, company.instrument, company.cik, carrier)
            base.update(party=party, party_cik=holder_cik, period=latest)
            info = {
                "amendment_type": cur.amendment_types.get(carrier.accession),
                "new_holdings_from": cur.merged,
                "holder_cik": holder_cik,
            }
            rows.append(
                {
                    **base,
                    "seq": 0,
                    "row_kind": FilingRowKind.FILING.value,
                    "amends_accession": cur.amends,
                    "details_json": _json(info),
                }
            )
            rows.append(
                {
                    **base,
                    "seq": 1,
                    "row_kind": FilingRowKind.HOLDER_CHANGE.value,
                    "shares": now_sh,
                    "shares_change": now_sh - was_sh,
                    "value_usd": now_val,
                    "details_json": _json(
                        {
                            "cusip": cusip,
                            "title_of_class": title,
                            "previous_period": previous.isoformat() if previous else None,
                            "previous_shares": was_sh,
                        }
                    ),
                }
            )
            for acc in cur.merged:  # NEW HOLDINGS amendments: a filing row, amending nothing
                f = cur.by_accession[acc]
                extra = self._base(req, company.instrument, company.cik, f)
                extra.update(party=party, party_cik=holder_cik, period=latest)
                rows.append(
                    {
                        **extra,
                        "seq": 0,
                        "row_kind": FilingRowKind.FILING.value,
                        "details_json": _json(
                            {"amendment_type": NEW_HOLDINGS, "merged_into": carrier.accession}
                        ),
                    }
                )
        return rows


# ---------------------------------------------------------------------------------------------
# fetch bundle
# ---------------------------------------------------------------------------------------------


class _Bundle:
    """The documents one EDGAR fetch reads, and the single ``RawResponse`` made of them."""

    def __init__(self, req: FetchRequest, http: HttpClient, user_agent: str) -> None:
        self.req = req
        self.http = http
        self._ua = user_agent
        self.responses: list[RawResponse] = []
        self.documents: list[dict[str, Any]] = []
        self.incomplete: str | None = None
        #: documents of one filing that answered 404: that filing is skipped, not the fetch
        self.skipped: list[dict[str, Any]] = []

    @property
    def skipped_accessions(self) -> set[str]:
        return {str(d["accession"]) for d in self.skipped}

    def _get(self, role: str, url: str, accession: str | None = None) -> RawResponse | None:
        """The response, or ``None`` when a filing's document is not found (404, skipped).

        Any other failure, and any failure of a request that belongs to no single filing (the
        submissions JSON), fails the whole fetch.
        """
        raw = self.http.get(self.req, url, secret_headers={"User-Agent": self._ua})
        self.responses.append(raw)
        if raw.ok:
            return raw
        if accession is not None and raw.http_status == 404:
            self.skipped.append(
                {"role": role, "accession": accession, "url": raw.url_redacted, "http_status": 404}
            )
            get_logger(source=SOURCE, dataset=self.req.dataset).warning(
                "edgar.document_skipped", role=role, accession=accession, http_status=404
            )
            return None
        raise _FetchFailedError(role, raw)

    def _doc(self, role: str, raw: RawResponse, accession: str | None, text: str) -> None:
        self.documents.append(
            {"role": role, "url": raw.url_redacted, "accession": accession, "text": text}
        )

    def get_text(self, role: str, url: str, *, accession: str | None = None) -> str | None:
        raw = self._get(role, url, accession)
        if raw is None:
            return None
        text = raw.body.decode("utf-8", "replace")
        self._doc(role, raw, accession, text)
        return text

    def get_json(self, role: str, url: str, *, accession: str | None = None) -> Any:
        text = self.get_text(role, url, accession=accession)
        if text is None:
            return None
        try:
            return json.loads(text)
        except ValueError:
            self.incomplete = f"{role} is not valid JSON"
            return None

    def get_info_table(self, url: str, accession: str, cusips: list[str]) -> bool:
        """Read and filter one information table; ``False`` when it was skipped (404)."""
        raw = self._get("infotable", url, accession)
        if raw is None:
            return False
        try:
            text = filter_info_table(raw.body, cusips)
        except ParseError as exc:
            self.incomplete = f"{accession}: {exc}"
            text = ""
        doc = {
            "role": "infotable",
            "url": raw.url_redacted,
            "accession": accession,
            "text": text,
            "filtered_by_cusip": sorted(c.upper() for c in cusips),
            "full_bytes": len(raw.body),
            "full_sha256": hashlib.sha256(raw.body).hexdigest(),
        }
        self.documents.append(doc)
        return True

    def _span(self) -> tuple[Any, Any, int]:
        first, last = self.responses[0], self.responses[-1]
        return first.requested_at, last.responded_at, sum(r.attempts for r in self.responses)

    def failure(self, failed: _FetchFailedError) -> RawResponse:
        """The fetch failed as a whole: status and error of the request that failed."""
        req_at, resp_at, attempts = self._span()
        bad = failed.raw
        return RawResponse(
            request=self.req,
            fetch_id=new_id(),
            requested_at=req_at,
            responded_at=resp_at,
            http_status=bad.http_status,
            content_type=bad.content_type,
            body=b"",
            error=f"{failed.role} request failed: {bad.error or bad.http_status}",
            attempts=attempts,
            url_redacted=bad.url_redacted,
            status=FetchStatus.ERROR if bad.fetch_status is FetchStatus.OK else bad.fetch_status,
        )

    def response(self) -> RawResponse:
        req_at, resp_at, attempts = self._span()
        body = {
            "bundle": BUNDLE_VERSION,
            "dataset": self.req.dataset,
            "cik": self.req.params.get("cik"),
            "incomplete": self.incomplete,
            "documents": self.documents,
            "skipped": self.skipped,
        }
        return RawResponse(
            request=self.req,
            fetch_id=new_id(),
            requested_at=req_at,
            responded_at=resp_at,
            http_status=200,
            content_type="application/json",
            body=json.dumps(body, sort_keys=True).encode(),
            attempts=attempts,
            url_redacted=self.responses[0].url_redacted,
        )


def _read_bundle(raw: RawResponse) -> tuple[list[dict[str, Any]], set[str]]:
    """The bundle's documents and the accessions it skipped (a document answered 404)."""
    try:
        doc = json.loads(raw.body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ParseError(f"EDGAR bundle is not valid JSON: {type(exc).__name__}") from exc
    if not isinstance(doc, dict) or doc.get("bundle") != BUNDLE_VERSION:
        raise ParseError(f"EDGAR body is not a {BUNDLE_VERSION} bundle")
    if doc.get("incomplete"):
        raise ParseError(f"EDGAR bundle is incomplete: {doc['incomplete']}")
    docs = doc.get("documents")
    if not isinstance(docs, list) or not all(
        isinstance(d, dict) and isinstance(d.get("role"), str) and isinstance(d.get("text"), str)
        for d in docs
    ):
        raise ParseError("EDGAR bundle documents are malformed")
    skipped = doc.get("skipped") or []
    if not isinstance(skipped, list) or not all(
        isinstance(d, dict) and isinstance(d.get("accession"), str) for d in skipped
    ):
        raise ParseError("EDGAR bundle skipped list is malformed")
    return docs, {d["accession"] for d in skipped}


def _one(docs: list[dict[str, Any]], role: str) -> dict[str, Any]:
    found = [d for d in docs if d["role"] == role]
    if len(found) != 1:
        raise ParseError(f"EDGAR bundle needs exactly one {role} document, has {len(found)}")
    return found[0]


def _info_table_name(index: Any, primary_document: str) -> str | None:
    """The information table's file name from a filing ``index.json``."""
    items = index.get("directory", {}).get("item") if isinstance(index, dict) else None
    if not isinstance(items, list):
        return None
    primary = _strip_xsl(primary_document).lower()
    names = [
        str(i.get("name"))
        for i in items
        if isinstance(i, dict) and str(i.get("name", "")).lower().endswith(".xml")
    ]
    candidates = [n for n in names if n.lower() not in (primary, "primary_doc.xml")]
    info = [n for n in candidates if "info" in n.lower()]
    picked = info or candidates
    return picked[0] if picked else None


def _json(obj: dict[str, Any]) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


# ---------------------------------------------------------------------------------------------
# Form 4
# ---------------------------------------------------------------------------------------------


def _flag(text: str | None) -> bool:
    return (text or "").strip().lower() in _TRUE


def _role(rel: ET.Element | None) -> str | None:
    parts: list[str] = []
    if _flag(_text(rel, "isDirector")):
        parts.append("Director")
    if _flag(_text(rel, "isOfficer")):
        title = _text(rel, "officerTitle")
        parts.append(f"Officer: {title}" if title else "Officer")
    if _flag(_text(rel, "isTenPercentOwner")):
        parts.append("10% Owner")
    if _flag(_text(rel, "isOther")):
        other = _text(rel, "otherText")
        parts.append(f"Other: {other}" if other else "Other")
    return ", ".join(parts) or None


def side_of(code: str) -> str:
    """``P`` -> buy, ``S`` -> sell, every other code -> other."""
    return {"P": TradeSide.BUY.value, "S": TradeSide.SELL.value}.get(code, TradeSide.OTHER.value)


@dataclass(frozen=True)
class Form4:
    """The parts of one Form 4 that become rows."""

    owner: str
    owner_cik: str | None
    role: str | None
    period: date | None
    original_submission: date | None
    trades: list[dict[str, Any]]
    other_owners: list[str]
    skipped: int

    def rows(
        self, base: dict[str, Any], amends: str | None, candidates: list[str] | None = None
    ) -> list[dict[str, Any]]:
        """Seq 0 filing row and one row per trade. ``candidates`` is set for a Form 4/A that
        matched no single original (see :func:`_form4_original`)."""
        party = {"party": self.owner, "party_cik": self.owner_cik, "party_role": self.role}
        info: dict[str, Any] = {
            "other_owners": self.other_owners,
            "skipped_transactions": self.skipped,
            "date_of_original_submission": self.original_submission,
        }
        if candidates is not None:
            info.update(amendment_unmatched=True, amends_candidates=candidates)
        head = {
            **base,
            **party,
            "period": self.period or base["period"],
            "seq": 0,
            "row_kind": FilingRowKind.FILING.value,
            "amends_accession": amends,
            "details_json": _json(info),
        }
        out = [head]
        for i, trade in enumerate(self.trades, start=1):
            out.append(
                {
                    **base,
                    **party,
                    "period": self.period or base["period"],
                    "seq": i,
                    "row_kind": FilingRowKind.INSIDER_TRADE.value,
                    **trade,
                }
            )
        return out


def parse_form4(text: str, filing: Filing) -> Form4:
    """Owner, relationship and non-derivative transactions of a Form 4 / 4/A XML."""
    root = _xml(text)
    if _local(root.tag) != "ownershipDocument":
        raise ParseError(f"Form 4 {filing.accession}: root is {_local(root.tag)!r}")
    owners = _children(root, "reportingOwner")
    if not owners:
        raise ParseError(f"Form 4 {filing.accession}: no reportingOwner")
    first = owners[0]
    name = _text(first, "reportingOwnerId", "rptOwnerName")
    if name is None:
        raise ParseError(f"Form 4 {filing.accession}: reporting owner has no name")
    owner_cik_text = _text(first, "reportingOwnerId", "rptOwnerCik")
    owner_cik = pad_cik(owner_cik_text) if owner_cik_text and owner_cik_text.isdigit() else None
    footnotes = {
        f.get("id", ""): " ".join("".join(f.itertext()).split())
        for f in _descendants(root, "footnote")
    }
    box = _flag(_text(root, "aff10b5One"))
    trades: list[dict[str, Any]] = []
    skipped = 0
    for tx in _descendants(root, "nonDerivativeTransaction"):
        code = _text(tx, "transactionCoding", "transactionCode")
        trade_date = _day(_value(tx, "transactionDate"), "transactionDate")
        shares = _float(_value(tx, "transactionAmounts", "transactionShares"), "shares")
        if code is None or trade_date is None or shares is None:
            skipped += 1  # recorded on the filing row; never a half-filled trade row
            continue
        price = _float(_value(tx, "transactionAmounts", "transactionPricePerShare"), "price")
        refs = [e.get("id", "") for e in _descendants(tx, "footnoteId")]
        plan = any(_PLAN_RE.search(footnotes.get(r, "")) for r in refs) or (
            box and code in ("P", "S")
        )
        trades.append(
            {
                "trade_date": trade_date,
                "trade_code": code,
                "side": side_of(code),
                "shares": shares,
                "price": price,
                "value_usd": shares * price if price is not None else None,
                "plan_10b5_1": bool(plan),
                "details_json": _json(
                    {
                        "security_title": _value(tx, "securityTitle"),
                        "acquired_disposed": _value(
                            tx, "transactionAmounts", "transactionAcquiredDisposedCode"
                        ),
                        "ownership": _value(tx, "ownershipNature", "directOrIndirectOwnership"),
                        "shares_after": _value(
                            tx, "postTransactionAmounts", "sharesOwnedFollowingTransaction"
                        ),
                    }
                ),
            }
        )
    others = [n for o in owners[1:] if (n := _text(o, "reportingOwnerId", "rptOwnerName"))]
    return Form4(
        owner=name,
        owner_cik=owner_cik,
        role=_role(_child(first, "reportingOwnerRelationship")),
        period=_day(_text(root, "periodOfReport"), "periodOfReport"),
        original_submission=_day(_text(root, "dateOfOriginalSubmission"), "original date"),
        trades=trades,
        other_owners=others,
        skipped=skipped,
    )


def _skipped_originals(
    filing: Filing, form4: Form4, filings: list[Filing], skipped: set[str]
) -> list[str]:
    """Form 4s skipped this run (their document answered 404) that could be ``filing``'s
    original: filed on its ``dateOfOriginalSubmission``, or, when it gives none, filed no later
    than the amendment. The owner cannot be checked without the document, so this errs wide."""
    on_date = form4.original_submission
    return sorted(
        f.accession
        for f in filings
        if f.accession in skipped
        and f.form == "4"
        and f.accession != filing.accession
        and (f.filed == on_date if on_date is not None else f.filed <= filing.filed)
    )


def _form4_original(
    filing: Filing, form4: Form4, originals: list[tuple[Filing, Form4]]
) -> tuple[str | None, list[str] | None]:
    """``(accession it restates, None)``, or ``(None, candidates)`` when that is not one filing.

    The original is the one Form 4 of the same reporting owner filed on the amendment's
    ``dateOfOriginalSubmission``; failing that, the one Form 4 of that owner, filed no later
    than the amendment, either on that date or with the same ``periodOfReport``. Otherwise
    ``candidates`` lists those possible originals (possibly none): the reader drops them, so
    the amendment is counted alone and never next to its original (PR #102 review).
    """

    def same_owner(p: Form4) -> bool:
        return p.owner_cik == form4.owner_cik if form4.owner_cik else p.owner == form4.owner

    mine = [(f, p) for f, p in originals if f.accession != filing.accession and same_owner(p)]
    on_date = form4.original_submission
    strict = [f.accession for f, _ in mine if on_date is not None and f.filed == on_date]
    if len(strict) == 1:
        return strict[0], None
    loose = sorted(
        {
            f.accession
            for f, p in mine
            if f.filed <= filing.filed
            and (
                (on_date is not None and f.filed == on_date)
                or (form4.period is not None and p.period == form4.period)
            )
        }
    )
    if len(loose) == 1:
        return loose[0], None
    return None, loose


# ---------------------------------------------------------------------------------------------
# 13F
# ---------------------------------------------------------------------------------------------


@dataclass
class _PeriodHoldings:
    holdings: dict[str, tuple[float, float, str | None]]  # cusip -> (shares, value, class)
    carrier: Filing | None  # the filing whose accession carries the period's rows
    amends: str | None  # the original a restatement carrier replaces
    merged: list[str]  # NEW HOLDINGS amendments added in
    amendment_types: dict[str, str | None]
    by_accession: dict[str, Filing]


def _positions(text: str) -> dict[str, tuple[float, float, str | None]]:
    root = _xml(text)
    out: dict[str, tuple[float, float, str | None]] = {}
    for row in _descendants(root, "infoTable"):
        cusip = (_text(row, "cusip") or "").upper()
        kind = (_text(row, "shrsOrPrnAmt", "sshPrnamtType") or "").upper()
        if not cusip or kind != "SH" or _text(row, "putCall"):
            continue  # principal amounts and options are not share positions
        shares = _float(_text(row, "shrsOrPrnAmt", "sshPrnamt"), "13F shares") or 0.0
        value = _float(_text(row, "value"), "13F value") or 0.0
        sh, val, title = out.get(cusip, (0.0, 0.0, None))
        out[cusip] = (sh + shares, val + value, title or _text(row, "titleOfClass"))
    return out


def _amendment_type(cover: str | None) -> str | None:
    if cover is None:
        return None
    root = _xml(cover)
    for e in _descendants(root, "amendmentType"):
        if e.text and e.text.strip():
            return e.text.strip().upper()
    return None


def _add(
    into: dict[str, tuple[float, float, str | None]],
    more: dict[str, tuple[float, float, str | None]],
) -> None:
    for cusip, (sh, val, title) in more.items():
        a, b, t = into.get(cusip, (0.0, 0.0, None))
        into[cusip] = (a + sh, b + val, t or title)


def _period_holdings(
    filings: list[Filing],
    period: date | None,
    tables: dict[str, str],
    covers: dict[str, str],
) -> _PeriodHoldings:
    """Holdings of one 13F period: the latest full restatement if any (else the original),
    plus every NEW HOLDINGS amendment filed after it."""
    same = sorted(
        (f for f in filings if f.form in FORM13F and f.period == period),
        key=lambda f: (f.filed, f.accession),
    )
    for f in same:
        if f.accession not in tables:
            raise ParseError(f"13F {f.accession}: information table missing from the bundle")
    types = {f.accession: _amendment_type(covers.get(f.accession)) for f in same}
    for f in same:
        if f.form == "13F-HR/A" and types[f.accession] not in (RESTATEMENT, NEW_HOLDINGS):
            raise ParseError(f"13F {f.accession}: amendment type {types[f.accession]!r} unknown")
    originals = [f for f in same if f.form == "13F-HR"]
    restated = [f for f in same if types[f.accession] == RESTATEMENT]
    carrier = restated[-1] if restated else (originals[-1] if originals else None)
    if carrier is None and same:
        carrier = same[0]
    holdings: dict[str, tuple[float, float, str | None]] = {}
    merged: list[str] = []
    if carrier is not None:
        _add(holdings, _positions(tables[carrier.accession]))
        for f in same:
            if types[f.accession] == NEW_HOLDINGS and (f.filed, f.accession) > (
                carrier.filed,
                carrier.accession,
            ):
                _add(holdings, _positions(tables[f.accession]))
                merged.append(f.accession)
    amends = None
    if carrier is not None and types.get(carrier.accession) == RESTATEMENT and originals:
        amends = originals[-1].accession
    return _PeriodHoldings(
        holdings=holdings,
        carrier=carrier,
        amends=amends,
        merged=merged,
        amendment_types=types,
        by_accession={f.accession: f for f in same},
    )


register(SOURCE, EdgarSource)
