"""Text normalisation and bounded chunking for long (OCR / document) text.

``iter_chunks`` is a generator: it never materialises more than one paragraph
of chunk objects at a time, so very large inputs stay bounded.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable, Iterator

from .exceptions import InvalidInputError

_ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍⁠﻿­"), None)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_HYPHEN_WRAP = re.compile(r"(\w)-\n(\w)")
_PARA_SPLIT = re.compile(r"\n\s*\n")
_WS = re.compile(r"[ \t ]+")
# Sentence end: . ! ? … (and CJK/Devanagari equivalents) followed by space/quote.
_SENT_END = re.compile(r"(?<=[.!?…。！？।])[\"'”’)\]]*\s+")
_CLAUSE = re.compile(r"(?<=[,;:—–，、])\s+")

# Only unambiguous abbreviations; "St." etc. are left alone on purpose.
_ABBREV = {
    r"\bDr\.": "Doctor", r"\bMr\.": "Mister", r"\bMrs\.": "Missus",
    r"\bMs\.": "Ms", r"\bProf\.": "Professor", r"\be\.g\.": "for example",
    r"\bi\.e\.": "that is", r"\betc\.": "et cetera", r"\bvs\.": "versus",
    # Kokoro reads plain "AI" with a weak, clipped first letter; "A.I" gives a clear "ay eye" (no sentence split,
    # unlike "A.I."; and "A I" would make the first letter the article "uh").
    r"\b(?:AI|Ai)\b(?!\.\w)": "A.I",
}
# Periods inside these must not end a sentence once expanded; we expand first.


@dataclass(frozen=True)
class Chunk:
    text: str
    pause_ms: int  # silence to insert after this chunk
    index: int = 0
    paragraph_end: bool = False


def normalize(text: str, expand_abbreviations: bool = True) -> str:
    """Unicode-normalise, strip junk characters, repair OCR line wrapping."""
    if not isinstance(text, str):
        raise InvalidInputError("text must be a string")
    text = unicodedata.normalize("NFC", text).replace("\r\n", "\n").replace("\r", "\n")
    text = text.translate(_ZERO_WIDTH)
    text = _CONTROL.sub(" ", text)
    text = text.replace("�", " ")  # replacement chars from bad decoding/OCR
    text = _HYPHEN_WRAP.sub(r"\1\2", text)  # "exam-\nple" -> "example"
    if expand_abbreviations:
        for pat, rep in _ABBREV.items():
            text = re.sub(pat, rep, text)
    return text


def _paragraphs(text: str) -> Iterator[str]:
    for para in _PARA_SPLIT.split(text):
        # Single newlines inside a paragraph are OCR line wraps -> spaces.
        para = _WS.sub(" ", para.replace("\n", " ")).strip()
        if para:
            yield para


def _has_speakable(s: str) -> bool:
    return any(ch.isalnum() for ch in s)


def _split_long(sentence: str, max_chars: int) -> Iterator[str]:
    """Split an over-long sentence at clauses, then words, then hard cut."""
    if len(sentence) <= max_chars:
        yield sentence
        return
    buf = ""
    for part in _CLAUSE.split(sentence):
        if len(part) > max_chars:
            if buf:
                yield buf
                buf = ""
            words, cur = part.split(" "), ""
            for w in words:
                while len(w) > max_chars:  # pathological unbroken token
                    if cur:
                        yield cur
                        cur = ""
                    yield w[:max_chars]
                    w = w[max_chars:]
                if len(cur) + len(w) + 1 > max_chars and cur:
                    yield cur
                    cur = w
                else:
                    cur = f"{cur} {w}".strip()
            if cur:
                yield cur
        elif len(buf) + len(part) + 1 > max_chars and buf:
            yield buf
            buf = part
        else:
            buf = f"{buf} {part}".strip()
    if buf:
        yield buf


def iter_chunks(
    text: str,
    max_chars: int = 400,
    paragraph_pause_ms: int = 650,
    sentence_pause_ms: int = 280,
    expand_abbreviations: bool = True,
) -> Iterator[Chunk]:
    """Yield speakable chunks of <= max_chars, packed at sentence boundaries.

    Raises InvalidInputError if the text contains nothing speakable.
    """
    if max_chars < 20:
        raise InvalidInputError("max_chars must be >= 20")
    norm = normalize(text, expand_abbreviations)
    index, produced = 0, False
    for para in _paragraphs(norm):
        pieces: list[str] = []
        for sent in _SENT_END.split(para):
            sent = sent.strip()
            if sent and _has_speakable(sent):
                pieces.extend(_split_long(sent, max_chars))
        # pack consecutive short sentences together (fewer synth calls, smoother prosody)
        packed: list[str] = []
        for p in pieces:
            if packed and len(packed[-1]) + len(p) + 1 <= max_chars:
                packed[-1] = f"{packed[-1]} {p}"
            else:
                packed.append(p)
        for i, p in enumerate(packed):
            last = i == len(packed) - 1
            yield Chunk(p, paragraph_pause_ms if last else sentence_pause_ms, index, last)
            index += 1
            produced = True
    if not produced:
        raise InvalidInputError("no speakable text (input is empty or has no letters/digits)")


def count_chunks(chunks: Iterable[Chunk]) -> int:
    return sum(1 for _ in chunks)
