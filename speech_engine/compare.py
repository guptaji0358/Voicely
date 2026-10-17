"""Text canonicalisation for comparing intended text with a recognised transcript.

Goal: remove HARMLESS differences before anything is reported as an error - case, punctuation, curly
quotes, hyphenation, contractions and number formatting ("2024" == "twenty twenty four" == "2,024").
English number handling only; other languages are compared as plain case/punctuation-folded words and the
report says so. Nothing here ever edits the text that is spoken - it only builds comparison tokens, each with
the character span of the source text it came from so findings can be traced back.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

ONES = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
    "seventeen eighteen nineteen".split())}
TENS = {w: 10 * i for i, w in enumerate("_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()) if w != "_"}
SCALES = {"hundred": 100, "thousand": 1000, "million": 10**6, "billion": 10**9}
ORD_WORDS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8,
             "ninth": 9, "tenth": 10, "eleventh": 11, "twelfth": 12, "twentieth": 20, "thirtieth": 30, "hundredth": 100}
NUM_WORDS = set(ONES) | set(TENS) | set(SCALES) | {"oh"}
FILLERS = {"uh", "um", "uhm", "erm", "hmm", "mm"}
CONTRACTIONS = {"won't": "will not", "can't": "can not", "cannot": "can not", "shan't": "shall not", "let's": "let us",
                "i'm": "i am", "it's": "it is", "that's": "that is", "there's": "there is", "what's": "what is",
                "he's": "he is", "she's": "she is", "here's": "here is", "who's": "who is"}
CONTR_SUFFIX = (("n't", " not"), ("'re", " are"), ("'ve", " have"), ("'ll", " will"), ("'d", " would"))

_TOKEN = re.compile(
    r"(?P<cur>[$€£]\d[\d,]*(?:\.\d+)?)|(?P<pct>\d[\d,]*(?:\.\d+)?\s?%)|(?P<time>\b\d{1,2}:\d{2}\b)|"
    r"(?P<ord>\b\d+(?:st|nd|rd|th)\b)|(?P<num>\d[\d,]*(?:\.\d+)?)|(?P<amp>&)|"
    r"(?P<word>[^\W\d_]+(?:['’][^\W\d_]+)*)", re.UNICODE)


@dataclass
class Tok:
    norm: str
    start: int
    end: int

    def __repr__(self) -> str:
        return f"{self.norm}@{self.start}"


def _cardinal(words: list[str]) -> int | None:
    """Standard English cardinal grammar; None if the words are not one valid cardinal."""
    total, cur, seen = 0, 0, False
    prev_kind = None
    for w in words:
        if w in ONES:
            if prev_kind in ("ones", "teens"):
                return None
            if prev_kind == "tens" and ONES[w] >= 10:
                return None
            cur += ONES[w]
            prev_kind = "ones" if ONES[w] < 10 else "teens"
        elif w in TENS:
            if prev_kind in ("tens", "ones", "teens"):
                return None
            cur += TENS[w]
            prev_kind = "tens"
        elif w == "hundred":
            if not (prev_kind in ("ones", "teens", "tens")) or cur == 0 or cur >= 100:
                if cur == 0 and not seen:   # bare "hundred"
                    cur = 1
                else:
                    return None
            cur *= 100
            prev_kind = "hundred"
        elif w in SCALES:
            if cur == 0:
                return None
            total += cur * SCALES[w]
            cur = 0
            prev_kind = "scale"
        else:
            return None
        seen = True
    return total + cur if seen else None


def _year_style(words: list[str]) -> int | None:
    """'twenty twenty four' -> 2024, 'nineteen oh five' -> 1905, 'nineteen ninety nine' -> 1999."""
    for k in range(1, len(words)):
        a, b = words[:k], words[k:]
        if b and b[0] == "oh" and len(b) == 2 and b[1] in ONES and ONES[b[1]] < 10:
            hi, lo = _cardinal(a), ONES[b[1]]
            lo_s = f"0{lo}"
        else:
            hi, lo = _cardinal(a), _cardinal(b)
            lo_s = f"{lo:02d}" if lo is not None and lo < 100 else None
        if hi is not None and 10 <= hi <= 99 and lo_s is not None and len(lo_s) == 2:
            return int(f"{hi}{lo_s}")
    return None


def words_to_number(words: list[str]) -> str | None:
    """Canonical digit string for a run of English number words, or None."""
    n = _cardinal(words)
    if n is None:
        n = _year_style(words)
    return None if n is None else str(n)


def _canon_numeric(raw: str) -> str:
    return raw.replace(",", "")


def _expand_contraction(w: str) -> list[str]:
    if w in CONTRACTIONS:
        return CONTRACTIONS[w].split()
    for suf, rep in CONTR_SUFFIX:
        if w.endswith(suf) and len(w) > len(suf):
            return (w[: -len(suf)] + rep).split()
    return [w]


def canonical_tokens(text: str, english_numbers: bool = True) -> list[Tok]:
    """Comparison tokens with source spans. Digits and number words both become '#<digits>'."""
    text = unicodedata.normalize("NFKC", text)
    raw: list[Tok] = []
    for m in _TOKEN.finditer(text):
        k, s, e, g = m.lastgroup, m.start(), m.end(), m.group()
        if k == "cur":
            sym = g[0]
            name = {"$": "dollars", "€": "euros", "£": "pounds"}[sym]
            raw += [Tok("#" + _canon_numeric(g[1:]), s, e), Tok(name, s, e)]
        elif k == "pct":
            raw += [Tok("#" + _canon_numeric(g.rstrip("% ").strip()), s, e), Tok("percent", s, e)]
        elif k == "time":
            h, mi = g.split(":")
            raw += [Tok("#" + str(int(h)), s, e)] + ([Tok("#" + str(int(mi)), s, e)] if int(mi) else [])
        elif k == "ord":
            raw.append(Tok("#" + re.sub(r"\D", "", g) + "th", s, e))
        elif k == "num":
            raw.append(Tok("#" + _canon_numeric(g), s, e))
        elif k == "amp":
            raw.append(Tok("and", s, e))
        else:
            w = g.lower().replace("’", "'")
            if w in FILLERS:
                continue
            for part in _expand_contraction(w):
                raw.append(Tok(part, s, e))
    if not english_numbers:
        return raw
    out: list[Tok] = []
    i = 0
    while i < len(raw):
        t = raw[i]
        if t.norm in NUM_WORDS or t.norm in ORD_WORDS:
            j = i
            while j < len(raw) and (raw[j].norm in NUM_WORDS or raw[j].norm in ORD_WORDS
                                    or (raw[j].norm == "and" and j > i and j + 1 < len(raw) and raw[j + 1].norm in NUM_WORDS)):
                j += 1
            run = raw[i:j]
            k = 0
            while k < len(run):          # greedy: longest valid number first ("three thirty" -> 3, 30)
                done = False
                for m in range(len(run), k, -1):
                    seg = run[k:m]
                    words = [x.norm for x in seg if x.norm != "and"]
                    if not words or seg[0].norm == "and" or seg[-1].norm == "and":
                        continue
                    ordinal = words[-1] in ORD_WORDS
                    if ordinal:
                        base = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven", 8: "eight",
                                9: "nine", 10: "ten", 11: "eleven", 12: "twelve", 20: "twenty", 30: "thirty",
                                100: "hundred"}[ORD_WORDS[words[-1]]]
                        n = words_to_number(words[:-1] + [base]) if len(words) > 1 or True else None
                    else:
                        n = words_to_number(words)
                    if n is not None:
                        out.append(Tok("#" + n + ("th" if ordinal else ""), seg[0].start, seg[-1].end))
                        k = m
                        done = True
                        break
                if not done:
                    out.append(run[k])
                    k += 1
            i = j
            continue
        out.append(t)
        i += 1
    # decimals: "# point # #" -> "#3.14"
    res: list[Tok] = []
    i = 0
    while i < len(out):
        t = out[i]
        if t.norm.startswith("#") and i + 2 < len(out) and out[i + 1].norm == "point" and out[i + 2].norm.startswith("#") \
                and "." not in t.norm:
            digits = ""
            j = i + 2
            while j < len(out) and out[j].norm.startswith("#") and out[j].norm[1:].isdigit() and len(out[j].norm) == 2:
                digits += out[j].norm[1:]
                j += 1
            if digits:
                res.append(Tok(t.norm + "." + digits, t.start, out[j - 1].end))
                i = j
                continue
        res.append(t)
        i += 1
    # "five dollars and fifty cents" -> "#5.50 dollars"
    fin: list[Tok] = []
    i = 0
    while i < len(res):
        t = res[i]
        if t.norm.startswith("#") and i + 3 < len(res) and res[i + 1].norm in ("dollars", "dollar"):
            k = i + 2 + (1 if res[i + 2].norm == "and" else 0)
            if k + 1 < len(res) and res[k].norm.startswith("#") and res[k + 1].norm in ("cents", "cent"):
                fin.append(Tok(f"{t.norm}.{res[k].norm[1:].zfill(2)}", t.start, res[k + 1].end))
                fin.append(Tok("dollars", t.start, res[k + 1].end))
                i = k + 2
                continue
        if t.norm == "dollar":
            t = Tok("dollars", t.start, t.end)
        fin.append(t)
        i += 1
    return fin


def fold_equal(a: str, b: str) -> bool:
    """True when two strings differ only in case/punctuation/number format."""
    return [t.norm for t in canonical_tokens(a)] == [t.norm for t in canonical_tokens(b)]
