"""The morning note, every number checked (P-024, card T66; was stretch card T50).

Claude receives only the brief's own model (the read, the movers, Check before using and the
snapshot) and writes four or five plain sentences. The prompt forbids advice, signals, targets
and predictions. The note is written once, by the morning job's report step, and stored in the
brief's ``model.json``; the dashboard shows that stored note and never calls the API.

The check (:func:`check`) fails closed (PR #102 review). A sentence is dropped, and the reason
logged, when:

* any number in it does not match a value the model shows with the same unit, written no more
  precisely than the brief shows it (``1.6%`` for a shown ``−1.6%``; never ``1.64%``, and never
  ``2.4%`` for a shown ``2%``);
* an explicit sign differs from the shown one, or the nearest direction word before a number
  ("rose", "fell", "widened", "narrowed", ...) disagrees with the sign of the change it matches;
* a number cannot be read: a digit inside a word, an unknown suffix ("37bpz"), a unit the brief
  never uses ("$40m", "3x"), or a number written as a word ("fifteen");
* it holds a forbidden word (``brief.note.forbidden_words``), matched as a stem ("selling",
  "expected"), with "sell-off", "buyback", "buyers" and "sellers" allowed.

The numbers a note may use are the market values only (mover and snapshot values, changes and
sigma, and the unit-carrying figures in Check before using): never a date, a clock time or the
brief's own time. Market names with digits ("S&P 500", "10Y", "2-year") are names, not numbers;
"40 years" or "7 sessions" is a count and is checked. The kept sentences show "every number
checked (n of n)".

No note, and the brief says "No note today" with the reason, when: the note is turned off, no
API key is set (``brief.note.api_key_env``; the key is read from the environment and never
logged), the month's spend would pass ``brief.note.monthly_cap_usd``, the API errors or times
out, the reply is empty, declined or cut at the token limit, or no sentence survives the check.
Each call's cost is written to a spend ledger (``reports/note_spend/month=YYYY-MM/``, by the
call's ET month) the moment it returns; a timeout counts the worst case. A call is made only
when the worst case (``max_output_tokens`` at the output price) still fits the cap.

Hosted mode (the public dashboard shows the stored note): the prompt never holds a value of a
tier the hosted dashboard hides (tier 1 always, plus ``dashboard.hosted_hidden_tiers``), so the
note cannot carry one either.
"""
# ruff: noqa: RUF001, RUF002  (the sigma and minus signs are intentional output)

from __future__ import annotations

import json
import re
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Protocol, cast

from deskbrief.config import Config, NoteSettings
from deskbrief.ids import new_id
from deskbrief.logging import get_logger
from deskbrief.report.model import BriefModel, Note
from deskbrief.store.backend import REPORTS_PREFIX, StorageBackend
from deskbrief.timeutil import ensure_utc, to_et, utc_now

#: tiers the hosted dashboard never shows, whatever config says (mirrors app/components/data.py)
ALWAYS_HIDDEN = frozenset({1})

#: a number token: optional sign, optional $, digits (grouped or not, or a leading ".5"),
#: decimals, then an optional unit. Units the brief never shows (bps is bp; m, bn, k, x are
#: suffixes no shown value carries) are read so they fail, never skipped.
_NUMBER = re.compile(
    r"(?P<sign>[+\-−])?(?P<usd>\$)?(?P<num>(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?|\.\d+)"
    r"(?:\s?(?P<unit>%|percent(?:age points?)?\b|bps\b|bp\b|basis points?\b|pp\b|pts\b|"
    r"points?\b|σ|sigma\b|contracts\b|x\b|bn\b|m\b|k\b))?",
    re.IGNORECASE,
)
_UNITS = {
    "%": "pct", "percent": "pct", "bp": "bp", "bps": "bp", "basis point": "bp",
    "basis points": "bp", "pp": "pp", "percentage point": "pp", "percentage points": "pp",
    "pts": "pts", "point": "pts", "points": "pts", "σ": "sigma", "sigma": "sigma",
    "contracts": "contracts", "x": "times", "bn": "billion", "m": "million", "k": "thousand",
}  # fmt: skip
#: market names with digits in them (tenors such as "2-year" or "10Y", index names); a plain
#: "40 years" or "7 sessions" is a count, not a name, and is checked like any number
_NAMES = re.compile(
    r"S&P\s?500|Nasdaq[- ]100|Russell\s2000|Nikkei\s225|FTSE\s100|DAX\s40|"
    r"\b\d{1,2}-(?:year|month|week)\b|\b\d{1,2}(?-i:[YM])\b|\b\d+s\d+s\b",
    re.IGNORECASE,
)
#: numbers written as words: a sentence holding one fails (they cannot be checked)
_NUMBER_WORDS = re.compile(
    r"\b(?:zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|"
    r"fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|"
    r"seventy|eighty|ninety|hundreds?|thousands?|millions?|billions?|trillions?|dozens?|"
    r"half|double[ds]?|triple[ds]?|twice|thrice)\b",
    re.IGNORECASE,
)
#: dates and clock times inside free text (Check before using) are never market values
_DATES = re.compile(
    r"\b\d{1,2}:\d{2}\b|\b\d{1,2}\s(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\b|"
    r"\b(?:19|20)\d{2}\b"
)
#: words that give a move its direction; the nearest one before a number in its clause must
#: agree with the sign of the shown change it matches
_UP = re.compile(
    r"\b(?:rose|rise[sn]?|rising|gained|gains?|climbed|climbs?|jumped|jumps?|widened|widens?|"
    r"higher|increased|increases?|advanced|up)\b",
    re.IGNORECASE,
)
_DOWN = re.compile(
    r"\b(?:fell|falls?|falling|fallen|dropped|drops?|declined|declines?|slipped|slips?|slid|"
    r"narrowed|narrows?|tightened|tightens?|lower|decreased|decreases?|lost|down)\b",
    re.IGNORECASE,
)
_CLAUSE = re.compile(r"[,;:]|\s(?:and|but|while|against|versus|vs)\s", re.IGNORECASE)
_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9$+\-−\"'(])")
_MINUS = "−"
_PLACEHOLDER = " \u2063 "  # an invisible separator standing in for a masked name


@dataclass(frozen=True)
class Shown:
    """A number: its absolute value as written, unit, decimals and explicit sign (or None)."""

    value: Decimal
    unit: str  # pct | bp | pp | pts | sigma | usd | contracts | "" (a bare level) | other
    decimals: int
    sign: str | None = None  # "+" | "-" | None (not written)


@dataclass(frozen=True)
class _Token:
    number: Shown
    start: int
    end: int


def _tokens(text: str) -> tuple[list[_Token], bool]:
    """The number tokens of ``text`` (names masked first) and whether every digit in it was
    read as part of a clean token (False: a digit sits inside a word or an unknown suffix)."""
    masked = _NAMES.sub(_PLACEHOLDER, text)
    out: list[_Token] = []
    clean = True
    for m in _NUMBER.finditer(masked):
        before = masked[m.start() - 1] if m.start() > 0 else " "
        after = masked[m.end()] if m.end() < len(masked) else " "
        if before.isalnum() or before in "._" or after.isalnum() or after in "%_":
            clean = False  # "37bpz", "x10", "1.2.3": unreadable, never skipped
            continue
        raw = m["num"].replace(",", "")
        dec = raw.split(".", 1)[1] if "." in raw else ""
        unit = _UNITS.get((m["unit"] or "").lower(), "") or ("usd" if m["usd"] else "")
        sign = m["sign"].replace(_MINUS, "-") if m["sign"] else None
        out.append(_Token(Shown(Decimal(raw), unit, len(dec), sign), m.start(), m.end()))
    rest = "".join(ch for i, ch in enumerate(masked) if not any(t.start <= i < t.end for t in out))
    return out, clean and not any(ch.isdigit() for ch in rest)


def numbers(text: str) -> list[Shown]:
    """Every readable number in ``text`` with its unit and sign, names with digits left out."""
    return [t.number for t in _tokens(text)[0]]


def _direction(text: str, token: _Token, previous_end: int) -> str | None:
    """ "+" or "-" from the nearest direction word before ``token`` in its clause, or None."""
    segment = text[previous_end : token.start]
    cut = list(_CLAUSE.finditer(segment))
    if cut:
        segment = segment[cut[-1].end() :]
    hits = [(m.end(), "+") for m in _UP.finditer(segment)]
    hits += [(m.end(), "-") for m in _DOWN.finditer(segment)]
    return max(hits)[1] if hits else None


def _matches(n: Shown, shown: Iterable[Shown], direction: str | None = None) -> bool:
    """``n`` is a shown number of the same unit written at most as precisely as the brief
    shows it (``1.6%`` for a shown ``−1.6%``; never ``1.64%`` or ``2.4%`` for ``2%``), with an
    explicit sign equal to the shown one, and a direction word agreeing with a signed change."""
    for s in shown:
        if s.unit != n.unit or n.decimals > s.decimals:
            continue
        step = Decimal(1).scaleb(-n.decimals)
        if s.value.quantize(step, rounding=ROUND_HALF_UP) != n.value:
            continue
        if n.sign is not None and n.sign != s.sign:
            continue
        if direction is not None and s.sign is not None and s.sign != direction:
            continue
        return True
    return False


#: words that look like a forbidden stem but describe the market, not advice
_ALLOWED_WORDS = re.compile(r"\b(?:sell-?offs?|buybacks?|sellers|buyers)\b", re.IGNORECASE)


def _forbidden(sentence: str, words: Collection[str]) -> str | None:
    """The first forbidden word or phrase in ``sentence``, matched as a word stem ("sell" finds
    "selling", "expect" finds "expected"); a phrase may hold one extra word ("will likely
    rise"). "sell-off", "buyback", "buyers" and "sellers" are allowed."""
    low = _ALLOWED_WORDS.sub(" ", sentence.lower())
    for w in words:
        parts = [re.escape(p) for p in w.split()]
        pattern = r"\b" + r"\s+(?:[a-z]+\s+)?".join(parts) + r"[a-z]*\b"
        if re.search(pattern, low):
            return w
    return None


@dataclass(frozen=True)
class Checked:
    """What survived the check, and why each dropped sentence went."""

    kept: tuple[str, ...]
    n_numbers: int  # numbers in the kept sentences (all matched)
    dropped: tuple[str, ...]


def sentences(text: str) -> list[str]:
    out = []
    for line in text.replace("\r", "").split("\n"):
        line = line.strip().lstrip("-•* ").strip()
        out += [s.strip() for s in _SENTENCE.split(line) if s.strip()]
    return out


def _mask_names(sentence: str, names: Collection[str]) -> str:
    for name in sorted(names, key=len, reverse=True):
        if name and any(ch.isdigit() for ch in name):
            sentence = sentence.replace(name, _PLACEHOLDER)
    return sentence


def _why_not(sentence: str, shown: Collection[Shown], names: Collection[str]) -> str | None:
    """Why ``sentence`` fails the number check, or None when every number in it matches."""
    words = _NUMBER_WORDS.search(sentence)
    if words:
        return f"number word {words.group(0)!r} cannot be checked"
    text = _mask_names(sentence, names)
    tokens, clean = _tokens(text)
    if not clean:
        return "a number it holds cannot be read"
    masked = _NAMES.sub(_PLACEHOLDER, text)
    end = 0
    for t in tokens:
        n = t.number
        if not _matches(n, shown, _direction(masked, t, end)):
            sign = n.sign or ""
            unit = f" {n.unit}" if n.unit else ""
            return f"number {sign}{n.value}{unit} matches no value in the brief"
        end = t.end
    return None


def check(
    reply: str,
    shown: Collection[Shown],
    forbidden: Collection[str],
    limit: int,
    names: Collection[str] = (),
) -> Checked:
    """Keep the sentences whose every number matches a shown number (fail closed: a number the
    check cannot read drops the sentence) and that hold no forbidden word; at most ``limit``.
    ``names`` are market names whose digits are not numbers ("S&P fut", "10Y real")."""
    kept: list[str] = []
    dropped: list[str] = []
    total = 0
    for s in sentences(reply):
        word = _forbidden(s, forbidden)
        if word is not None:
            dropped.append(f"forbidden word {word!r}")
            continue
        why = _why_not(s, shown, names)
        if why is not None:
            dropped.append(why)
            continue
        if len(kept) >= limit:
            dropped.append("over the sentence limit")
            continue
        kept.append(s)
        total += len(_tokens(_mask_names(s, names))[0])
    return Checked(tuple(kept), total, tuple(dropped))


# ---------------------------------------------------------------------------
# the facts Claude receives, and the numbers they show
# ---------------------------------------------------------------------------


def hidden_tiers(config: Config) -> frozenset[int]:
    """Tiers the hosted dashboard hides (where the stored note is shown)."""
    return ALWAYS_HIDDEN | frozenset(config.dashboard.hosted_hidden_tiers)


def facts(model: BriefModel, hidden: Collection[int]) -> dict[str, Any]:
    """The brief's model as the note's only input: no value of a hidden tier, no lineage."""
    tiers = {r.instrument_id: r.tier for r in model.rows()}
    visible = {i for i, t in tiers.items() if t is not None and t not in hidden}
    read = model.read
    return {
        "brief": to_et(model.report_time).strftime("%a %d %b %Y, %H:%M ET"),
        "read": read.text if read is not None else "",
        "rules_met": [r.label for r in read.met] if read is not None else [],
        "moved_more_than_usual": [
            {
                "market": m.short,
                "name": m.name,
                "value": m.value_text,
                "change": m.change_text,
                "sigma": f"{m.sigma:.1f}σ",
                "basis": m.basis,
            }
            for m in model.movers
            if m.instrument_id in visible
        ],
        "check_before_using": [
            {"item": c.title, "text": c.text}
            for c in model.caveats
            if all(i in visible for i in c.instrument_ids)
        ],
        "snapshot": [
            {"market": r.label, "value": r.value_text, "change": r.change_text, "note": r.meta}
            for g in model.snapshot
            for r in g.rows
            if r.instrument_id in visible
        ],
    }


def shown_numbers(payload: Mapping[str, Any]) -> list[Shown]:
    """The numbers a note may use: the market values the facts show (each mover's value,
    change and sigma; each snapshot row's value and change) and the unit-carrying numbers in
    Check before using, with dates and clock times masked. Never a date, a time, a basis note
    or the brief's own time (PR #102 review B3)."""
    out: list[Shown] = []
    for m in payload.get("moved_more_than_usual", ()):
        for key in ("value", "change", "sigma"):
            out += numbers(str(m.get(key) or ""))
    for r in payload.get("snapshot", ()):
        for key in ("value", "change"):
            out += numbers(str(r.get(key) or ""))
    for c in payload.get("check_before_using", ()):
        out += [n for n in numbers(_DATES.sub(" ", str(c.get("text") or ""))) if n.unit]
    return out


def market_names(payload: Mapping[str, Any]) -> list[str]:
    """Market names in the facts ("S&P fut", "10Y real"): digits in them are not numbers."""
    names: list[str] = []
    for group in ("moved_more_than_usual", "snapshot"):
        for row in payload.get(group, ()):
            names += [str(row.get(k) or "") for k in ("market", "name")]
    for c in payload.get("check_before_using", ()):
        names += [p.strip() for p in str(c.get("item") or "").split(" vs ")]
    return [n for n in dict.fromkeys(names) if n]


def prompt(settings: NoteSettings) -> str:
    words = ", ".join(f'"{w}"' for w in settings.forbidden_words)
    n = settings.max_sentences
    return (
        "You write the morning note for Desk Brief, a markets data brief read by a hedge-fund "
        f"desk. Write {max(1, n - 1)} or {n} short sentences in plain English about what moved "
        "overnight and what to check, using only the facts in the user message.\n"
        "Rules:\n"
        "- Every number you write must appear in the facts exactly as shown there, with the "
        "same unit (%, bp, pts, σ, $). Write no other number: no dates, times, years or counts.\n"
        "- Describe what happened. Give no advice, signals, recommendations, price targets, "
        "forecasts or predictions, and never say what a market will or should do.\n"
        f"- Do not use these words: {words}.\n"
        "- One sentence per line. No headings, lists, markdown or preamble."
    )


# ---------------------------------------------------------------------------
# spend and the call
# ---------------------------------------------------------------------------


class _Messages(Protocol):
    def create(self, **kwargs: Any) -> Any: ...


class ClientLike(Protocol):
    """What the note needs of ``anthropic.Anthropic`` (tests pass a fake)."""

    @property
    def messages(self) -> _Messages: ...


ClientFactory = Callable[[str, float], ClientLike]


def _sdk_client(api_key: str, timeout_s: float) -> ClientLike:
    import anthropic

    # one attempt: a retry after a timeout could double the spend and the wait
    return cast(ClientLike, anthropic.Anthropic(api_key=api_key, timeout=timeout_s, max_retries=0))


#: where each API call's cost is recorded the moment it returns, one file per call, by the
#: ET month of the call (not the brief's date, so a backfill counts in the month it ran)
SPEND_PREFIX = f"{REPORTS_PREFIX}/note_spend"


def spend_key(at: datetime, call_id: str) -> str:
    return f"{SPEND_PREFIX}/month={to_et(at):%Y-%m}/{call_id}.json"


def record_spend(store: StorageBackend, at: datetime, note: Note) -> str:
    """Write one call's cost to the spend ledger (write-once); returns the key."""
    key = spend_key(at, new_id())
    body = {"cost_usd": note.cost_usd, "model": note.model, "at": ensure_utc(at).isoformat()}
    store.put_bytes(key, json.dumps(body, sort_keys=True).encode())
    return key


def month_spend(store: StorageBackend, at: date | datetime) -> float:
    """USD spent on notes in ``at``'s calendar month (ET): the sum of the ledger files of that
    month. A file that cannot be parsed counts as zero."""
    month = f"{to_et(at):%Y-%m}" if isinstance(at, datetime) else f"{at:%Y-%m}"
    total = 0.0
    for key in store.list(f"{SPEND_PREFIX}/month={month}/"):
        if not key.endswith(".json"):
            continue
        try:
            total += float(json.loads(store.get_bytes(key)).get("cost_usd") or 0.0)
        except (ValueError, TypeError, AttributeError, OSError):
            continue
    return total


def _cost(settings: NoteSettings, input_tokens: int, output_tokens: int) -> float:
    return (
        input_tokens * settings.price_input_usd_per_mtok
        + output_tokens * settings.price_output_usd_per_mtok
    ) / 1_000_000


def _reply_text(response: Any) -> str:
    blocks = getattr(response, "content", None) or ()
    return "".join(getattr(b, "text", "") for b in blocks if getattr(b, "type", "") == "text")


def _usage(response: Any) -> tuple[int, int]:
    usage = getattr(response, "usage", None)
    return int(getattr(usage, "input_tokens", 0) or 0), int(getattr(usage, "output_tokens", 0) or 0)


def write_note(
    model: BriefModel,
    config: Config,
    env: Mapping[str, str],
    *,
    spent_usd: float = 0.0,
    client_factory: ClientFactory | None = None,
) -> Note:
    """The note for ``model``: one API call at most. ``spent_usd`` is this month's spend so far
    (:func:`month_spend`). :func:`with_note` guards it, so nothing here can cost the brief."""
    s = config.brief.note
    log = get_logger()
    if not s.enabled:
        return Note(reason="the note is turned off in config", model=s.model)
    key = (env.get(s.api_key_env) or "").strip()
    if not key:
        return Note(reason="no API key is configured", model=s.model)
    payload = facts(model, hidden_tiers(config))
    system, user = prompt(s), "Facts (JSON):\n" + json.dumps(payload, ensure_ascii=False)
    worst = _cost(s, (len(system) + len(user)) // 3 + 200, s.max_output_tokens)
    if spent_usd + worst > s.monthly_cap_usd:
        return Note(
            reason=f"the monthly spend cap of ${s.monthly_cap_usd:.2f} is reached", model=s.model
        )
    try:
        client = (client_factory or _sdk_client)(key, s.timeout_s)
        response = client.messages.create(
            model=s.model,
            max_tokens=s.max_output_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            thinking={"type": "adaptive"},
            output_config={"effort": s.effort},
        )
    except Exception as exc:  # the key is never logged; only the error's type
        kind = type(exc).__name__
        timed_out = isinstance(exc, TimeoutError) or "Timeout" in kind
        log.warning("note.failed", error=kind)
        if timed_out:  # the request may still be billed: count the worst case
            return Note(reason="the API timed out", model=s.model, cost_usd=worst)
        return Note(reason=f"the API returned an error ({kind})", model=s.model)
    cost = _cost(s, *_usage(response))
    stop = getattr(response, "stop_reason", "")
    if stop == "refusal":
        return Note(reason="the model declined to write it", model=s.model, cost_usd=cost)
    if stop == "max_tokens":  # config.NoteSettings: a reply cut at the ceiling is no note
        return Note(reason="the reply stopped at the token limit", model=s.model, cost_usd=cost)
    text = _reply_text(response).strip()
    if not text:
        return Note(reason="the reply was empty", model=s.model, cost_usd=cost)
    result = check(
        text, shown_numbers(payload), s.forbidden_words, s.max_sentences, market_names(payload)
    )
    for why in result.dropped:
        log.info("note.sentence_dropped", reason=why)
    if not result.kept:
        return Note(
            reason="no sentence passed the number check",
            dropped=result.dropped,
            model=s.model,
            cost_usd=cost,
        )
    return Note(
        sentences=result.kept,
        n_checked=result.n_numbers,
        n_numbers=result.n_numbers,
        dropped=result.dropped,
        model=s.model,
        cost_usd=cost,
    )


def with_note(
    model: BriefModel,
    config: Config,
    env: Mapping[str, str],
    store: StorageBackend,
    *,
    client_factory: ClientFactory | None = None,
    now: datetime | None = None,
) -> BriefModel:
    """``model`` with its note; never raises (the morning report step calls this once). A call's
    cost goes to the spend ledger the moment it returns, before anything else can fail."""
    at = ensure_utc(now) if now is not None else utc_now()
    log = get_logger()
    try:
        spent = month_spend(store, at)
    except Exception as exc:  # cannot tell the spend: do not call
        log.warning("note.spend_unknown", error=type(exc).__name__)
        return replace(model, note=Note(reason="this month's spend could not be read"))
    try:
        note = write_note(model, config, env, spent_usd=spent, client_factory=client_factory)
    except Exception as exc:  # an unexpected bug costs the note, never the brief or its email
        log.warning("note.unexpected", error=type(exc).__name__)
        note = Note(reason=f"the note could not be written ({type(exc).__name__})")
    if note.cost_usd > 0:
        try:
            record_spend(store, at, note)
        except Exception as exc:
            log.warning("note.spend_not_recorded", error=type(exc).__name__)
    return replace(model, note=note)


def stored_note(store: StorageBackend, report_date: date) -> Note | None:
    """The note stored with the newest archived brief of ``report_date`` (the dashboard reads
    this and never calls the API); None when no brief of that date is archived or it holds no
    note."""
    prefix = f"{REPORTS_PREFIX}/date={report_date.isoformat()}/"
    versions = []
    for key in store.list(prefix):
        m = re.match(r"v(\d+)/model\.json$", key[len(prefix) :])
        if m:
            versions.append((int(m[1]), key))
    if not versions:
        return None
    raw = json.loads(store.get_bytes(max(versions)[1])).get("note")
    if not isinstance(raw, Mapping):
        return None
    return Note(
        sentences=tuple(str(x) for x in raw.get("sentences") or ()),
        n_checked=int(raw.get("n_checked") or 0),
        n_numbers=int(raw.get("n_numbers") or 0),
        reason=raw.get("reason"),
        model=str(raw.get("model") or ""),
        cost_usd=float(raw.get("cost_usd") or 0.0),
    )


__all__ = [
    "Checked",
    "Shown",
    "check",
    "facts",
    "market_names",
    "month_spend",
    "numbers",
    "record_spend",
    "shown_numbers",
    "stored_note",
    "with_note",
    "write_note",
]
