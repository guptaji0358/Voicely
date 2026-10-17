"""Pre-synthesis text analysis: finds spans that deserve attention. It NEVER rewrites text - it returns findings
(with offsets into the ORIGINAL text, so everything stays traceable) and suggestions a person can accept.

Heuristics only (no word-frequency list is bundled): a finding means "worth a look", not "will be mispronounced".
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass, field

from .pron_dictionary import PronDictionary

HETERONYMS = {
    "read": "red / reed", "lead": "led / leed", "live": "liv / lyve", "wind": "wind / wynd", "tear": "tair / teer",
    "bass": "bayss / bass", "close": "klohs / klohz", "desert": "DEZ-ert / di-ZERT", "minute": "MIN-it / my-NYOOT",
    "object": "OB-ject / ob-JECT", "record": "REC-ord / ri-CORD", "present": "PREZ-ent / pri-ZENT",
    "wound": "woond / wownd", "bow": "boh / bow", "row": "roh / row", "refuse": "REF-yoos / ri-FYOOZ",
    "content": "KON-tent / kon-TENT", "produce": "PROD-yoos / pro-DYOOS", "resume": "ri-ZYOOM / REZ-oo-may",
    "sow": "soh / sow", "use": "yoos / yooz", "does": "duz / doze", "number": "NUM-ber / NUM-er"}
ABBREVIATIONS = {   # abbreviation -> possible expansions; >1 means ambiguous
    "st.": ["Street", "Saint"], "dr.": ["Doctor", "Drive"], "no.": ["Number", "no"], "inc.": ["Incorporated"],
    "ltd.": ["Limited"], "approx.": ["approximately"], "dept.": ["department"], "est.": ["established", "estimated"],
    "jr.": ["Junior"], "sr.": ["Senior"], "mt.": ["Mount"], "ave.": ["Avenue"], "fig.": ["figure"], "vol.": ["volume"],
    "ca.": ["circa"], "cf.": ["compare"], "al.": ["others"], "ed.": ["edition", "editor"]}
UNITS = {"km", "m", "cm", "mm", "kg", "g", "mg", "lb", "lbs", "oz", "mph", "kph", "gb", "mb", "kb", "tb", "ghz", "mhz",
         "khz", "hz", "ms", "ns", "s", "min", "h", "hr", "°c", "°f", "kw", "mw", "kwh", "ml", "l", "px", "pt", "db", "ft", "in"}
ROMAN = re.compile(r"^(?=[MDCLXVI])M{0,3}(CM|CD|D?C{0,3})(XC|XL|L?X{0,3})(IX|IV|V?I{0,3})$")

_WORD = re.compile(r"[^\W\d_](?:\w|['’\-])*", re.UNICODE)
_SENT_START = re.compile(r"(?:^|[.!?…]\s+|\n\s*)$")
_NUMBER = re.compile(r"[$€£]?\d[\d,]*(?:\.\d+)?%?")
_DATE = [re.compile(r"\b\d{4}-\d{2}-\d{2}\b"), re.compile(r"\b\d{1,2}[/.]\d{1,2}[/.]\d{2,4}\b"),
         re.compile(r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.? \d{1,2}(?:st|nd|rd|th)?(?:,? \d{4})?\b")]
_TIME = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\s?(?:[ap]\.?m\.?)?", re.I)


@dataclass
class Finding:
    id: str
    kind: str                 # number|date|unit|abbreviation|acronym|heteronym|name|technical|rare|multilingual|dictionary|punctuation
    start: int
    end: int
    text: str
    severity: str             # "info" (will probably be fine) | "review" (a person should decide)
    reason: str
    suggestions: list[str] = field(default_factory=list)
    occurrences: int = 1


@dataclass
class AnalysisResult:
    findings: list[Finding]
    counts: dict
    notes: list[str]
    applications: list[dict]          # dictionary entries that WILL be applied (traceable)
    unsupported: list[dict]           # IPA-only entries the selected backend cannot honour

    def to_dict(self) -> dict:
        return {"findings": [asdict(f) for f in self.findings], "counts": self.counts, "notes": self.notes,
                "applications": self.applications, "unsupported": self.unsupported}


def _script(ch: str) -> str:
    try:
        return unicodedata.name(ch).split()[0]
    except ValueError:
        return "UNKNOWN"


def _syllable_estimate(w: str) -> int:
    return max(1, len(re.findall(r"[aeiouy]+", w.lower())))


def analyze_text(text: str, language: str = "en-us", dictionary: PronDictionary | None = None,
                 ipa_ok: bool = False, max_findings: int = 400) -> AnalysisResult:
    findings: list[Finding] = []
    seen_words: dict[tuple[str, str], Finding] = {}
    covered: list[tuple[int, int]] = []
    n = 0

    def add(kind, s, e, severity, reason, sugg=None, dedupe=False):
        nonlocal n
        key = (kind, text[s:e].lower())
        if dedupe and key in seen_words:
            seen_words[key].occurrences += 1
            return
        n += 1
        f = Finding(f"f{n}", kind, s, e, text[s:e], severity, reason, sugg or [])
        findings.append(f)
        if dedupe:
            seen_words[key] = f
        covered.append((s, e))

    def is_covered(s, e):
        return any(a <= s and e <= b for a, b in covered)

    english = language.lower().startswith("en")
    apps, unsupported = [], []
    if dictionary is not None:
        for a in dictionary.find(text, language, ipa_ok=ipa_ok):
            e = dictionary.entries[a.entry_id]
            apps.append({"start": a.start, "end": a.end, "original": a.original, "entry_id": a.entry_id,
                         "spoken": a.spoken, "mode": a.mode, "ipa": a.ipa})
            add("dictionary", a.start, a.end, "info",
                f"dictionary entry applies ({a.mode}): spoken as {a.spoken!r}" if a.mode == "text"
                else f"dictionary IPA /{a.ipa}/ will be given to the voice",
                [e.spoken_text() or e.ipa])
        for e in dictionary.unsupported_for(text, language) if not ipa_ok else []:
            unsupported.append({"entry_id": e.id, "written": e.written, "reason": "this backend cannot take IPA"})

    for rx in _DATE:                                           # dates first so their digits are not double-reported
        for m in rx.finditer(text):
            amb = bool(re.fullmatch(r"\d{1,2}[/.]\d{1,2}[/.]\d{2,4}", m.group()))
            add("date", m.start(), m.end(), "review" if amb else "info",
                "day/month order is ambiguous (03/04/2025 can be 3 April or March 4)" if amb else "date will be read aloud as written",
                ["write the date in words, e.g. 'the third of April, 2025'"] if amb else [])
    for m in _TIME.finditer(text):
        if not is_covered(m.start(), m.end()):
            add("date", m.start(), m.end(), "info", "time of day")
    for m in _NUMBER.finditer(text):
        if is_covered(m.start(), m.end()):
            continue
        after = re.match(r"\s?([A-Za-z°]+)", text[m.end():m.end() + 6])
        if after and after.group(1).lower() in UNITS:
            add("unit", m.start(), m.end() + after.end(), "info", f"unit {after.group(1)!r} after a number: check how it is read")
        elif re.fullmatch(r"\d{5,}", m.group()):
            add("number", m.start(), m.end(), "review", "long digit string: read as a number or digit by digit?",
                ["digit by digit", "as one number"])
        elif m.group().startswith(("$", "€", "£")) or m.group().endswith("%") or "," in m.group() or "." in m.group():
            add("number", m.start(), m.end(), "info", "formatted number (currency/percent/separators)")
    for m in re.finditer(r"\b(?:[A-Za-z]{1,6}\.)+(?=\s|$)", text):
        low = m.group().lower()
        if low in ABBREVIATIONS and not is_covered(m.start(), m.end()):
            ex = ABBREVIATIONS[low]
            add("abbreviation", m.start(), m.end(), "review" if len(ex) > 1 else "info",
                "ambiguous abbreviation" if len(ex) > 1 else "abbreviation", ex)
    for m in _WORD.finditer(text):
        s, e, w = m.start(), m.end(), m.group()
        if is_covered(s, e):
            continue
        scripts = {_script(c) for c in w if c.isalpha()}
        if english and scripts - {"LATIN"}:
            add("multilingual", s, e, "review", f"{'/'.join(sorted(scripts - {'LATIN'}))} text in an English-language read; "
                "the selected voice/language may mispronounce it", ["select the matching language"], dedupe=True)
            continue
        if english and any(c in "áàâãäåçéèêëíìîïñóòôõöúùûüýÿšžčřěłøæœß" for c in w.lower()):
            add("multilingual", s, e, "review", "accented/non-English word; pronunciation depends on language support",
                ["add a dictionary entry with a respelling"], dedupe=True)
            continue
        low = w.lower()
        if w.isupper() and 2 <= len(w) <= 6 and w.isalpha() and not ROMAN.match(w):
            add("acronym", s, e, "review", "acronym: read as letters or as a word?",
                ["spell out letters", "read as a word"], dedupe=True)
        elif re.search(r"[a-z][A-Z]", w) or re.search(r"[A-Za-z]\d|\d[A-Za-z]", w) or "_" in w:
            add("technical", s, e, "review", "mixed-case or letters+digits identifier", ["add a dictionary respelling"], dedupe=True)
        elif low in HETERONYMS:
            add("heteronym", s, e, "review" if english else "info",
                f"heteronym: pronunciation depends on meaning ({HETERONYMS[low]})",
                ["add a context-sensitive dictionary entry"], dedupe=True)
        elif w[0].isupper() and not _SENT_START.search(text[max(0, s - 4):s]) and w[1:].islower() and english:
            odd = len(w) >= 7 or re.search(r"[bcdfghjklmnpqrstvwxz]{4}|[qxz]|(?:gh|ph|kh|ng|ny|zh)", low)
            if odd:
                add("name", s, e, "review", "capitalised name: names are the most commonly mispronounced words",
                    ["add a respelling or IPA to the dictionary"], dedupe=True)
        elif english and w.isalpha() and (len(w) >= 12 or (len(w) >= 8 and re.search(r"[bcdfghjklmnpqrstvwxz]{4}", low))):
            add("rare", s, e, "info", "long or unusual word", dedupe=True)
    if re.search(r"[…]{2,}|\.{4,}|[!?]{3,}", text):
        m = re.search(r"[…]{2,}|\.{4,}|[!?]{3,}", text)
        add("punctuation", m.start(), m.end(), "info", "repeated punctuation may change pauses or intonation")

    findings.sort(key=lambda f: (f.start, f.end))
    capped = len(findings) > max_findings
    findings = findings[:max_findings]
    counts: dict[str, int] = {}
    for f in findings:
        counts[f.kind] = counts.get(f.kind, 0) + 1
    notes = []
    if not english:
        notes.append("Number and abbreviation analysis is English-only; other languages get a generic pass.")
    if capped:
        notes.append(f"Only the first {max_findings} findings are shown.")
    notes.append("Findings are heuristics; nothing in the text has been changed.")
    return AnalysisResult(findings, {"total": len(findings), "review": sum(f.severity == "review" for f in findings), **counts},
                          notes, apps, unsupported)
