"""Helpers shared by the producing agents: reply schemas, trace parsing and grounding."""

from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from decimal import Decimal
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, create_model

from autolab.state.models import (
    AnalysisReport,
    ExperimentDesign,
    ExperimentResult,
    FormalizedStep,
    Hypothesis,
    ParameterValue,
    ReasoningStep,
    ReasoningTrace,
)


def content_schema(model: type[BaseModel], server_owned: frozenset[str]) -> type[BaseModel]:
    """`model` without its `server_owned` fields: the schema the LLM is asked to fill.

    Extra keys in a reply (e.g. an `id` the model or a fixture supplies) are ignored,
    so ids, timestamps and cross-references can only come from code.
    """
    fields: dict[str, Any] = {
        name: (info.annotation, info)
        for name, info in model.model_fields.items()
        if name not in server_owned
    }
    return create_model(f"{model.__name__}Reply", __config__=ConfigDict(extra="ignore"), **fields)


HYPOTHESIS_REPLY: type[BaseModel] = content_schema(
    Hypothesis, frozenset({"id", "created_at", "question"})
)
DESIGN_REPLY: type[BaseModel] = content_schema(ExperimentDesign, frozenset({"id", "hypothesis_id"}))
REPORT_REPLY: type[BaseModel] = content_schema(
    AnalysisReport, frozenset({"id", "created_at", "result_id"})
)


class _TraceSteps(BaseModel):
    model_config = ConfigDict(extra="forbid")

    steps: tuple[ReasoningStep, ...]


def parse_trace(agent: str, raw: str) -> ReasoningTrace:
    """Validate an LLM `{"steps": [...]}` reply into a `ReasoningTrace`.

    Raises:
        pydantic.ValidationError: if `raw` is not a valid steps object.
    """
    return ReasoningTrace(agent=agent, steps=_TraceSteps.model_validate_json(raw).steps)


def conclusion_instruction(claim: str) -> str:
    """Prompt line telling the LLM which claim its final step must state."""
    return f"The final step must be a conclusion whose claim is exactly: {claim}"


def with_feedback(prompt: str, feedback: str) -> str:
    """Append feedback on a previous attempt (verifier summary or human note), if any."""
    return f"{prompt}\n\nfeedback_on_previous_attempt: {feedback}" if feedback else prompt


def result_claim(result: ExperimentResult) -> str:
    """The claim an Execute trace must conclude: the status and measured values."""
    readings = ", ".join(f"{m.name} = {_decimal(m.value)}" for m in result.measurements)
    return f"experiment {result.status}: {readings or 'no measurements'}"


def measurement_facts(result: ExperimentResult) -> tuple[FormalizedStep, ...]:
    """One `(= <name> <value>)` Real fact per measurement, built in code, not by the LLM.

    Raises:
        ValueError: if a measurement value is not finite.
    """
    return tuple(_real_fact(_symbol(m.name), m.value) for m in result.measurements)


def design_claim(design: ExperimentDesign) -> str:
    """The claim a Design trace must conclude: the protocol and every structured field."""
    values = [
        f"estimated_cost_usd = {_decimal(design.estimated_cost_usd)}",
        f"estimated_duration_minutes = {_decimal(design.estimated_duration_minutes)}",
    ]
    for p in design.parameters:
        text = _decimal(float(p.value)) if _numeric(p.value) else str(p.value)
        values.append(f"{_param_symbol(p.name)} = {text}" + (f" {p.unit}" if p.unit else ""))
    return f"design protocol: {design.protocol}; {', '.join(values)}"


def _wordset(*lines: str) -> frozenset[str]:
    return frozenset(" ".join(lines).split())


_ISO_4217: Final = _wordset(
    "AED AFN ALL AMD ANG AOA ARS AUD AWG AZN BAM BBD BDT BGN BHD BIF BMD BND BOB BOV BRL BSD "
    "BTN BWP BYN BZD CAD CDF CHE CHF CHW CLF CLP CNY COP COU CRC CUC CUP CVE CZK DJF DKK DOP "
    "DZD EGP ERN ETB EUR FJD FKP GBP GEL GHS GIP GMD GNF GTQ GYD HKD HNL HRK HTG HUF IDR ILS "
    "INR IQD IRR ISK JMD JOD JPY KES KGS KHR KMF KPW KRW KWD KYD KZT LAK LBP LKR LRD LSL LYD "
    "MAD MDL MGA MKD MMK MNT MOP MRU MUR MVR MWK MXN MXV MYR MZN NAD NGN NIO NOK NPR NZD OMR "
    "PAB PEN PGK PHP PKR PLN PYG QAR RON RSD RUB RWF SAR SBD SCR SDG SEK SGD SHP SLE SLL SOS "
    "SRD SSP STN SVC SYP SZL THB TJS TMT TND TOP TRY TTD TWD TZS UAH UGX USD USN UYI UYU UYW "
    "UZS VED VEF VES VND VUV WST XAF XAG XAU XCD XCG XDR XOF XPD XPF XPT YER ZAR ZMW ZWG ZWL"
)
_CURRENCY_WORDS: Final = _wordset(
    "usd eur gbp inr jpy cny chf dollar dollars buck bucks euro euros pound pounds sterling "
    "quid pence penny cent cents yen yuan renminbi rmb rupee rupees rs franc francs krona "
    "krone kronor kroner kr peso pesos rand ruble rubles rouble roubles lira lire shekel "
    "shekels dirham dirhams riyal riyals rial rials dinar dinars baht ringgit rupiah zloty "
    "złoty zł forint koruna naira cedi cedis won reais dong lei leu lev grand"
)
_TIME_WORDS: Final = _wordset(
    "msec msecs millisecond milliseconds microsecond microseconds sec secs second seconds "
    "min mins minute minutes hr hrs hour hours day days wk wks week weeks fortnight "
    "fortnights mth mths month months yr yrs year years"
)
_TIME_EXACT: Final = frozenset({"s", "h", "d", "y", "ms", "µs", "μs", "mo", "mos", "am", "pm"})
"""Short units matched in this case only: `16S` (Svedberg), `3D`, `5 M`, `MS` are not times."""
_MULTIPLIERS: Final = frozenset(
    {"k", "K", "M", "B", "mn", "bn", "thousand", "thousands", "million", "millions"}
    | {"billion", "billions", "lakh", "lakhs", "crore", "crores"}
)
_NUMBER_WORDS: Final = _wordset(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
    "fifteen sixteen seventeen eighteen nineteen twenty thirty forty fifty sixty seventy "
    "eighty ninety hundred dozen half"
)
_NUMERALS: Final = r"\d\u00b2\u00b3\u00b9\u2070\u2074-\u2079\u00bc-\u00be\u2150-\u215e"
_TOKEN: Final = re.compile(
    rf"(?P<num>[{_NUMERALS}]+(?:[.,]\d+)*)"
    rf"|(?P<word>[^\W_{_NUMERALS}]+)"
    r"|(?P<sep>[\s_\-\u2010-\u2015]+)"
    r"|(?P<other>.)",
    re.DOTALL,
)
_LEAD_IN: Final = frozenset(":=~≈.")
"""Between a unit word and the number after it: `USD 500`, `Rs. 500`, `minutes: 240`."""
_CONFUSABLES: Final = str.maketrans(
    "аеһіјорсѕухԁӏАВЕКМНОРСТХУІЈЅαοιρυνκΑΒΕΖΗΙΚΜΝΟΡΤΥΧ",
    "aehijopcsyxdlABEKMHOPCTXYIJSaoipuvkABEZHIKMNOPTYX",
)
"""Cyrillic and Greek letters that look Latin: `48 \\u04bbours` (Cyrillic shha) reads as hours."""
_DOTTED: Final = re.compile(r"\b(?:[A-Za-z]\.){2,}")
_WINDOW: Final = 3


def _is_time(word: str, *, short: bool = True) -> bool:
    return word.lower() in _TIME_WORDS or (short and word in _TIME_EXACT)


def _is_currency(word: str) -> bool:
    return word in _ISO_4217 or word.lower() in _CURRENCY_WORDS


def _is_amount(match: re.Match[str]) -> bool:
    return match.lastgroup == "num" or (
        match.lastgroup == "word" and match.group().lower() in _NUMBER_WORDS
    )


def _normalized(text: str) -> str:
    """NFKC, without format characters (soft hyphen, zero-width space), confusables folded."""
    kept = (c for c in unicodedata.normalize("NFKC", text) if unicodedata.category(c) != "Cf")
    return "".join(kept).translate(_CONFUSABLES)


def protocol_figures(protocol: str) -> tuple[str, ...]:
    """Currency amounts and durations stated outright in a design's free text.

    Cost and duration live only in the bound `estimated_cost_usd` /
    `estimated_duration_minutes` fields. This is a rejection check, not parsing
    into state (CLAUDE.md §9): a match only blocks admission (with a retry), and
    no value is read back from the text. It is not the guard against understated
    figures, which is the Design agent's independent estimate (SG25), so it flags
    only explicit forms: any currency symbol, and any amount (digits or a number
    word) next to a time unit, a currency word or an ISO 4217 code, with spaces,
    hyphens or a k/M multiplier between them. Gene names (`TOP10`), ages
    (`8-week-old`), day labels (`Day 1`), `per cent` and metres are not figures.
    """
    text = _DOTTED.sub(lambda m: m.group().replace(".", ""), _normalized(protocol))
    toks = list(_TOKEN.finditer(text))
    found: list[str] = []
    for i, tok in enumerate(toks):
        kind, word = tok.lastgroup, tok.group()
        if kind == "other" and any(unicodedata.category(c) == "Sc" for c in word):
            lo = i - 1 if i and toks[i - 1].lastgroup == "num" else i
            hi = i + 1 if i + 1 < len(toks) and toks[i + 1].lastgroup == "num" else i
            found.append(text[toks[lo].start() : toks[hi].end()])
        elif _is_amount(tok):
            after = _words_after(toks, i)
            if after and after[0].group() in _MULTIPLIERS:
                after = after[1:]
            words = [w.group() for w in after]
            if words and (_is_time(words[0]) or _is_currency(words[0])):
                if not (_is_time(words[0]) and words[1:2] == ["old"]):  # `8-week-old`: an age
                    found.append(text[tok.start() : after[0].end()])
            elif (
                len(words) > 1
                and words[0].lower() != "per"  # `2 per cent`
                and (_is_time(words[1], short=False) or words[1].lower() in _CURRENCY_WORDS)
            ):
                found.append(text[tok.start() : after[1].end()])
        elif kind == "word" and (_is_currency(word) or _is_time(word, short=False)):
            j, lead_in = i + 1, False
            while j < len(toks) and (toks[j].lastgroup == "sep" or toks[j].group() in _LEAD_IN):
                lead_in = lead_in or toks[j].group() in _LEAD_IN
                j += 1
            # A time word needs `:`/`=` before its number (`Day 1` is a label), and a bare
            # ISO code a separator (`TOP10`, `PGK1` are genes; `USD500` is a price).
            if not _is_currency(word):
                stated = lead_in
            else:
                stated = j > i + 1 or word.lower() in _CURRENCY_WORDS
            if stated and j < len(toks) and toks[j].lastgroup == "num":
                found.append(text[tok.start() : toks[j].end()])
    return tuple(found)


def _words_after(toks: list[re.Match[str]], i: int) -> list[re.Match[str]]:
    words: list[re.Match[str]] = []
    for tok in toks[i + 1 :]:
        if tok.lastgroup == "word" and len(words) < _WINDOW:
            words.append(tok)
        elif tok.lastgroup != "sep":
            break
    return words


def design_figures(design: ExperimentDesign) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """`protocol_figures` of the protocol, and of the parameters' free text.

    Names, values and units all reach `design_claim`, and a string value or a unit
    has no fact behind it, so they are held to the same rule as the protocol
    (AUDIT-15 #4): `"$500"` or `30` with unit `min` is a figure the cost/duration
    pause never reads.
    """
    texts = [text for p in design.parameters for text in (p.name, f"{p.value} {p.unit or ''}")]
    return protocol_figures(design.protocol), tuple(f for t in texts for f in protocol_figures(t))


def design_facts(design: ExperimentDesign) -> tuple[FormalizedStep, ...]:
    """Code-built Real facts for the Design's cost, duration and numeric parameters.

    Raises:
        ValueError: if a value is not finite.
    """
    return (
        _real_fact("estimated_cost_usd", design.estimated_cost_usd),
        _real_fact("estimated_duration_minutes", design.estimated_duration_minutes),
        *(
            _real_fact(_param_symbol(p.name), float(p.value))
            for p in design.parameters
            if _numeric(p.value)
        ),
    )


def _numeric(value: ParameterValue) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


_PLAIN: Final = re.compile(r"[A-Za-z0-9_]+")


def _param_symbol(name: str) -> str:
    if _PLAIN.fullmatch(name):
        return f"param_{name}"
    return f"paramx_{_hashed(name)}"


def _real_fact(sym: str, value: float) -> FormalizedStep:
    return FormalizedStep(
        declarations=f"(declare-const {sym} Real)", assertion=f"(= {sym} {_real(value)})"
    )


def _symbol(name: str) -> str:
    if _PLAIN.fullmatch(name) and name[0].isalpha() and not name.startswith("m_"):
        return name
    return f"m_{_hashed(name)}"


def _hashed(name: str) -> str:
    """ASCII-only, and injective up to the hash: `name` keeps its own digest."""
    sym = re.sub(r"[^A-Za-z0-9_]", "_", name)
    return f"{sym}_{hashlib.sha256(name.encode()).hexdigest()[:12]}"


def _decimal(value: float) -> str:
    if not math.isfinite(value):
        raise ValueError(f"measurement value must be finite, got {value!r}")
    text = format(Decimal(repr(value)), "f")
    return text if "." in text else f"{text}.0"


def _real(value: float) -> str:
    text = _decimal(value)
    return f"(- {text[1:]})" if text.startswith("-") else text


__all__ = [
    "DESIGN_REPLY",
    "HYPOTHESIS_REPLY",
    "REPORT_REPLY",
    "conclusion_instruction",
    "content_schema",
    "design_claim",
    "design_facts",
    "design_figures",
    "measurement_facts",
    "parse_trace",
    "protocol_figures",
    "result_claim",
    "with_feedback",
]
