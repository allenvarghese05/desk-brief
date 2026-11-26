"""The morning note, every number checked (P-024, card T66; was stretch card T50).

Claude receives only the brief's own model (the read, the movers, Check before using and the
snapshot) and writes four or five plain sentences. The prompt forbids advice, signals, targets
and predictions. The note is written once, by the morning job's report step, and stored in the
brief's ``model.json``; the dashboard shows that stored note and never calls the API.

The model never writes a number (round 3 of the PR #102 review). It receives each figure the
brief shows as a named slot (``{ES_F.chg_1d}``, ``{HY_OAS.level}``, ``{HY_OAS.sigma}``, and
``{ES_F.name}`` for a market's name), with what the slot shows for context, and writes the slot
wherever it means a value. :func:`check` replaces each slot with the brief's exact display
string, and drops a sentence (logging why) when:

* it holds an unknown slot;
* its own words, outside slots, hold a numeric character of any script (digits, "½"), a percent,
  per-mille, sigma or currency sign, a unit word ("percent", "bp", "points"), or a number,
  fraction or period word ("two", "dozen", "half", "third", "quarter", "decade");
* a change slot disagrees with a direction word anywhere in its clause (rise, rally, surge, add,
  climb, higher, widen, steepen, firm ... for a fall; fall, drop, slip, lower, narrow, tighten,
  flatten ... for a rise; any direction for a change of zero);
* a value slot sits in a clause that names another market, or in a sentence that never names
  its market (by its name slot, the brief's names, or a plain alias such as "Brent");
* it holds a forbidden word (``brief.note.forbidden_words``, which include hedged forecasts such
  as "likely", "could", "may"), matched as a stem, with "sell-off", "buyback", "buyers" and
  "sellers" allowed.

"Every number checked (n of n)" counts the value slots filled in the kept sentences.

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
# ruff: noqa: RUF001, RUF003  (the sigma and minus signs are intentional output)

from __future__ import annotations

import json
import re
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Any, Protocol, cast

from deskbrief.config import Config, NoteSettings
from deskbrief.ids import new_id
from deskbrief.logging import get_logger
from deskbrief.report.model import NOTE_NO_KEY, NOTE_OFF, SHORT_LABELS, BriefModel, Note
from deskbrief.store.backend import REPORTS_PREFIX, StorageBackend
from deskbrief.timeutil import ensure_utc, to_et, utc_now

#: tiers the hosted dashboard never shows, whatever config says (mirrors app/components/data.py)
ALWAYS_HIDDEN = frozenset({1})

#: a slot the model writes wherever it means a number or a market's name (round 3 of the
#: PR #102 review: the model never writes a number itself)
_SLOT = re.compile(r"\{([A-Za-z0-9_]+)\.([A-Za-z0-9_]+)\}")  # any slot; unknown ones fail
#: what may never appear in the model's own words, outside slots: a numeric character (any
#: script: digits, ½, superscripts), a percent, per-mille or sigma sign, a unit word, or a number,
#: fraction or period word
_NUMBER_WORDS = re.compile(
    r"\b(?:zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|"
    r"fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|"
    r"seventy|eighty|ninety|hundreds?|thousands?|millions?|billions?|trillions?|dozens?|"
    r"halfs?|halves|halved|thirds?|quarters?|fifths?|sixths?|sevenths?|eighths?|ninths?|"
    r"tenths?|decades?|centur(?:y|ies)|twice|thrice|double[ds]?|triple[ds]?|quadruple[ds]?|"
    r"percent|percentage|basis|bps?|pp|pts?|points?|sigma|single-digit|double-digit|"
    # spans and dates claim a history the brief does not hold ("for a week", "since March")
    r"days|weeks?|fortnights?|months?|years?|sessions|since|"
    r"January|February|March|April|June|July|August|September|October|November|December|"
    r"II|III|IV|VI|VII|VIII|IX|XI|XII|XIII|XIV|XV|XX)\b",
    re.IGNORECASE,
)
_SIGNS = ("%", "‰", "σ", "Σ", "$", "€", "£", "¥")
#: direction words, by stem; "widen" and "steepen" mean up, "narrow", "tighten" and "flatten"
#: mean down (spreads and curves). A change slot must agree with every one in its clause.
_UP = re.compile(
    r"\b(?:ris(?:e|es|en|ing)|rose|gain\w*|rall\w*|surg\w*|add(?:s|ed|ing)?|climb\w*|jump\w*|"
    r"higher|up|upward\w*|advanc\w*|widen\w*|steepen\w*|firm(?:s|ed|ing)?|strengthen\w*|"
    r"soar\w*|spik\w*|rebound\w*|recover\w*|increas\w*|bounc\w*|climbs?|lift\w*|grew|grow\w*)\b",
    re.IGNORECASE,
)
_DOWN = re.compile(
    r"\b(?:fell|fall(?:s|en|ing)?|drop\w*|declin\w*|slip\w*|slid(?:e|es|ing)?|narrow\w*|"
    r"tighten\w*|flatten\w*|lower|down|downward\w*|decreas\w*|los(?:s|t|es|ing)|sank|sink\w*|"
    r"sunk|tumbl\w*|plung\w*|slump\w*|retreat\w*|eas(?:e|ed|es|ing)|weaken\w*|soften\w*|"
    r"dip(?:s|ped|ping)?|shed\w*|sell-?off|slipped)\b",
    re.IGNORECASE,
)
_CLAUSE = re.compile(
    r"[,;:—–]|\s(?:and|but|while|whereas|against|versus|vs\.?|as|though|although)\s",
    re.IGNORECASE,
)
_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z{\"'(])")
_MINUS = "−"


@dataclass(frozen=True)
class Slot:
    """One value the note may use: the brief's exact display string and whose it is."""

    key: str  # "{ES_F.chg_1d}"
    instrument_id: str
    kind: str  # name | level | chg_1d | sigma
    shows: str  # "−2.7%", "79.33", "3.6σ", "S&P futures"
    sign: str | None = None  # "+" | "-" for a signed change; None otherwise (zero, level, ...)
    what: str = ""  # context for the model ("change since previous NYSE close (19 Nov)")


#: plain-word aliases of markets a sentence may name in its own words; the brief's own names
#: (short label and full name) are added from the model
_ALIASES: Mapping[str, tuple[str, ...]] = {
    "ES_F": ("S&P futures", "S&P 500 futures", "S&P", "equity futures", "stock futures"),
    "HY_OAS": ("high-yield spreads", "high-yield spread", "credit spreads", "credit spread",
               "high yield", "junk"),
    "VIX": ("VIX", "volatility"),
    "BRENT": ("Brent",),
    "WTI": ("WTI",),
    "GOLD_F": ("gold",),
    "GLD": ("GLD",),
    "EURUSD": ("euro",),
    "USDJPY": ("yen",),
    "GBPUSD": ("sterling", "pound"),
    "N225": ("Nikkei",),
    "FTSE": ("FTSE",),
    "DAX": ("DAX",),
}  # fmt: skip


def _display_name(row_name: str) -> str:
    """A market's name for prose: the brief's full name without a parenthetical."""
    return re.sub(r"\s*\([^)]*\)", "", row_name).strip()


def slots(model: BriefModel, hidden: Collection[int]) -> dict[str, Slot]:
    """Every value the note may use, keyed by slot: each visible mover's name, level, change and
    sigma, and each visible snapshot row's name, level and change. Nothing of a tier the hosted
    dashboard hides (the stored note is shown there)."""
    rows = {r.instrument_id: r for r in model.rows()}
    visible = {i for i, r in rows.items() if r.tier is not None and r.tier not in hidden}
    out: dict[str, Slot] = {}

    def add(iid: str, kind: str, shows: str, what: str = "") -> None:
        if not shows:
            return
        sign = None
        if kind == "chg_1d":
            sign = "+" if shows.startswith("+") else "-" if shows.startswith(_MINUS) else None
        key = f"{{{iid}.{kind}}}"
        out.setdefault(key, Slot(key, iid, kind, shows, sign, what))

    for m in model.movers:
        if m.instrument_id not in visible:
            continue
        add(m.instrument_id, "name", _display_name(m.name))
        add(m.instrument_id, "level", m.value_text, "latest value")
        add(m.instrument_id, "chg_1d", m.change_text, f"change since {m.basis}")
        add(m.instrument_id, "sigma", f"{m.sigma:.1f}σ", "size against its own recent daily moves")
    for g in model.snapshot:
        for r in g.rows:
            if r.instrument_id not in visible:
                continue
            row = rows.get(r.instrument_id)
            add(r.instrument_id, "name", _display_name(row.name) if row else r.label)
            add(r.instrument_id, "level", r.value_text, r.meta)
            add(r.instrument_id, "chg_1d", r.change_text, "change")
    return out


def facts(model: BriefModel, hidden: Collection[int]) -> dict[str, Any]:
    """The note's only input: the read, the rules met, Check before using (words only) and the
    slots with what each shows. No value of a hidden tier, no lineage."""
    tiers = {r.instrument_id: r.tier for r in model.rows()}
    visible = {i for i, t in tiers.items() if t is not None and t not in hidden}
    read = model.read
    return {
        "brief": to_et(model.report_time).strftime("%a %d %b %Y, %H:%M ET"),
        "read": read.text if read is not None else "",
        "rules_met": [r.label for r in read.met] if read is not None else [],
        "moved_more_than_usual": [
            m.instrument_id for m in model.movers if m.instrument_id in visible
        ],
        "check_before_using": [
            {"item": c.title, "text": c.text}
            for c in model.caveats
            if all(i in visible for i in c.instrument_ids)
        ],
        "slots": [
            {"slot": sl.key, "market": sl.instrument_id, "shows": sl.shows, "what": sl.what}
            for sl in slots(model, hidden).values()
        ],
    }


def market_aliases(model: BriefModel, hidden: Collection[int]) -> dict[str, tuple[str, ...]]:
    """The words that name each visible market in prose: the brief's short label and name, and
    the plain aliases above. Used to tie a value slot to the market its clause names."""
    out: dict[str, tuple[str, ...]] = {}
    for r in model.rows():
        if r.tier is None or r.tier in hidden:
            continue
        names = {r.label, _display_name(r.name), *_ALIASES.get(r.instrument_id, ())}
        names.add(SHORT_LABELS.get(r.instrument_id, r.instrument_id))
        out[r.instrument_id] = tuple(n for n in names if n and not any(c.isnumeric() for c in n))
    return out


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

    kept: tuple[str, ...]  # with every slot replaced by the brief's display string
    n_numbers: int  # value slots filled in the kept sentences
    dropped: tuple[str, ...]


def sentences(text: str) -> list[str]:
    out = []
    for line in text.replace("\r", "").split("\n"):
        line = line.strip().lstrip("-•* ").strip()
        out += [s.strip() for s in _SENTENCE.split(line) if s.strip()]
    return out


def _own_words(sentence: str) -> str:
    return _SLOT.sub(" ", sentence)


def _names_in(text: str, aliases: Mapping[str, tuple[str, ...]]) -> set[str]:
    """Markets ``text`` names in its own words (whole words, case-insensitive)."""
    found = set()
    for iid, names in aliases.items():
        for n in names:
            if re.search(rf"(?<![\w&]){re.escape(n)}(?![\w])", text, re.IGNORECASE):
                found.add(iid)
                break
    return found


def _why_not(
    sentence: str, table: Mapping[str, Slot], aliases: Mapping[str, tuple[str, ...]]
) -> str | None:
    """Why ``sentence`` fails, or None (fail closed: anything the check cannot vouch for)."""
    for m in _SLOT.finditer(sentence):
        if m.group(0) not in table:
            return f"unknown slot {m.group(0)}"
    own = _own_words(sentence)
    if any(ch.isnumeric() for ch in own):
        return "it writes a number itself instead of a slot"
    if any(sign in own for sign in _SIGNS):
        return "it writes a unit sign itself instead of a slot"
    word = _NUMBER_WORDS.search(own)
    if word:
        return f"number or unit word {word.group(0)!r} outside a slot"
    clauses = [c for c in _CLAUSE.split(sentence) if c.strip()]
    sentence_markets = {table[m.group(0)].instrument_id for m in _SLOT.finditer(sentence)
                        if table[m.group(0)].kind == "name"} | _names_in(own, aliases)  # fmt: skip
    for i, clause in enumerate(clauses):
        used = [table[m.group(0)] for m in _SLOT.finditer(clause)]
        words = _own_words(clause)
        up, down = bool(_UP.search(words)), bool(_DOWN.search(words))
        if not (up or down) and i > 0:  # "rose sharply, {X.chg_1d}": the word comes first
            before = _own_words(clauses[i - 1])
            up, down = bool(_UP.search(before)), bool(_DOWN.search(before))
        named = {s.instrument_id for s in used if s.kind == "name"} | _names_in(words, aliases)
        for sl in used:
            if sl.kind == "name":
                continue
            if sl.kind == "chg_1d":
                if sl.sign == "-" and up:
                    return f"{sl.key} is a fall, but the clause says it rose"
                if sl.sign == "+" and down:
                    return f"{sl.key} is a rise, but the clause says it fell"
                if sl.sign is None and (up or down):
                    return f"{sl.key} did not move, but the clause gives it a direction"
            if named and sl.instrument_id not in named:
                return f"{sl.key} belongs to another market than the one its clause names"
            if not named and sl.instrument_id not in sentence_markets:
                return f"{sl.key} is used without naming its market"
    return None


def _fill(sentence: str, table: Mapping[str, Slot]) -> str:
    return _SLOT.sub(lambda m: table[m.group(0)].shows, sentence)


def check(
    reply: str,
    table: Mapping[str, Slot],
    forbidden: Collection[str],
    limit: int,
    aliases: Mapping[str, tuple[str, ...]] | None = None,
) -> Checked:
    """Keep the sentences that pass every rule (module docstring), with their slots filled; at
    most ``limit``. ``table`` is :func:`slots`; ``aliases`` is :func:`market_aliases`."""
    aliases = aliases or {}
    kept: list[str] = []
    dropped: list[str] = []
    total = 0
    for s in sentences(reply):
        word = _forbidden(_own_words(s), forbidden)
        if word is not None:
            dropped.append(f"forbidden word {word!r}")
            continue
        why = _why_not(s, table, aliases)
        if why is not None:
            dropped.append(why)
            continue
        if len(kept) >= limit:
            dropped.append("over the sentence limit")
            continue
        kept.append(_fill(s, table))
        total += sum(1 for m in _SLOT.finditer(s) if table[m.group(0)].kind != "name")
    return Checked(tuple(kept), total, tuple(dropped))


def hidden_tiers(config: Config) -> frozenset[int]:
    """Tiers the hosted dashboard hides (where the stored note is shown)."""
    return ALWAYS_HIDDEN | frozenset(config.dashboard.hosted_hidden_tiers)


def prompt(settings: NoteSettings) -> str:
    words = ", ".join(f'"{w}"' for w in settings.forbidden_words)
    n = settings.max_sentences
    return (
        "You write the morning note for Desk Brief, a markets data brief read by a hedge-fund "
        f"desk. Write {max(1, n - 1)} or {n} short sentences in plain English about what moved "
        "overnight and what to check, using only the facts in the user message.\n"
        "Rules:\n"
        "- Never write a number, a number word, a percent or sigma sign, or a unit yourself. "
        "Wherever you mean a value, write its slot exactly as listed (for example "
        "{ES_F.chg_1d} or {HY_OAS.sigma}); it is replaced by the brief's own figure. Name "
        "markets with their {X.name} slot or a plain name, and use a market's value slots only "
        "in a clause about that market.\n"
        "- A change slot's direction must match your words: say rose, widened or steepened only "
        "for a change shown with +, and fell, narrowed or flattened only for one shown with "
        "the minus sign.\n"
        "- Describe what happened. Give no advice, signals, recommendations, price targets, "
        "forecasts or predictions, and never say what a market will, may, could or should do.\n"
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
        log.info("note.skipped", reason=NOTE_OFF)
        return Note(reason=NOTE_OFF, model=s.model)
    key = (env.get(s.api_key_env) or "").strip()
    if not key:
        log.info("note.skipped", reason=NOTE_NO_KEY)
        return Note(reason=NOTE_NO_KEY, model=s.model)
    hidden = hidden_tiers(config)
    payload = facts(model, hidden)
    table = slots(model, hidden)
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
    # the call's cost first, from the reply's usage, so nothing after it can lose it
    try:
        cost = _cost(s, *_usage(response))
    except Exception:  # an unreadable usage block: count the worst case
        cost = worst
    try:
        return _read_reply(response, s, table, market_aliases(model, hidden), cost)
    except Exception as exc:
        log.warning("note.unexpected", error=type(exc).__name__)
        reason = f"the note could not be written ({type(exc).__name__})"
        return Note(reason=reason, model=s.model, cost_usd=cost)


def _read_reply(
    response: Any,
    s: NoteSettings,
    table: Mapping[str, Slot],
    aliases: Mapping[str, tuple[str, ...]],
    cost: float,
) -> Note:
    log = get_logger()
    stop = getattr(response, "stop_reason", "")
    if stop == "refusal":
        return Note(reason="the model declined to write it", model=s.model, cost_usd=cost)
    if stop == "max_tokens":  # config.NoteSettings: a reply cut at the ceiling is no note
        return Note(reason="the reply stopped at the token limit", model=s.model, cost_usd=cost)
    text = _reply_text(response).strip()
    if not text:
        return Note(reason="the reply was empty", model=s.model, cost_usd=cost)
    result = check(text, table, s.forbidden_words, s.max_sentences, aliases)
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
    doc = json.loads(store.get_bytes(max(versions)[1]))
    raw = doc.get("note")
    if not isinstance(raw, Mapping):
        return None
    written = doc.get("report_time")
    return Note(
        sentences=tuple(str(x) for x in raw.get("sentences") or ()),
        n_checked=int(raw.get("n_checked") or 0),
        n_numbers=int(raw.get("n_numbers") or 0),
        reason=raw.get("reason"),
        model=str(raw.get("model") or ""),
        cost_usd=float(raw.get("cost_usd") or 0.0),
        written_at=ensure_utc(datetime.fromisoformat(written)) if written else None,
    )


__all__ = [
    "Checked",
    "Slot",
    "check",
    "facts",
    "market_aliases",
    "month_spend",
    "record_spend",
    "slots",
    "stored_note",
    "with_note",
    "write_note",
]
